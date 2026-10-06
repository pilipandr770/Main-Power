"""Branding und Inhalte je Klub (White-Label).

Alles, was einen Klub von einem anderen unterscheidet — Name, Farben, Logo, Texte der Startseite, Bilder, Stimmen,
Rechtliches, Persona der KI-Assistenz, Termin-Import — liegt als Einstellung `club.<schlüssel>` in der
(klubbezogenen) Tabelle `settings`. Fehlende Werte fallen auf DEFAULTS zurück (= neutraler „Klub“). Jeder Klub überschreibt sie
in den Klub-Einstellungen mit eigenem Namen, Texten, Logo und Bildern.
"""
from __future__ import annotations

import json
import re

from flask import current_app, g, has_app_context, url_for

from ..extensions import db

PREFIX = "club."

# Platzhalter-Impressum: nur zur Ansicht in der Demo — jeder Klub trägt hier sein eigenes Impressum ein (§ 5 DDG).
IMPRESSUM_PLACEHOLDER = (
    "Angaben gemäß § 5 DDG\n\nMusterklub e. V.\nMusterstraße 1\n12345 Musterstadt\n\n"
    "Vertreten durch: Vorname Nachname\nKontakt: kontakt@example.org\nVereinsregister: Amtsgericht Musterstadt, VR 00000\n\n"
    "Hinweis: Dies ist ein Platzhalter. Der Klub ersetzt ihn in den Klub-Einstellungen durch sein eigenes Impressum.")

# Einfache Textfelder: key -> (Label im Admin, Standardwert, max. Länge, mehrzeilig)
TEXT_FIELDS: dict[str, tuple[str, str, int, bool]] = {
    "name": ("Name (kurz)", "Klub", 60, False),
    "full_name": ("Voller Name", "Klub", 120, False),
    "subtitle": ("Zusatz neben dem Logo", "Netzwerk", 60, False),
    "tagline": ("Slogan", "Gemeinsam weiterkommen.", 80, False),
    "city": ("Stadt / Region", "", 80, False),
    "operator": ("Betreiber (für Datenschutz und Bedingungen)",
                 "Betreiber des Klubs — bitte in den Klub-Einstellungen eintragen", 300, False),
    "contact_email": ("Kontakt-E-Mail", "kontakt@example.org", 120, False),
    "main_site_url": ("Hauptwebsite des Klubs (optional)", "", 200, False),
    "impressum_url": ("Impressum-Link (leer = eigener Impressum-Text unten)", "", 200, False),
    "impressum_text": ("Impressum-Text (wird unter /impressum angezeigt)", IMPRESSUM_PLACEHOLDER, 3000, True),
    "privacy_main_url": ("Datenschutzerklärung der Hauptwebsite (optional)", "", 200, False),
    "instagram_url": ("Instagram (optional)", "", 200, False),
    "accent": ("Akzentfarbe (Hex)", "#14b8a6", 7, False),
    "assistant_name": ("Name der KI-Assistenz", "Aiko", 30, False),
    "registration": ("Registrierung", "open", 10, False),
    "hero_title": ("Startseite: Überschrift", "Die richtigen Menschen. Zur richtigen Zeit.", 120, False),
    "hero_lead": ("Startseite: Einleitung",
                  "Unser Klub bringt Unternehmer:innen, Selbstständige und Fachleute zusammen. Du erzählst, wo du stehst und "
                  "was du suchst — die KI schlägt dir Menschen vor, die dir wirklich weiterhelfen. Schon vor dem ersten "
                  "Treffen.", 500, True),
    "meta_description": ("Beschreibung für Suchmaschinen",
                         "Klub mit KI-Matching: relevante Kontakte für Unternehmer:innen und Fachleute.", 200, False),
    "about": ("Wofür steht der Klub? (für die KI-Assistenz)",
              "Der Klub verbindet Unternehmer:innen, Selbstständige und Fachleute in regelmäßigen, kuratierten Treffen. "
              "Ziel: wenige, dafür relevante Kontakte, die sich gegenseitig weiterbringen.", 800, True),
    "assistant_facts": ("Feste Fakten für die KI-Assistenz (z. B. Preise, Orte, Regeln)", "", 1500, True),
    "steps_meet_text": ("Startseite: Schritt „Begegnung“",
                        "Beim Business-Frühstück, im Themenabend, beim Walk & Talk oder online — mit dem Wissen, wen du "
                        "treffen solltest.", 300, True),
    "formats_title": ("Startseite: Überschrift Formate", "Vier Formate für echte Begegnungen.", 120, False),
    "formats_intro": ("Startseite: Text Formate",
                      "Vom Frühstück bis zur Online-Runde: Wähle, was zu deinem Kalender passt.", 400, True),
    "values_label": ("Registrierung: Werte-Bestätigung",
                     "Ich akzeptiere die Werte des Klubs und bringe mich respektvoll und konstruktiv ein.", 300, True),
    "events_sync_url": ("Termin-Import: JSON-Adresse (optional)", "", 300, False),
    "events_sync_label": ("Termin-Import: Name der Quelle", "", 60, False),
}

