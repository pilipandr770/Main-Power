"""KI-Assistenz eines Klubs (Standardname Aiko; Name, Klub und Fakten aus den Klub-Einstellungen).

Zwei Modi:
- public: Besucher:innen ohne Konto. Keine Speicherung auf dem Server, kein Zugriff auf Mitgliederdaten.
- member: kennt das eigene Profil, kommende Termine, Leistungen und die Top-Matches (nur Personen mit
          Matching-Einwilligung; es werden NIE Kontaktdaten herausgegeben).

KI-Transparenz (Art. 50 KI-VO): Aiko stellt sich als KI vor, die Oberfläche kennzeichnet sie dauerhaft.
"""
from __future__ import annotations

import re
from datetime import timedelta

from flask import current_app, url_for

from ..extensions import db
from ..utils import scrub_contacts
from ..models import FORMATS, ChatMessage, Event, KnowledgeItem, Service, Setting, User, utcnow
from .llm import LLMUnavailable, complete

BASE_PERSONA = """Du bist {assistant}, die KI-Assistentin von {club}{city}.
{about}

Ton: ruhig, hochwertig, klar, warm — nie aufdringlich, nie werblich überdreht. Du-Form. Deutsch, außer die Person
schreibt eindeutig in einer anderen Sprache. Kurze Absätze, maximal ca. 120 Wörter, außer es wird mehr verlangt.

Regeln:
- Du bist eine KI und sagst das offen, wenn danach gefragt wird oder es relevant ist.
- Erfinde keine Termine, Preise, Personen oder Leistungen. Nutze nur den Kontext unten. Wenn du etwas nicht weißt,
  verweise auf {contact}.
- Keine Rechts-, Steuer- oder Medizinberatung — verweise auf passende Fachleute (gern aus der Community).
- Gib niemals Kontaktdaten (E-Mail, Telefon, Social-Media-Links) anderer Mitglieder heraus. Für Kontakt immer auf
  die Funktion „Kontakt anfragen“ auf der Plattform verweisen — Kontaktdaten werden erst nach Zustimmung geteilt.
- Frage nicht nach sensiblen Daten (Gesundheit, Religion, Finanzen, Ausweisnummern).
"""


def _contact() -> str:
    from . import club as club_settings
    return club_settings.settings().get("contact_email") or "das Team"


def _persona() -> str:
    from . import club as club_settings
    c = club_settings.settings()
    return BASE_PERSONA.format(assistant=c["assistant_name"], club=c["full_name"],
                               city=f" in {c['city']}" if c.get("city") else "", about=c.get("about") or "",
                               contact=c.get("contact_email") or "das Team")


def _formats_block() -> str:
    lines = []
    for key, f in FORMATS.items():
        if key == "community":
            continue
        price = "kostenlos" if not f["price_cents"] else f"{f['price_cents'] / 100:.0f} € pro Person"
        lines.append(f"- {f['name']} ({price}; {f['rhythm']}): {f['description']}")
    from . import club as club_settings
    facts = club_settings.settings().get("assistant_facts")
    if facts:
        lines.append("- " + facts)
    return "\n".join(lines)


def _events_block(limit: int = 8) -> str:
    now = utcnow() - timedelta(hours=6)
    events = (Event.query.filter(Event.status == "published", Event.starts_at >= now)
              .order_by(Event.starts_at).limit(limit).all())
    if not events:
        return "Aktuell sind keine Termine veröffentlicht."
    from ..utils import fmt_event_date
    return "\n".join(
        f"- {fmt_event_date(e)} · {e.title}" + (f" · {e.price_cents / 100:.0f} €" if e.is_paid else " · kostenlos")
        for e in events)


def _faq_block(public_only: bool) -> str:
    q = KnowledgeItem.query.filter_by(active=True)
    if public_only:
        q = q.filter_by(public=True)
    return "\n".join(f"F: {k.question}\nA: {k.answer}" for k in q.order_by(KnowledgeItem.sort))


