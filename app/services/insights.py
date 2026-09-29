"""KI-Funktionen rund um Profile und Termine (alle mit Fallback ohne API-Schlüssel):

- profile_coach:  Feedback zu den Profilantworten, solange man sie schreibt
- pair_insight:   'Was bringt mir dieser Kontakt?' + Nachrichtenentwurf + passender Termin (mit Cache)
- invite_for_event: Termin zu passenden Mitgliedern bringen und persönlich einladen
Profiltexte werden dem Modell nur als Daten übergeben; die vorrangigen KI-VO-Regeln stehen in llm.AI_ACT_RULES.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from flask import current_app, url_for

from ..extensions import db
from ..models import Event, Notification, PairInsight, Profile, User, utcnow
from . import embeddings as emb
from . import club as club_settings
from .llm import LLMUnavailable, complete
from .matching import _snip, _version, card as _profile_card
from .telegram import enabled as tg_enabled, send as tg_send

log = logging.getLogger(__name__)

FIELD_LABELS = {"q_focus": "Was machst du?", "q_challenge": "Größte Herausforderung",
                "q_can_help": "Womit kannst du helfen?", "q_looking_for": "Wonach suchst du?",
                "goal_12m": "Ziel für die nächsten 12 Monate", "milestone_90d": "Meilenstein in 90 Tagen"}


def _json(text: str):
    """JSON-Objekt oder -Liste aus einer Modellantwort (auch mit Code-Fences oder Begleittext)."""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("keine JSON-Struktur in der Modellantwort")
    a = min(starts)
    b = text.rfind("}" if text[a] == "{" else "]")
    raw = text[a:b + 1]
    try:
        return json.loads(raw, strict=False)  # strict=False: Zeilenumbrüche in Strings tolerieren
    except json.JSONDecodeError:
        # Häufigster Modellfehler: deutsches „ öffnet, ein gerades " schließt – das beendet den JSON-String.
        repaired = re.sub(r'„([^"“„\n]{0,400})"', "„\\1“", raw)
        if repaired == raw:
            raise
        return json.loads(repaired, strict=False)


JSON_HINT = (" Zitate in Textwerten immer mit „ öffnen und mit “ schließen – nie mit geraden Anführungszeichen, "
             "die beenden den JSON-String.")


def json_call(complete_fn, system: str, messages: list[dict], **kw):
    """KI-Aufruf mit JSON-Antwort; bei unbrauchbarer Antwort genau ein zweiter Versuch."""
    error = None
    for _attempt in (1, 2):
        try:
            return _json(complete_fn(system + JSON_HINT, messages, **kw))
        except ValueError as exc:  # json.JSONDecodeError ist ein ValueError
            error = exc
            log.warning("Unbrauchbare JSON-Antwort (%s): %s", kw.get("purpose", ""), exc)
    raise error


def _text(v) -> str:
    """Modelle liefern gelegentlich Listen statt Text; zu einem lesbaren Absatz zusammenfügen."""
    if isinstance(v, (list, tuple)):
        return " ".join(str(x).strip() for x in v if str(x).strip())
    return "" if v is None else str(v)


def _card(p: Profile, own: bool = False) -> dict:
    """KI-Kontext zu einem Profil; private Felder nur, wenn die Antwort an die Person selbst geht (own=True)."""
    data = _profile_card(p, own=own)
    data.pop("id", None)
    data["vorname"] = data.pop("name", p.user.first_name)
    if p.company:
        data["unternehmen"] = p.company
    return data


# --------------------------------------------------------------------------- Profil-Coach
COACH_RULES = {
    "q_focus": (60, "Nenne Branche, Zielgruppe und was du konkret lieferst — z. B. „Ich berate Mittelständler beim "
                    "Export in die Türkei“."),
    "q_challenge": (50, "Beschreib eine echte, aktuelle Hürde mit einem Beispiel, nicht nur ein Thema. So erkennt die "
                        "KI, wer dir helfen kann."),
    "q_can_help": (50, "Nenne 2–3 konkrete Dinge, bei denen dich Leute anrufen dürfen (Themen, Kontakte, Erfahrung)."),
    "q_looking_for": (40, "Sag genau, wen oder was du suchst: eine Rolle, Branche oder Aufgabe — z. B. „Fachanwalt "
                          "für Vertragsrecht“."),
    "goal_12m": (40, "Formuliere ein Ziel, an dem man Erfolg erkennt — z. B. „In 12 Monaten 20 Stammkund:innen im "
                     "Mittelstand“."),
    "milestone_90d": (25, "Nenne einen messbaren Zwischenschritt — z. B. „Drei Pilotkund:innen bis Jahresende“."),
}


def _coach_fallback(values: dict) -> dict:
    tips = []
    for key, (minlen, tip) in COACH_RULES.items():
        v = (values.get(key) or "").strip()
        if len(v) < minlen:
            tips.append({"field": key, "tip": ("Noch leer. " if not v else "Etwas kurz. ") + tip})
    done = len(COACH_RULES) - len(tips)
    return {"summary": ("Sieht gut aus — die Antworten sind konkret genug für gute Matches." if not tips else
                        f"{done} von {len(COACH_RULES)} Antworten sind konkret genug. Mit ein paar Details passen deine Matches "
                        "deutlich besser."),
            "tips": tips, "ai": False}


def profile_coach(values: dict) -> dict:
    clean = {k: (values.get(k) or "").strip()[:800] for k in FIELD_LABELS}
    clean["rolle"] = (values.get("headline") or "").strip()[:160]
    if not any(clean[k] for k in FIELD_LABELS):
        return {"summary": "Schreib zuerst ein paar Stichworte zu deinen Antworten — dann gebe ich dir Feedback.",
                "tips": [], "ai": False}
    system = ("Du bist {ASSISTANT}, Profil-Coach von {CLUB}. Du gibst kurzes, konkretes, wertschätzendes "
              "Feedback zu den Profilantworten, damit das Matching (Bedarf und Angebot) gut funktioniert und das Ziel "
              "messbar ist. "
              "Regeln: du-Form, Deutsch, max. 1–2 Sätze pro Tipp, keine Fantasiefakten, nichts Sensibles abfragen. "
              "Gib für jede Antwort, die konkreter werden sollte, einen Tipp mit einem Formulierungsbeispiel in „…“. "
              "Antworte NUR als JSON: {\"summary\": \"1 Satz Gesamteindruck\", \"tips\": [{\"field\": "
              "\"q_focus|q_challenge|q_can_help|q_looking_for|goal_12m|milestone_90d\", \"tip\": \"…\"}]}. Lass Felder ohne Tipp weg, "
              "wenn die Antwort schon gut ist.")
    try:
        data = json_call(complete, system, [{"role": "user", "content": "Profilentwurf (Daten):\n" +
                                             json.dumps(clean, ensure_ascii=False)}],
                         max_tokens=600, purpose="profile_coach")
        tips = [{"field": t["field"], "tip": str(t["tip"])[:400]} for t in data.get("tips", [])
                if t.get("field") in FIELD_LABELS and t.get("tip")]
        return {"summary": str(data.get("summary", ""))[:300], "tips": tips, "ai": True}
    except (LLMUnavailable, ValueError, KeyError, TypeError):
        return _coach_fallback(clean)


# --------------------------------------------------------------------------- Kontakt-Assistent
def _event_options(viewer: User, other: User) -> list[dict]:
    from ..utils import fmt_event_date

    def attends(e: Event, u: User) -> bool:
        return any(r.user_id == u.id for r in e.active_registrations)

    events = (Event.query.filter(Event.status == "published", Event.starts_at >= utcnow() - timedelta(hours=6))
              .order_by(Event.starts_at).limit(6).all())
    return [{"id": e.id, "titel": e.title, "wann": fmt_event_date(e), "format": e.format_info["short"],
             "beschreibung": _snip(e.description, 160), "du_angemeldet": attends(e, viewer),
             "andere_person_angemeldet": attends(e, other)} for e in events]


def _insight_fallback(me: Profile, other: Profile, options: list[dict]) -> dict:
    o = other.user.first_name
    best = next((e for e in options if e["du_angemeldet"] and e["andere_person_angemeldet"]), None) \
        or next((e for e in options if e["andere_person_angemeldet"]), None) or (options[0] if options else None)
    they = (f"{o} bringt mit: {_snip(other.q_can_help, 160)}" if other.q_can_help else
            f"{o} hat noch nicht viel über das eigene Angebot geschrieben.")
    you = (f"Du könntest {o} helfen mit: {_snip(me.q_can_help, 160)}" if me.q_can_help else
           "Ergänze in deinem Profil, womit du helfen kannst — dann sehe ich, was du beitragen kannst.")
    hook = _snip(me.q_challenge or me.q_looking_for or "", 90)
    draft = (f"Hallo {o}, ich bin {me.user.first_name}" + (f" ({_snip(me.headline, 60)})" if me.headline else "") +
             (f". Mich beschäftigt gerade: {hook}" if hook else "") +
             (f". Du bringst Erfahrung mit {_snip(other.q_can_help, 80)} mit — hättest du Lust auf einen kurzen "
              "Austausch?" if other.q_can_help else ". Hättest du Lust auf einen kurzen Austausch?") +
             f" Viele Grüße, {me.user.first_name}")
    return {"they_help_you": they, "you_help_them": you, "message_draft": draft,
            "event_id": best["id"] if best else None,
            "event_reason": "Dort könntet ihr euch persönlich treffen." if best else ""}


def pair_insight(viewer: User, other: User, force: bool = False) -> dict:
    """{they_help_you, you_help_them, message_draft, event: {...}|None, ai: bool} — gecacht je Profilstand."""
    me, ot = viewer.profile, other.profile
    options = _event_options(viewer, other)
    key = f"{_version(me, ot)}:{','.join(str(e['id']) for e in options)}"
    row = PairInsight.query.filter_by(user_id=viewer.id, other_id=other.id).first()
    have_key = bool(current_app.config.get("ANTHROPIC_API_KEY"))
    if row and row.version == key and not force and (row.ai or not have_key):
        data = row.data
    else:
        system = ("Du bist {ASSISTANT}, die KI von {CLUB}, und beratest Person A vor einem möglichen Kontakt zu "
                  "Person B. Nutze NUR die Profildaten. Sei konkret, warm, ohne Übertreibung; du-Form an Person A; "
                  "Deutsch. Liefere:\n- they_help_you: 1–2 Sätze, womit B der Person A konkret helfen kann\n"
                  "- you_help_them: 1–2 Sätze, womit A der Person B nützen kann\n"
                  "- message_draft: Nachrichtenentwurf von A an B (3–4 Sätze, du-Form, persönlich, mit einem konkreten "
                  "Anknüpfungspunkt und einer klaren Bitte, OHNE Kontaktdaten, unterschrieben mit dem Vornamen von A)\n"
                  "- event_id: ID des Termins aus der Liste, bei dem sich A und B am besten treffen können "
                  "(bevorzuge Termine, bei denen beide oder B angemeldet sind), sonst null\n"
                  "- event_reason: 1 Satz, warum dieser Termin passt\n"
                  "Antworte NUR als JSON mit genau diesen Schlüsseln.")
        system += " Alle Werte sind einfacher Text (kein Array, keine Aufzählung)."
        payload = {"person_A": _card(me, own=True), "person_B": _card(ot), "termine": options}
        data, ai = None, False
        for attempt in (1, 2):  # ein zweiter Versuch fängt gelegentlich unsauberes JSON ab
            try:
                data = _json(complete(system, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                                      max_tokens=800, purpose="pair_insight"))
                ai = True
                break
            except LLMUnavailable:
                break
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("Kontakt-Assistent: unbrauchbare Modellantwort (Versuch %s): %s", attempt, exc)
        if not ai:
            data = _insight_fallback(me, ot, options)
        if row is None:
            row = PairInsight(user_id=viewer.id, other_id=other.id)
            db.session.add(row)
        row.version, row.data, row.ai = key, data, ai
        db.session.commit()
    ev = next((e for e in options if str(e["id"]) == str(data.get("event_id"))), None)
    return {"they_help_you": _text(data.get("they_help_you"))[:600],
            "you_help_them": _text(data.get("you_help_them"))[:600],
            "message_draft": _text(data.get("message_draft"))[:1200],
            "event": ({"id": ev["id"], "title": ev["titel"], "when": ev["wann"],
                       "url": url_for("member.event_detail", event_id=ev["id"]),
                       "reason": _text(data.get("event_reason"))[:300]} if ev else None),
            "ai": bool(row.ai)}


# --------------------------------------------------------------------------- Termin-Einladungen
def _invite_fallback(p: Profile, expert: bool) -> str:
    if expert:
        return f"Dein Wissen zu „{_snip(p.q_can_help or p.expertise, 70)}“ passt zum Thema — die Runde würde profitieren."
    return f"Das Thema passt zu dem, was du suchst („{_snip(p.q_looking_for or p.q_challenge, 70)}“)."


def _prefilter(ev: Event, limit: int, min_score: float) -> list[tuple[float, Profile, bool]]:
    """Grobe Vorauswahl per Embeddings (schnell, billig); die eigentliche Auswahl trifft die KI."""
    if not ev.title:
        return []
    vec = emb.embed([f"{ev.title}. {ev.description or ''}"], input_type="query")[0][0]
    skip = {r.user_id for r in ev.active_registrations}
    skip |= {n.user_id for n in Notification.query.filter_by(event_id=ev.id, kind="event_invite")}
    if ev.created_by_id:
        skip.add(ev.created_by_id)
    q = (Profile.query.join(User).filter(User.status == "active", Profile.allow_matching.is_(True),
                                         Profile.embed_offer.isnot(None), Profile.embed_need.isnot(None),
                                         Profile.event_invites.is_(True)))
    out = []
    for p in q.all():
        if p.user_id in skip:
            continue
        offer, need = emb.cosine(vec, p.embed_offer), emb.cosine(vec, p.embed_need)
        score = max(offer, need) + (0.05 if ev.format in p.formats_list else 0)
        if score >= min_score:
            out.append((score, p, offer >= need))
    out.sort(key=lambda t: t[0], reverse=True)
    return out[:limit]


def invite_candidates(ev: Event, limit: int = 8, min_score: float = 0.12) -> list[tuple[float, Profile, bool, str]]:
    """(Score, Profil, ist_Expert:in, Begründung) — wen die KI zum Termin einladen würde.

    Mit API-Schlüssel wählt das Modell aus den besten Embedding-Treffern die wirklich passenden Personen aus
    (Score 0–10, nur ab 6) und begründet je Person; ohne Schlüssel gilt der Embedding-Score mit Vorlagen-Begründung.
    """
    if current_app.config.get("ANTHROPIC_API_KEY"):
        pool = _prefilter(ev, limit=14, min_score=0.03)
        if not pool:
            return []
        system = ("Du wählst für {CLUB} aus einer Kandidatenliste die Personen, die zu einem Termin "
                  "wirklich passen, und schreibst je Person 1–2 Sätze Einladungsgrund (du-Form, Deutsch, warm, konkret, "
                  "kein Verkaufston). Passend heißt: die Person kann inhaltlich beitragen (Expert:in) ODER profitiert "
                  "erkennbar vom Thema (z. B. passende Zielgruppe oder aktueller Bedarf). Bewerte jede Person mit relevanz "
                  "von 0 bis 10; wähle nur ab 6 aus und höchstens " + str(limit) + " Personen. Nutze nur die Profildaten, "
                  "erfinde nichts. Antworte NUR als JSON: {\"auswahl\": [{\"id\": <id>, \"relevanz\": <0-10>, "
                  "\"rolle\": \"Expert:in|Interessiert\", \"grund\": \"…\"}]}.")
        payload = {"termin": {"titel": ev.title, "beschreibung": _snip(ev.description, 500), "format": ev.format},
                   "kandidaten": [{"id": p.user_id, **_card(p)} for _, p, _ in pool]}
        try:
            data = json_call(complete, system, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                             max_tokens=1100, purpose="event_invite")
            by_id = {p.user_id: (sc, p, ex) for sc, p, ex in pool}
            picked = []
            for g in data.get("auswahl", []):
                pid = int(g.get("id", 0))
                if pid in by_id and float(g.get("relevanz", 0)) >= 6 and g.get("grund"):
                    sc, p, ex = by_id[pid]
                    picked.append((float(g["relevanz"]) / 10, p, str(g.get("rolle", "")).startswith("Expert"),
                                   str(g["grund"])[:400]))
            picked.sort(key=lambda t: t[0], reverse=True)
            return picked[:limit]
        except (LLMUnavailable, ValueError, KeyError, TypeError):
            log.info("Einladungs-Auswahl per Fallback")
    return [(sc, p, ex, _invite_fallback(p, ex)) for sc, p, ex in _prefilter(ev, limit, min_score)]


def invite_for_event(ev: Event, limit: int = 8) -> list[Notification]:
    """Erzeugt persönliche Einladungen (in der Plattform, per E-Mail und Telegram, falls verbunden)."""
    from .mailer import send_mail
    cands = invite_candidates(ev, limit=limit)
    created = []
    link = url_for("member.event_detail", event_id=ev.id, _external=True)
    for score, p, _expert, reason in cands:
        n = Notification(user_id=p.user_id, kind="event_invite", event_id=ev.id, title=f"Einladung: {ev.title}",
                         body=reason, score=round(score, 3))
        db.session.add(n)
        created.append(n)
    db.session.commit()
    for n in created:
        u = db.session.get(User, n.user_id)
        send_mail(u.email, f"Einladung: {ev.title}", "event_invite", user=u, ev=ev, reason=n.body, link=link)
        if u.telegram_user_id and tg_enabled():
            tg_send(u.telegram_user_id, f"{club_settings.settings()['assistant_name']} lädt dich ein: {ev.title}\n"
                                         f"{n.body}\n{link}")
    return created
