"""Hybrides Matching: Texte (semantisch) + Fragebogen (strukturiert) + Ausschlüsse.

semantisch(A→B) = 0.6 · cos(Bedarf_A, Angebot_B) + 0.4 · cos(Bedarf_B, Angebot_A), kalibriert auf 0–1
strukturiert    = questionnaire.structured_fit (Rolle/Suche, Gegenseitigkeit, Ziel, Sprache, Treffen, Werte)
Score           = 0.65 · semantisch + 0.35 · strukturiert  (ohne Auswahlangaben: nur semantisch)
Ausschluss      = „Womit möchtest du nicht kontaktiert werden?“ einer der beiden Seiten passt sehr stark zum Angebot
                  der anderen → keine Empfehlung (beide Richtungen); bei mittlerer Ähnlichkeit nur Abwertung.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from ..extensions import db
from ..models import Match, Profile, User, utcnow
from ..questionnaire import (FIRST_OUTCOMES, GOAL_CATEGORIES, HELP_MODES, LANGUAGES, PARTNER_TYPES, RESOURCES, ROLES,
                             STAGES, club_questions, custom_display, label, labels, shared_facts, structured_fit)
from . import embeddings as emb
from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

W_FORWARD, W_BACKWARD = 0.6, 0.4
W_SEMANTIC, W_STRUCTURED = 0.65, 0.35
AVOID_BLOCK = 0.9   # kalibrierte Ähnlichkeit, ab der ein Ausschluss greift
AVOID_SOFT = 0.5    # darüber wird die Empfehlung abgewertet (Wortähnlichkeit ist kein sicherer Treffer)

# Typische Cosinus-Spannen je Embedding-Anbieter (unpassend -> sehr passend); macht Scores vergleichbar
_CALIBRATION = {"local": (0.02, 0.22), "openai": (0.20, 0.60), "voyage": (0.25, 0.70)}


def calibrate(raw: float) -> float:
    lo, hi = _CALIBRATION.get(emb.provider_name(), _CALIBRATION["local"])
    return max(0.0, min(1.0, (raw - lo) / (hi - lo)))


def refresh_embeddings(profile: Profile, commit: bool = True) -> None:
    texts = [profile.need_text, profile.offer_text]
    if (profile.not_wanted or "").strip():
        texts.append(profile.not_wanted)
    vecs, provider = emb.embed(texts, input_type="document")
    profile.embed_need, profile.embed_offer = vecs[0], vecs[1]
    profile.embed_avoid = vecs[2] if len(vecs) > 2 else None
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


def semantic_score(a: Profile, b: Profile) -> float:
    return calibrate(W_FORWARD * emb.cosine(a.embed_need, b.embed_offer)
                     + W_BACKWARD * emb.cosine(b.embed_need, a.embed_offer))


def _avoid(a: Profile, b: Profile) -> float:
    """Wie stark passt das Angebot einer Seite zu dem, was die andere ausgeschlossen hat? (0–1, beide Richtungen)"""
    return max((calibrate(emb.cosine(x.embed_avoid, y.embed_offer)) for x, y in ((a, b), (b, a)) if x.embed_avoid),
               default=0.0)


def blocked(a: Profile, b: Profile) -> bool:
    """True, wenn eine Seite genau diese Art Kontakt ausgeschlossen hat."""
    return _avoid(a, b) >= AVOID_BLOCK


def pair_score(a: Profile, b: Profile) -> float:
    sem = semantic_score(a, b)
    struct = structured_fit(a, b)
    score = sem if struct is None else W_SEMANTIC * sem + W_STRUCTURED * struct
    return max(0.0, score - 0.5 * max(0.0, _avoid(a, b) - AVOID_SOFT))


def _version(a: Profile, b: Profile) -> str:
    raw = f"{a.user_id}:{a.updated_at}:{b.user_id}:{b.updated_at}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _snip(text: str, n: int = 140) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + " …"


def _fallback_reason(a: Profile, b: Profile) -> tuple[str, str]:
    facts = shared_facts(a, b)
    reason = (f"{b.user.first_name} bringt mit: {_snip(b.q_can_help or b.asked_for or b.q_focus)}. "
              f"Das passt zu dem, was du suchst: {_snip(a.q_looking_for or a.goal_12m or a.q_challenge)}.")
    if facts:
        reason += f" Außerdem: {b.user.first_name} " + ", ".join(facts[:2]) + "."
    opener = (f"Erzähl {b.user.first_name} von deiner Herausforderung „{_snip(a.q_challenge, 70)}“ "
              "und frag nach Erfahrungen und Kontakten dazu.") if a.q_challenge else \
        f"Frag {b.user.first_name}, woran gerade gearbeitet wird und wo Unterstützung hilft."
    return reason, opener


def card(p: Profile, own: bool = False) -> dict:
    """Profil als KI-Kontext. own=False: nur, was die andere Person sehen darf (private Felder bleiben draußen)."""
    get = (lambda f: getattr(p, f) or "") if own else p.shown
    data = {"id": p.user_id, "name": p.user.first_name, "rolle": p.headline, "funktion": label(ROLES, p.role),
            "phase": label(STAGES, p.stage), "branche": p.industry, "taetigkeit": p.q_focus,
            "kann_helfen": p.q_can_help, "wird_gefragt_zu": p.asked_for, "bringt_ein": labels(RESOURCES, p.resources),
            "hilft_so": label(HELP_MODES, p.help_mode), "sucht": p.q_looking_for,
            "sucht_typen": labels(PARTNER_TYPES, p.partner_types),
            "erstes_gespraech": label(FIRST_OUTCOMES, p.first_outcome),
            "ziel_kategorie": label(GOAL_CATEGORIES, get("goal_category")), "ziel": get("goal_12m"),
            "herausforderung": get("q_challenge"), "schon_versucht": get("q_tried"),
            "sprachen": labels(LANGUAGES, p.languages), "stolz_auf": p.proud_of, "expertise": p.expertise}
    if p.custom_answers:
        data["klubfragen"] = {q.label: custom_display(p, q) for q in club_questions()
                              if (own or q.public) and custom_display(p, q)}
    return {k: v for k, v in data.items() if v}


def _llm_reasons(a: Profile, others: list[Profile]) -> dict[int, tuple[str, str]]:
    system = (
        "Du bist die Matching-Engine von {CLUB}. Du erklärst kurz, konkret und "
        "wertschätzend, warum zwei Menschen sich treffen sollten. Ton: ruhig, hochwertig, auf Deutsch, du-Form. "
        "Keine Übertreibungen, keine erfundenen Fakten — nur was in den Profilen steht. Beziehe dich auf das Ziel "
        "und die gesuchten Partnertypen von Person A und darauf, was die Kandidat:innen einbringen. "
        "Antworte ausschließlich mit gültigem JSON, ohne Markdown."
    )
    prompt = (
        "Person A:\n" + json.dumps(card(a, own=True), ensure_ascii=False) + "\n\nKandidat:innen:\n"
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
    scored = sorted(((pair_score(me, p), p) for p in pool if not blocked(me, p)), key=lambda t: t[0],
                    reverse=True)

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
        ranked = sorted(((pair_score(a, b), b) for b in pool if b.user_id != a.user_id and not blocked(a, b)),
                        key=lambda t: t[0], reverse=True)[:per_person]
        out.append({"person": a, "matches": [{"profile": b, "score": round(s, 3)} for s, b in ranked]})
    out.sort(key=lambda d: d["person"].user.first_name)
    return out


def fit_label(score: float) -> str:
    """Verständliche Einordnung statt Rohwerten (Score ist bereits auf 0–1 kalibriert)."""
    if score >= 0.6:
        return "Sehr hohe Passung"
    if score >= 0.33:
        return "Hohe Passung"
    return "Interessante Ergänzung"


def reembed_all() -> int:
    n = 0
    for p in Profile.query.all():
        refresh_embeddings(p, commit=False)
        n += 1
    db.session.commit()
    return n