# Bilder: Liste aus {"src": "img/…" | "upload:<datei>", "alt": "…"}
# Leer = die Startseite zeigt animierte 3D-Szenen (static/js/scenes.js); eigene Fotos ersetzen sie je Slot.
DEFAULT_HERO: list[dict] = []
DEFAULT_BAND: list[dict] = []
DEFAULT_TESTIMONIALS: list[dict] = []  # keine erfundenen Stimmen — echte Zitate trägt der Klub selbst ein
JSON_FIELDS = {"hero_images": DEFAULT_HERO, "band_images": DEFAULT_BAND, "testimonials": DEFAULT_TESTIMONIALS}
OTHER_FIELDS = {"logo": ""}  # "upload:<datei>" oder leer (= Standard-Logo)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# Wer darf sich registrieren? Standard: jede Person (open). approval = Konto wartet auf Freigabe durch die Klubleitung,
# invite = nur mit Einladungslink. Eine gültige Einladung überspringt die Freigabe.
REG_MODES = {"open": "Offen: jede Person kann sich registrieren",
             "approval": "Mit Freigabe: neue Konten prüft die Klubleitung",
             "invite": "Nur mit Einladung: Registrierung nur über einen Einladungslink"}


class ClubView(dict):
    """dict mit Attributzugriff für Templates: club.name, club.hero_images …"""

    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc


def _env_defaults() -> dict:
    """Werte aus der .env überschreiben die Voreinstellungen (Abwärtskompatibilität)."""
    cfg = current_app.config if has_app_context() else {}
    out = {}
    for key, cfg_key in (("contact_email", "CONTACT_EMAIL"), ("main_site_url", "MAIN_SITE_URL"),
                         ("impressum_url", "IMPRESSUM_URL"), ("events_sync_url", "EVENTS_SYNC_URL")):
        if cfg.get(cfg_key) is not None:
            out[key] = cfg.get(cfg_key) or ""
    return out


def defaults() -> dict:
    d = {k: v[1] for k, v in TEXT_FIELDS.items()}
    d.update(_env_defaults())
    d.update({k: [dict(x) for x in v] for k, v in JSON_FIELDS.items()})
    d.update(OTHER_FIELDS)
    return d


def settings() -> ClubView:
    """Einstellungen des aktuellen Klubs (pro Request gecacht)."""
    if has_app_context() and g.get("club_settings") is not None:
        return g.club_settings
    data = defaults()
    from ..tenancy import current_club_id
    if current_club_id() is not None:  # ohne Klub nie aus der DB lesen (sonst Zeilen aller Klubs gemischt)
        from ..models import Setting
        try:
            rows = Setting.query.filter(Setting.key.like(PREFIX + "%")).all()
        except Exception:
            db.session.rollback()
            rows = []
        for r in rows:
            key = r.key[len(PREFIX):]
            if key in JSON_FIELDS:
                try:
                    val = json.loads(r.value or "[]")
                    if isinstance(val, list):
                        data[key] = val
                except ValueError:
                    pass
            elif key in TEXT_FIELDS or key in OTHER_FIELDS:
                data[key] = r.value or ""
    if not HEX.match(data.get("accent") or ""):
        data["accent"] = TEXT_FIELDS["accent"][1]
    view = ClubView(data)
    if has_app_context():
        g.club_settings = view
    return view


def base_url(club=None) -> str:
    """Öffentliche Adresse eines Klubs (auch ohne Request, z. B. Cron): eigene Domain, sonst <slug>.<PLATFORM_DOMAIN>."""
    from ..tenancy import current_club, default_slug
    club = club or current_club()
    cfg = current_app.config
    if club and club.domain_list:
        return f"https://{club.domain_list[0]}"
    if club and cfg.get("PLATFORM_DOMAIN") and club.slug != default_slug():
        return f"https://{club.slug}.{cfg['PLATFORM_DOMAIN']}"
    return cfg["BASE_URL"].rstrip("/")


def invalidate() -> None:
    if has_app_context():
        g.pop("club_settings", None)


def save(values: dict) -> None:
    """Werte speichern (nur bekannte Schlüssel). Aufrufer committet."""
    from ..models import Setting
    from ..tenancy import current_club_id
    if current_club_id() is None:
        raise RuntimeError("Klub-Einstellungen nur im Kontext eines Klubs speichern")
    for key, val in values.items():
        if key in JSON_FIELDS:
            Setting.set(PREFIX + key, json.dumps(val, ensure_ascii=False))
        elif key in TEXT_FIELDS:
            limit = TEXT_FIELDS[key][2]
            Setting.set(PREFIX + key, str(val or "").strip()[:limit])
        elif key in OTHER_FIELDS:
            Setting.set(PREFIX + key, str(val or "")[:200])
    invalidate()


def export() -> dict:
    s = settings()
    return {k: s[k] for k in list(TEXT_FIELDS) + list(JSON_FIELDS) + list(OTHER_FIELDS)}


# --------------------------------------------------------------------------- Farben und Medien
def _mix(hex_color: str, target: int, amount: float) -> str:
    r, g_, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(c + (target - c) * amount)  # noqa: E731
    return "#{:02x}{:02x}{:02x}".format(mix(r), mix(g_), mix(b))


def css() -> str:
    """Klubspezifische CSS-Variablen (als eigene Datei ausgeliefert — CSP erlaubt keine Inline-Styles)."""
    accent = settings()["accent"]
    return (f":root {{ --ember: {accent}; --ember-soft: {_mix(accent, 255, 0.4)}; }}\n"
            f".logo svg, .bond svg, .brand-mark {{ color: {accent}; }}\n")


def media_url(ref: str) -> str:
    """"img/x.jpg" -> /static/img/x.jpg, "upload:abc.png" -> Klub-Medien-Route."""
    ref = ref or ""
    if ref.startswith("upload:"):
        return url_for("public.club_media", name=ref[len("upload:"):])
    if ref.startswith(("http://", "https://")):
        return ref
    return url_for("static", filename=ref or "img/favicon.svg")


def slug(text: str) -> str:
    text = (text or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:40] or "klub"
