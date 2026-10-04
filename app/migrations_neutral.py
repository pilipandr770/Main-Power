"""Einmalige, idempotente Bereinigung älterer Installationen von Inhalten des früheren Demo-Kunden.

Frühere Versionen lieferten Texte, Formate (Hub, Stammtisch, Laufen, Frauenkreis), Termine aus dem Kalender der Website
und einen Admin-Namen mit. Die Plattform ist jetzt neutral („Klub“). `flask init-db` ruft diese Funktion auf; sie fasst nur
unveränderte Standardinhalte an. Slug und Datenbankname bleiben unverändert.
"""
from __future__ import annotations

import logging

from .extensions import db

log = logging.getLogger(__name__)

LEGACY_FORMAT_KEYS = {"hub": "fruehstueck", "stammtisch": "themenabend", "laufen": "walk", "frauenkreis": "online"}
LEGACY_IMAGES = ("img/format-hub.png", "img/format-stammtisch.png", "img/format-laufen.png", "img/format-frauenkreis.png")
OLD_DEMO_DOMAIN, NEW_DEMO_DOMAIN = "demo.main-power.local", "demo.klub.local"
MARKERS = ("main power", "main-power", "mainpower", "beissenov")
TEXT_FIXES = {  # Demo-Profiltexte mit Ortsbezug des früheren Kunden
    "Gastronomie in Frankfurt, Catering": "Gastronomie in der Region, Catering",
    "Gewerbeflächen und Praxisräumen in Frankfurt": "Gewerbeflächen und Praxisräumen in der Stadt",
    "Marktüberblick zu Mieten in Frankfurt": "Marktüberblick zu Mieten vor Ort",
    "Teil der Main Power Community": "Teil von Klub",
    "Main Power Community": "Klub",
}


def _has_marker(text: str | None) -> bool:
    low = (text or "").lower()
    return any(m in low for m in MARKERS)


def neutralize_legacy_branding() -> list[str]:
    from .models import Club
    from .tenancy import use_club
    log_lines: list[str] = []
    for club in Club.query.execution_options(all_clubs=True).order_by(Club.id).all():
        with use_club(club):
            _neutralize_club(club, log_lines)
            db.session.commit()  # vor dem Verlassen des Klubs: use_club verwirft nicht gespeicherte Objekte
    return log_lines


def _neutralize_club(club, out: list[str]) -> None:
    from .club_presets import _default_faq, apply_faq, apply_formats, preset_formats
    from .models import Event, KnowledgeItem, MeetingFormat, Profile, Setting, User, invalidate_formats
    from .services import club as club_settings

    def note(msg: str) -> None:
        out.append(f"{club.slug}: {msg}")

    # 1. Gespeicherte Branding-Werte mit Altinhalten verwerfen -> neutrale Standardwerte greifen
    dropped = []
    for row in Setting.query.filter(Setting.key.like(club_settings.PREFIX + "%")).all():
        key = row.key[len(club_settings.PREFIX):]
        value = row.value or ""
        stale = _has_marker(value) or "img/community-" in value or "img/neutral/hero-" in value \
            or "img/neutral/band-" in value or (key == "testimonials" and "Roland" in value)
        if stale and key in club_settings.TEXT_FIELDS | club_settings.JSON_FIELDS:
            db.session.delete(row)
            dropped.append(key)
    if dropped:
        club_settings.invalidate()
        note("alte Branding-Werte entfernt: " + ", ".join(sorted(dropped)))

    # 2. Klubdaten
    if _has_marker(club.name) and club.name.strip().lower() in ("main power", "main power community"):
        club.name = "Klub"
        note("Anzeigename auf „Klub“ gesetzt (Slug bleibt)")
    if _has_marker(club.contact_email):
        club.contact_email = "kontakt@example.org"

    # 3. Formate, Termine
    formats = MeetingFormat.query.all()
    legacy = [f for f in formats if f.image in LEGACY_IMAGES or (f.key in LEGACY_FORMAT_KEYS and _has_marker(f.name))]
    if legacy:
        url = club_settings.settings().get("events_sync_url") or ""
        source_gone = not url or _has_marker(url)  # Kalenderquelle des früheren Kunden: nichts mehr nachzuladen
        n = 0
        for ev in Event.query.filter_by(source="sync").all():
            if source_gone or _has_marker(ev.external_id) or _has_marker(ev.title):
                db.session.delete(ev)
                n += 1
        if n:
            note(f"{n} importierte Termine der früheren Kalenderquelle entfernt")
        for ev in Event.query.filter(Event.format.in_(list(LEGACY_FORMAT_KEYS))).all():
            ev.format = "community"
        for p in Profile.query.filter(Profile.preferred_formats != "").all():
            keys = [LEGACY_FORMAT_KEYS.get(k, k) for k in p.preferred_formats.split(",") if k]
            p.preferred_formats = ",".join(dict.fromkeys(keys))
        db.session.flush()
        apply_formats(preset_formats("neutral"))
        for f in legacy:
            if f.key in LEGACY_FORMAT_KEYS:
                db.session.delete(f)
        invalidate_formats()
        note("Formate auf die neutrale Vorlage gesetzt")
        from .seed import _seed_events
        _seed_events()

    # 4. FAQ mit Altinhalten ersetzen
    items = KnowledgeItem.query.all()
    if any(_has_marker(k.question + " " + k.answer) for k in items):
        apply_faq(_default_faq())
        note("FAQ auf die neutrale Vorlage gesetzt")

    # 5. Personen und Profile
    for u in User.query.filter(User.email.like(f"%@{OLD_DEMO_DOMAIN}")).all():
        new = u.email.replace(OLD_DEMO_DOMAIN, NEW_DEMO_DOMAIN)
        if not User.query.filter_by(email=new).first():
            u.email = new
    for u in User.query.filter_by(role="superadmin").all():
        if (u.first_name, u.last_name) == ("Asset", "Beissenov"):
            u.first_name, u.last_name = "Admin", ""
            note("Superadmin-Name neutralisiert")
            if u.profile:
                u.profile.headline, u.profile.company = "Klubleitung", ""
                u.profile.q_focus = "Aufbau und Betreuung des Klubs"
                u.profile.q_can_help = "Kontakte innerhalb des Klubs, Moderation, Community-Aufbau"
                u.profile.q_challenge = "Den Klub mit den richtigen Mitgliedern wachsen lassen"
    for ev in Event.query.filter_by(location="Frankfurt Westend").all():
        ev.location = "Musterstadt, Innenstadt"
    for p in Profile.query.all():
        if p.city == "Frankfurt am Main" and p.region == "rhein-main":  # frühere Spaltenvorgabe, keine eigene Angabe
            p.city, p.region = "", ""
        for col in ("bio", "q_focus", "q_can_help", "q_looking_for", "company"):
            val = getattr(p, col) or ""
            for old, new in TEXT_FIXES.items():
                if old in val:
                    val = val.replace(old, new)
            if val != (getattr(p, col) or ""):
                setattr(p, col, val)
