"""Semantisches Need/Offer-Matching.

Score(A→B) = 0.6 · cos(Bedarf_A, Angebot_B) + 0.4 · cos(Bedarf_B, Angebot_A)
→ bevorzugt Paare, die sich gegenseitig helfen können (nicht nur einseitig).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from ..extensions import db
from ..models import Match, Profile, User, utcnow
from . import embeddings as emb
from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

W_FORWARD, W_BACKWARD = 0.6, 0.4


def refresh_embeddings(profile: Profile, commit: bool = True) -> None:
    vecs, provider = emb.embed([profile.need_text, profile.offer_text], input_type="document")
    profile.embed_need, profile.embed_offer = vecs[0], vecs[1]
    profile.embed_provider = provider
    profile.embed_updated_at = utcnow()
    if commit:
        db.session.commit()


def _pool(exclude_user_id: int | None = None, user_ids: list[int] | None = None) -> list[Profile]:
    q = (Profile.query.join(User).filter(User.status == "active", Profile.allow_matching.is_(True),
                                         Profile.embed_offer.isnot(None)))
    if exclude_user_id:
        q = q.filter(Profile.user_id != exclude_user_id)
    if user_ids is not None:
        q = q.filter(Profile.user_id.in_(user_ids))
    return q.all()


def pair_score(a: Profile, b: Profile) -> float:
    return (W_FORWARD * emb.cosine(a.embed_need, b.embed_offer)
            + W_BACKWARD * emb.cosine(b.embed_need, a.embed_offer))


def _version(a: Profile, b: Profile) -> str:
    raw = f"{a.user_id}:{a.updated_at}:{b.user_id}:{b.updated_at}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _snip(text: str, n: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + " …"


def _fallback_reason(a: Profile, b: Profile) -> tuple[str, str]:
    reason = (f"{b.user.first_name} bringt mit: {_snip(b.q_can_help or b.q_focus)}. "
              f"Das passt zu dem, was du suchst: {_snip(a.q_looking_for or a.q_challenge)}.")
    opener = (f"Erzähl {b.user.first_name} von deiner Herausforderung „{_snip(a.q_challenge, 70)}“ "
              "und frag nach Erfahrungen und Kontakten dazu.") if a.q_challenge else \
        f"Frag {b.user.first_name}, woran gerade gearbeitet wird und wo Unterstützung hilft."
    return reason, opener


def _llm_reasons(a: Profile, others: list[Profile]) -> dict[int, tuple[str, str]]:
    def card(p: Profile) -> dict:
        return {"id": p.user_id, "name": p.user.first_name, "rolle": p.headline, "branche": p.industry,
                "taetigkeit": p.q_focus, "herausforderung": p.q_challenge, "kann_helfen": p.q_can_help,
                "sucht": p.q_looking_for, "expertise": p.expertise}

    system = (
        "Du bist die Matching-Engine von {CLUB}. Du erklärst kurz, konkret und "
        "wertschätzend, warum zwei Menschen sich treffen sollten. Ton: ruhig, hochwertig, auf Deutsch, du-Form. "
        "Keine Übertreibungen, keine erfundenen Fakten — nur was in den Profilen steht. "
        "Antworte ausschließlich mit gültigem JSON, ohne Markdown."
    )
    prompt = (
        "Person A:\n" + json.dumps(card(a), ensure_ascii=False) + "\n\nKandidat:innen:\n"
        + json.dumps([card(o) for o in others], ensure_ascii=False)
        + "\n\nGib für jede Kandidatin/jeden Kandidaten zurück: "
          '[{"id": <id>, "reason": "<max. 2 Sätze an Person A: warum dieses Treffen Wert hat>", '
          '"opener": "<1 konkreter Gesprächseinstieg>"}]'
    )
    text = complete(system, [{"role": "user", "content": prompt}], max_tokens=1200, temperature=0.3, purpose="matching")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    data = json.loads(text)
    return {int(d["id"]): (str(d.get("reason", "")), str(d.get("opener", ""))) for d in data if "id" in d}


def top_matches(user: User, k: int = 6, explain: bool = True) -> list[Match]:
    me = user.profile
    if not me or not me.embed_need:
        return []
    pool = _pool(exclude_user_id=user.id)
    scored = sorted(((pair_score(me, p), p) for p in pool), key=lambda t: t[0], reverse=True)

    dismissed = {m.other_id for m in Match.query.filter_by(user_id=user.id, dismissed=True)}
    scored = [(s, p) for s, p in scored if p.user_id not in dismissed][:k]

    results, need_reason = [], []
    for s, p in scored:
        m = Match.query.filter_by(user_id=user.id, other_id=p.user_id).first()
        if m is None:
            m = Match(user_id=user.id, other_id=p.user_id)
            db.session.add(m)
        m.score = round(s, 4)
        v = _version(me, p)
        if m.profile_version != v or not m.reason:
            m.profile_version = v
            need_reason.append((m, p))
        results.append(m)

    if need_reason and explain:
        reasons: dict[int, tuple[str, str]] = {}
        try:
            reasons = _llm_reasons(me, [p for _, p in need_reason])
        except (LLMUnavailable, ValueError, KeyError, TypeError) as exc:
            log.info("Matching-Begründung per Fallback (%s)", exc)
        for m, p in need_reason:
            m.reason, m.opener = reasons.get(p.user_id) or _fallback_reason(me, p)
    db.session.commit()
    return results


def semantic_search(query: str, k: int = 12, only_directory: bool = False,
                    exclude_user_id: int | None = None) -> list[tuple[float, Profile]]:
    """Organisator:innen-/Mitgliedersuche: 'Fachanwalt für Arbeitsrecht', 'Partner für IT-Startup' …"""
    if not query.strip():
        return []
    qv, _ = emb.embed([query], input_type="query")
    q = Profile.query.join(User).filter(User.status == "active", Profile.embed_offer.isnot(None))
    if only_directory:
        q = q.filter(Profile.visible_in_directory.is_(True))
    if exclude_user_id:
        q = q.filter(Profile.user_id != exclude_user_id)
    hits = [(emb.cosine(qv[0], p.embed_offer), p) for p in q.all()]
    hits.sort(key=lambda t: t[0], reverse=True)
    return [h for h in hits[:k] if h[0] > 0.05]


def event_pairings(user_ids: list[int], per_person: int = 3) -> list[dict]:
    """Für die Organisator:in: pro angemeldeter Person die Top-Gesprächspartner:innen im Raum."""
    pool = _pool(user_ids=user_ids)
    out = []
    for a in pool:
        ranked = sorted(((pair_score(a, b), b) for b in pool if b.user_id != a.user_id),
                        key=lambda t: t[0], reverse=True)[:per_person]
        out.append({"person": a, "matches": [{"profile": b, "score": round(s, 3)} for s, b in ranked]})
    out.sort(key=lambda d: d["person"].user.first_name)
    return out


def fit_label(score: float) -> str:
    """Verständliche Einordnung statt roher Cosinus-Werte (Skala hängt vom Embedding-Modell ab)."""
    local = emb.provider_name() == "local"
    hi, mid = (0.16, 0.09) if local else (0.55, 0.40)
    if score >= hi:
        return "Sehr hohe Passung"
    if score >= mid:
        return "Hohe Passung"
    return "Interessante Ergänzung"


def reembed_all() -> int:
    n = 0
    for p in Profile.query.all():
        refresh_embeddings(p, commit=False)
        n += 1
    db.session.commit()
    return n
