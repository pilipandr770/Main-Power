"""Branding und Inhalte je Klub (White-Label).

Alles, was einen Klub von einem anderen unterscheidet — Name, Farben, Logo, Texte der Startseite, Bilder, Stimmen,
Rechtliches, Persona der KI-Assistenz, Termin-Import — liegt als Einstellung `club.<schlüssel>` in der
(klubbezogenen) Tabelle `settings`. Fehlende Werte fallen auf DEFAULTS zurück (= bisheriger Stand von Main Power),
damit eine bestehende Installation nach dem Update unverändert aussieht.
"""
from __future__ import annotations

import json
import re

from flask import current_app, g, has_app_context, url_for

from ..extensions import db

PREFIX = "club."

# Einfache Textfelder: key -> (Label im Admin, Standardwert, max. Länge, mehrzeilig)
TEXT_FIELDS: dict[str, tuple[str, str, int, bool]] = {
    "name": ("Name (kurz)", "Main Power", 60, False),
    "full_name": ("Voller Name", "Main Power Community", 120, False),
    "subtitle": ("Zusatz neben dem Logo", "Community Frankfurt", 60, False),
    "tagline": ("Slogan", "Geladen bleiben.", 80, False),
    "city": ("Stadt / Region", "Frankfurt am Main", 80, False),
    "operator": ("Betreiber (für Datenschutz und Bedingungen)", "Main Power Community, Asset Beissenov, Frankfurt am Main",
                 300, False),
    "contact_email": ("Kontakt-E-Mail", "hallo@main-power.org", 120, False),
    "main_site_url": ("Hauptwebsite des Klubs (optional)", "https://www.main-power.org", 200, False),
    "impressum_url": ("Impressum-Link", "https://www.main-power.org/impressum", 200, False),
    "privacy_main_url": ("Datenschutzerklärung der Hauptwebsite (optional)", "https://www.main-power.org/datenschutz", 200, False),
    "instagram_url": ("Instagram (optional)", "https://instagram.com/mainpowercommunity", 200, False),
    "accent": ("Akzentfarbe (Hex)", "#fe4716", 7, False),
    "assistant_name": ("Name der KI-Assistenz", "Aiko", 30, False),
    "hero_title": ("Startseite: Überschrift", "Nicht mehr Kontakte. Relevantere Kontakte.", 120, False),
    "hero_lead": ("Startseite: Einleitung",
                  "Die Plattform der Main Power Community in Frankfurt. Du erzählst uns, wo du stehst und was du suchst — wir "
                  "bringen dich mit den Menschen zusammen, die dir wirklich weiterhelfen. Schon vor dem ersten Treffen.", 500, True),
    "meta_description": ("Beschreibung für Suchmaschinen",
                         "Menschen. Energie. Echte Verbindungen. Die Plattform der Main Power Community Frankfurt.", 200, False),
    "about": ("Wofür steht der Klub? (für die KI-Assistenz)",
              "Main Power bringt Unternehmer:innen, Expert:innen und Freelancer in kuratierten Offline-Formaten zusammen. "
              "Leitgedanke: „Nicht mehr Kontakte. Relevantere Kontakte.“ Markenversprechen: „Geladen bleiben.“", 800, True),
    "assistant_facts": ("Feste Fakten für die KI-Assistenz (z. B. Preisaufschlüsselung)",
                        "Hub-Preis: 25 € = 18 € Frühstück im Hotel + 7 € Organisationsbeitrag.", 1500, True),
    "steps_meet_text": ("Startseite: Schritt „Begegnung“",
                        "Beim Hub-Frühstück, Stammtisch, Laufen oder im Frauenkreis — mit dem Wissen, wen du treffen solltest.",
                        300, True),
    "formats_title": ("Startseite: Überschrift Formate", "Vier Wege, sich wirklich zu begegnen.", 120, False),
    "formats_intro": ("Startseite: Text Formate",
                      "Vier Formate, eine Community. Wähle, was gerade zu deinem Leben passt — oder lerne nach und nach "
                      "alle Seiten von Main Power kennen.", 400, True),
    "values_label": ("Registrierung: Werte-Bestätigung",
                     "Ich akzeptiere die Werte und Struktur von Main Power und werde mich respektvoll und konstruktiv in die "
                     "Gemeinschaft einbringen.", 300, True),
    "events_sync_url": ("Termin-Import: JSON-Adresse (optional)", "https://www.main-power.org/api/events", 300, False),
    "events_sync_label": ("Termin-Import: Name der Quelle", "main-power.org", 60, False),
}

# Bilder: Liste aus {"src": "img/…" | "upload:<datei>", "alt": "…"}
DEFAULT_HERO = [
    {"src": "img/community-conversation.jpg", "alt": "Zwei Mitglieder im Gespräch bei einem Main-Power-Abend"},
    {"src": "img/community-hero.jpg", "alt": "Main-Power-Frühstück auf einer Hotelterrasse in Frankfurt"},
    {"src": "img/community-voice.jpg", "alt": "Eine Teilnehmerin teilt ihre Gedanken in der Runde"},
]
DEFAULT_BAND = [
    {"src": "img/community-table.jpg", "alt": "Main-Power-Stammtisch in warmer Restaurantatmosphäre"},
    {"src": "img/community-moment.jpg", "alt": "Offenes Gespräch bei einem Main-Power-Abend"},
    {"src": "img/community-voice.jpg", "alt": "Eine Teilnehmerin spricht in der Gruppe"},
]
DEFAULT_TESTIMONIALS = [
    {"name": "Roland", "text": "Die Idee mit den Fragekarten war klasse – sie haben alle vier Energien sehr treffend adressiert "
                               "und den Einstieg in tiefere Gespräche enorm erleichtert."},
    {"name": "Sabine", "text": "Durch die Möglichkeit Fragen zu stellen entstand ein offener ehrlicher Austausch, abseits von "
                               "Small Talk."},
    {"name": "Sini", "text": "Ich konnte wirklich einiges mitnehmen und bin echt beflügelt!!"},
    {"name": "Brian", "text": "Danke für das Organisieren des coolen Abends. Das mit den Karten im Brief fand ich spaßig :)"},
]
JSON_FIELDS = {"hero_images": DEFAULT_HERO, "band_images": DEFAULT_BAND, "testimonials": DEFAULT_TESTIMONIALS}
OTHER_FIELDS = {"logo": ""}  # "upload:<datei>" oder leer (= Standard-Logo)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


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