def _services_block() -> str:
    services = Service.query.filter_by(active=True).order_by(Service.sort).all()
    return "\n".join(f"- {s.title}: {s.summary}" + (f" (Community-Vorteil: {s.member_benefit})" if s.member_benefit else "")
                     for s in services) or "—"


def _profile_block(user: User) -> str:
    p = user.profile
    if not p:
        return "Profil noch leer."
    from ..questionnaire import (GOAL_CATEGORIES, HELP_MODES, LANGUAGES, PARTNER_TYPES, RESOURCES, ROLES, STAGES,
                                 label, labels)
    lines = [
        f"Name: {user.first_name}",
        f"Rolle/Unternehmen: {p.headline} {('· ' + p.company) if p.company else ''}",
        f"Funktion und Phase: {label(ROLES, p.role)} {('· ' + label(STAGES, p.stage)) if p.stage else ''}",
        f"Branche: {p.industry}",
        f"Tätigkeitsbereich: {p.q_focus}",
        f"Ziel (12 Monate, {label(GOAL_CATEGORIES, p.goal_category) or 'ohne Kategorie'}): {p.goal_12m}",
        f"Meilenstein in 90 Tagen: {p.milestone_90d}",
        f"Größte Herausforderung: {p.q_challenge}",
        f"Schon versucht: {p.q_tried}",
        f"Sucht (Partnertypen): {', '.join(labels(PARTNER_TYPES, p.partner_types))}",
        f"Sucht konkret: {p.q_looking_for}",
        f"Kann anderen helfen mit: {p.q_can_help}",
        f"Bringt ein: {', '.join(labels(RESOURCES, p.resources))} · {label(HELP_MODES, p.help_mode)}",
        f"Sprachen: {', '.join(labels(LANGUAGES, p.languages))}",
        f"Expertise: {p.expertise}",
        f"Bevorzugte Formate: {p.preferred_formats}",
        f"Matching-Einwilligung: {'ja' if p.allow_matching else 'nein'}",
    ]
    from ..models import GoalCheckin
    last = (GoalCheckin.query.filter_by(user_id=user.id)
            .order_by(GoalCheckin.created_at.desc(), GoalCheckin.id.desc()).first())
    if last:
        lines.append(f"Letzter Ziel-Check-in ({last.created_at:%d.%m.%Y}): {last.status_label}"
                     + (f" – {last.note}" if last.note else ""))
    # Leere Angaben weglassen, damit das Modell nichts hineininterpretiert
    return "\n".join(line for line in lines if line.split(":", 1)[1].strip(" ·"))


def _matches_block(user: User) -> str:
    if not (user.profile and user.profile.allow_matching and user.profile.embed_need):
        return "Keine Matches: Matching ist nicht aktiviert oder das Profil ist noch unvollständig."
    from .matching import top_matches
    ms = top_matches(user, k=5, explain=False)
    if not ms:
        return "Noch keine passenden Mitglieder gefunden."
    lines = []
    for m in ms:
        o = m.other.profile
        lines.append(f"- {m.other.first_name} {m.other.last_name[:1]}. — {scrub_contacts(o.headline or '')}; kann helfen mit: "
                     f"{scrub_contacts((o.q_can_help or '')[:160])} (Score {m.score:.2f})")
    return "\n".join(lines)


def _fallback_answer(message: str, public_only: bool) -> str:
    """Ohne API-Key: einfache Schlagwortsuche in der Wissensbasis."""
    words = {w for w in re.findall(r"[a-zäöüß]{4,}", message.lower())}
    best, best_score = None, 0
    for k in KnowledgeItem.query.filter_by(active=True).all():
        if public_only and not k.public:
            continue
        hay = (k.question + " " + k.answer).lower()
        score = sum(1 for w in words if w in hay)
        if score > best_score:
            best, best_score = k, score
    if best:
        from . import club as club_settings
        return best.answer + f"\n\n_({club_settings.settings()['assistant_name']} läuft im Demo-Modus ohne KI-Anbindung.)_"
    return ("Das kann ich im Demo-Modus leider nicht beantworten. Schreib uns gern an " + _contact() + ".\n\n"
            "_(Die KI-Assistenz läuft im Demo-Modus ohne KI-Anbindung.)_")


