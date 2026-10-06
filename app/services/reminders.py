"""Erinnerung vor Terminen: eine E-Mail (und Telegram) am Vortag an alle Angemeldeten, mit einer kurzen Vorbereitung.

Läuft im Hintergrund (`flask event-reminders`, stündlich); je Person und Termin genau einmal (Notification-Zeile als Marke).
Die Vorbereitung nennt bis zu drei andere Angemeldete, die zu dir passen — nur Personen mit Matching-Einwilligung.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from ..extensions import db
from ..models import Event, Notification, Profile, Registration, User, utcnow
from . import club as club_settings, matching
from .mailer import send_mail

log = logging.getLogger(__name__)
KIND = "event_reminder"
LEAD_HOURS = 26  # alle Termine, die in den nächsten ~Tag beginnen


def _briefing(me: Profile, event: Event) -> list[str]:
    """Bis zu drei passende Mitangemeldete: „Vorname L. — Rolle“."""
    if not (me.allow_matching and me.embed_need):
        return []
    ids = [r.user_id for r in event.registrations if r.status in ("registered", "paid", "reserved")
           and r.user_id != me.user_id]
    if not ids:
        return []
    pool = [p for p in matching._pool(user_ids=ids) if not matching.blocked(me, p)]
    ranked = sorted(pool, key=lambda p: -matching.pair_score(me, p))[:3]
    return [f"{p.user.first_name} {p.user.last_name[:1]}. — {p.headline}".rstrip(" —") for p in ranked]


def send_event_reminders(lead_hours: int = LEAD_HOURS) -> int:
    """Erinnerungen im aktuellen Klub verschicken. Gibt die Zahl verschickter Erinnerungen zurück."""
    from .telegram import enabled as tg_enabled, send as tg_send
    now = utcnow()
    events = (Event.query.filter(Event.status == "published", Event.starts_at > now,
                                 Event.starts_at <= now + timedelta(hours=lead_hours)).all())
    sent = 0
    for ev in events:
        regs = Registration.query.filter(Registration.event_id == ev.id,
                                         Registration.status.in_(("registered", "paid", "reserved"))).all()
        for reg in regs:
            user = reg.user
            if user.status != "active" or Notification.query.filter_by(user_id=user.id, event_id=ev.id,
                                                                       kind=KIND).first():
                continue
            title = f"Morgen: {ev.title}" if (ev.starts_at - now) > timedelta(hours=8) else f"Gleich: {ev.title}"
            db.session.add(Notification(user_id=user.id, event_id=ev.id, kind=KIND, title=title,
                                        body="Dein Termin steht an. Schau, wen du treffen solltest."))
            db.session.commit()  # Marke zuerst: bei einem Fehler beim Versand gibt es keine doppelte Mail
            if not user.profile or user.profile.event_reminders:
                try:
                    people = _briefing(user.profile, ev) if user.profile else []
                except Exception:  # pragma: no cover - die Erinnerung darf nie an der Vorbereitung scheitern
                    log.exception("Vorbereitung für Erinnerung fehlgeschlagen")
                    people = []
                base = club_settings.base_url()  # läuft ohne Request (Hintergrundjob): Adresse des Klubs
                link = f"{base}/app/termine/{ev.id}"
                send_mail(user.email, title, "event_reminder", user=user, ev=ev, people=people, link=link,
                          ics=f"{link}/ics", reg=reg)
                if user.telegram_user_id and tg_enabled():
                    tg_send(user.telegram_user_id, f"{title}\n{link}")
            sent += 1
    return sent
