"""90-Tage-Check-ins: Ziel und Meilenstein im Blick behalten.

Ablauf: Wer einen Meilenstein einträgt, startet einen 90-Tage-Zeitraum (Profile.milestone_set_at). Ist er um, erinnert
`flask goal-checkins` (täglich per Cron) einmal in der Plattform, per E-Mail und Telegram. Das Mitglied meldet selbst,
wie es steht; Aiko schlägt daraufhin den nächsten Schritt vor – mit passenden Mitgliedern und Terminen aus dem Klub.
Der Fortschritt wird nie anderen Mitgliedern gezeigt; die Klubleitung sieht nur anonyme Summen (Auswertung).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from flask import url_for

from ..extensions import db
from ..utils import scrub_contacts
from ..models import Event, GoalCheckin, Notification, Profile, User, utcnow
from .llm import LLMUnavailable, complete
from .matching import _snip, card

log = logging.getLogger(__name__)

PERIOD = timedelta(days=90)
KIND = "goal_checkin"


def period_start(p: Profile):
    return p.milestone_set_at or p.updated_at


def due_at(p: Profile):
    start = period_start(p)
    return start + PERIOD if (p.milestone_90d and start) else None


def days_left(p: Profile) -> int | None:
    due = due_at(p)
    return None if due is None else (due - utcnow()).days


def is_due(p: Profile) -> bool:
    left = days_left(p)
    return left is not None and left <= 0


def history(user: User, limit: int = 12) -> list[GoalCheckin]:
    return (GoalCheckin.query.filter_by(user_id=user.id)
            .order_by(GoalCheckin.created_at.desc(), GoalCheckin.id.desc()).limit(limit).all())


# --------------------------------------------------------------------------- Check-in
def _context(user: User) -> dict:
    from .matching import top_matches
    matches = []
    if user.profile.allow_matching and user.profile.embed_need:
        for m in top_matches(user, k=4, explain=False):
            o = m.other.profile
            matches.append({"vorname": m.other.first_name, "rolle": o.headline, "kann_helfen": scrub_contacts(_snip(o.q_can_help, 160)),
                            "bringt_ein": card(o).get("bringt_ein", [])})
    events = [{"titel": e.title, "wann": f"{e.starts_at:%d.%m.%Y}"} for e in
              Event.query.filter(Event.status == "published", Event.starts_at >= utcnow())
              .order_by(Event.starts_at).limit(4)]
    return {"matches": matches, "events": events}


def _fallback_feedback(c: GoalCheckin, ctx: dict) -> str:
    lead = {"erreicht": "Glückwunsch – Meilenstein geschafft.",
            "auf_kurs": "Gut, dass es vorangeht.",
            "haengt": "Danke für die ehrliche Einschätzung – genau dafür ist die Community da.",
            "neu": "Neu ausrichten ist oft der klügste Schritt."}.get(c.status, "")
    parts = [lead]
    if c.next_milestone:
        parts.append(f"Dein nächster Meilenstein: „{_snip(c.next_milestone, 120)}“.")
    if ctx["matches"]:
        m = ctx["matches"][0]
        parts.append(f"Sprich mit {m['vorname']} ({_snip(m['rolle'], 60)}) – {m['kann_helfen'].rstrip('.')}.")
    if ctx["events"]:
        parts.append(f"Nächste Gelegenheit, Leute zu treffen: {ctx['events'][0]['titel']} am {ctx['events'][0]['wann']}.")
    return " ".join(p for p in parts if p)


def _ai_feedback(user: User, c: GoalCheckin, previous: list[GoalCheckin], ctx: dict) -> str:
    system = ("Du bist {ASSISTANT}, die KI von {CLUB}. Ein Mitglied meldet den Stand seines 90-Tage-Meilensteins. "
              "Antworte in 3–5 Sätzen, du-Form, Deutsch, warm und konkret, als reiner Text ohne Markdown und ohne "
              "Emojis. Der Ton folgt dem Status: „Erreicht“ – gratulieren; „Auf gutem Weg“ – bestärken; „Hängt gerade“ "
              "– KEIN Glückwunsch, sondern Verständnis und ein Weg aus der Blockade; „Neu ausgerichtet“ – die neue "
              "Richtung würdigen. Nenne EINEN nächsten Schritt für die kommenden 2 Wochen und – nur wenn es passt – ein "
              "oder zwei Mitglieder aus der Liste (Vorname, warum) oder einen Termin. Versprich nichts im Namen des "
              "Teams oder der Plattform (keine Vorstellung, kein Rückruf): Kontakt knüpft das Mitglied selbst über "
              "„Kontakt anfragen“. Keine Rechts-, Steuer- oder Finanzberatung, nichts erfinden, nichts vorschlagen, was "
              "unter „schon versucht“ steht.")
    payload = {"profil": card(user.profile, own=True),
               "check_in": {"meilenstein": c.milestone, "status": c.status_label, "notiz": c.note,
                            "naechster_meilenstein": c.next_milestone},
               "fruehere_check_ins": [{"meilenstein": p.milestone, "status": p.status_label} for p in previous[:3]],
               "passende_mitglieder": ctx["matches"], "termine": ctx["events"]}
    text = complete(system, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    max_tokens=450, purpose="goal_checkin")
    return re.sub(r"\*\*|__|^#+\s*", "", text, flags=re.M).strip()  # Markdown-Reste entfernen


def record_checkin(user: User, status: str, note: str = "", next_milestone: str = "", goal: str | None = None
                   ) -> GoalCheckin:
    from .matching import refresh_embeddings
    p = user.profile
    previous = history(user, limit=3)
    c = GoalCheckin(user_id=user.id, goal=p.goal_12m or "", milestone=p.milestone_90d or "",
                    status=status if status in GoalCheckin.STATUS else "auf_kurs", note=(note or "").strip()[:1000],
                    next_milestone=(next_milestone or "").strip()[:400])
    if goal is not None and goal.strip() and goal.strip() != p.goal_12m:
        p.goal_12m = goal.strip()[:800]
    if c.next_milestone:
        p.milestone_90d = c.next_milestone
    p.milestone_set_at = utcnow()  # neuer 90-Tage-Zeitraum
    ctx = _context(user)
    try:
        c.ai_feedback = _ai_feedback(user, c, previous, ctx)[:1500]
    except LLMUnavailable:
        c.ai_feedback = _fallback_feedback(c, ctx)
    db.session.add(c)
    # erledigte Erinnerungen als gelesen markieren
    Notification.query.filter_by(user_id=user.id, kind=KIND, read_at=None).update({"read_at": utcnow()})
    refresh_embeddings(p, commit=False)
    db.session.commit()
    return c


# --------------------------------------------------------------------------- Erinnerungen
def open_reminder(user: User) -> Notification | None:
    return (Notification.query.filter_by(user_id=user.id, kind=KIND, read_at=None)
            .order_by(Notification.created_at.desc()).first())


def _assistant() -> str:
    from . import club as club_settings
    return club_settings.settings().get("assistant_name") or "die KI-Assistenz"


def _goal_link() -> str:
    """Link zur Zielseite – auch ohne Request (Cron): über die Domain des aktuellen Klubs."""
    from flask import current_app, has_request_context
    from ..tenancy import current_club
    if has_request_context():
        return url_for("member.goal_page", _external=True)
    club, cfg = current_club(), current_app.config
    base = cfg["BASE_URL"]
    if club and club.domain_list:
        base = f"https://{club.domain_list[0]}"
    elif club and cfg.get("PLATFORM_DOMAIN") and club.slug != cfg.get("DEFAULT_CLUB_SLUG"):
        base = f"https://{club.slug}.{cfg['PLATFORM_DOMAIN']}"
    return base.rstrip("/") + "/app/ziel"


def send_reminders(limit: int = 500) -> int:
    """Einmal pro Zeitraum erinnern (im aktuellen Klub). Gibt die Zahl neuer Erinnerungen zurück."""
    from .mailer import send_mail
    from .telegram import enabled as tg_enabled, send as tg_send
    sent = 0
    q = (Profile.query.join(User).filter(User.status == "active", Profile.milestone_90d.isnot(None),
                                         Profile.milestone_90d != ""))
    for p in q.limit(limit).all():
        if not is_due(p):
            continue
        start = period_start(p)
        if Notification.query.filter(Notification.user_id == p.user_id, Notification.kind == KIND,
                                     Notification.created_at >= start).first():
            continue
        body = (f"Vor 90 Tagen hast du dir vorgenommen: „{_snip(p.milestone_90d, 160)}“. Wie weit bist du? "
                f"Zwei Minuten reichen – danach schlägt dir {_assistant()} den nächsten Schritt und passende "
                "Kontakte vor.")
        db.session.add(Notification(user_id=p.user_id, kind=KIND, title="Zeit für deinen 90-Tage-Check-in",
                                    body=body))
        db.session.commit()
        if p.goal_reminders:  # abbestellbar unter „Privatsphäre“; in der Plattform bleibt der Hinweis
            link = _goal_link()
            send_mail(p.user.email, "Dein 90-Tage-Check-in", "goal_checkin", user=p.user, body=body, link=link)
            if p.user.telegram_user_id and tg_enabled():
                tg_send(p.user.telegram_user_id, f"{body}\n{link}")
        sent += 1
    return sent