def answer_public(history: list[dict]) -> str:
    history = [h for h in history if h.get("role") in ("user", "assistant")][-10:]
    last = next((h["content"] for h in reversed(history) if h["role"] == "user"), "")
    system = (_persona() + "\nModus: Öffentliche Website. Die Person ist (noch) kein eingeloggtes Mitglied. "
              "Hilf, das passende Format zu finden und lade — wenn es passt — zur Registrierung ein, damit sie ein "
              f"Profil anlegen und passende Kontakte erhalten kann. Registrierung: {url_for('auth.register', _external=True)}\n"
              "Du hast KEINEN Zugriff auf Mitgliederdaten.\n\n"
              f"# Formate\n{_formats_block()}\n\n# Kommende Termine\n{_events_block()}\n\n"
              f"# FAQ\n{_faq_block(public_only=True)}\n\n{Setting.get('aiko_extra_instructions')}")
    try:
        return complete(system, history, max_tokens=600, purpose="aiko_public")
    except LLMUnavailable:
        return _fallback_answer(last, public_only=True)


def answer_member(user: User, message: str, channel: str = "web") -> str:
    message = message.strip()[:2000]
    db.session.add(ChatMessage(user_id=user.id, channel=channel, role="user", content=message))
    db.session.commit()

    history = (ChatMessage.query.filter_by(user_id=user.id)
               .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(14).all())
    msgs = [{"role": m.role, "content": m.content} for m in reversed(history)]

    from flask import request
    base = request.host_url.rstrip("/") if request else current_app.config["BASE_URL"]
    system = (_persona() + f"\nModus: Persönlicher Chat mit dem Mitglied {user.first_name}"
              f" (Kanal: {channel}). Du kennst das Profil und gibst persönliche, konkrete Empfehlungen: "
              "passende Formate und Termine, passende Mitglieder aus der Liste unten (nur Vorname + Initial, Rolle, "
              "warum), Tipps für die Vorstellungsrunde beim Treffen, und — nur wenn es wirklich zum Bedarf passt — "
              "passende Leistungen aus dem Ökosystem des Klubs.\n"
              "Hat das Mitglied ein Ziel oder einen 90-Tage-Meilenstein angegeben, richte Empfehlungen daran aus und "
              "frag bei Gelegenheit nach dem Fortschritt. Schlag nichts vor, was unter „Schon versucht“ steht.\n"
              "Wenn das Profil lückenhaft ist, stelle eine gezielte Rückfrage und empfiehl, das Profil zu ergänzen "
              f"({base}/app/profil).\nKontakt zu Mitgliedern nur über „Kontakt anfragen“ ({base}/app/matches).\n\n"
              f"# Profil des Mitglieds\n{_profile_block(user)}\n\n"
              f"# Passende Mitglieder (vorberechnet)\n{_matches_block(user)}\n\n"
              f"# Formate\n{_formats_block()}\n\n# Kommende Termine\n{_events_block()}\n\n"
              f"# Leistungen im Ökosystem\n{_services_block()}\n\n# FAQ\n{_faq_block(public_only=False)}\n\n"
              f"{Setting.get('aiko_extra_instructions')}")
    try:
        reply = complete(system, msgs, max_tokens=900, purpose="aiko_member")
    except LLMUnavailable:
        reply = _fallback_answer(message, public_only=False)

    db.session.add(ChatMessage(user_id=user.id, channel=channel, role="assistant", content=reply))
    db.session.commit()
    return reply
