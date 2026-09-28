"""Vorlagen für Klubs: Branding-Texte, Veranstaltungsformate und FAQ auf einen Klick.

- "mainpower": der bisherige Stand (Main Power Community, Frankfurt) — Einstellungen = Standardwerte aus services/club.py.
- "neutral":   ein neutraler Business-Klub mit eigenen, lizenzfreien Bildern als Ausgangspunkt für neue Kunden.
Neue Vorlagen: weiteren Eintrag in PRESETS anlegen.
"""
from __future__ import annotations

from .extensions import db
from .models import DEFAULT_FORMATS, KnowledgeItem, MeetingFormat, Setting, invalidate_formats
from .services import club as club_settings


def _mainpower_formats() -> list[dict]:
    out = []
    for i, (key, f) in enumerate(DEFAULT_FORMATS.items()):
        out.append(dict(key=key, name=f["name"], short=f["short"], tagline=f["tagline"], image=f["image"],
                        price_cents=f["price_cents"], rhythm=f["rhythm"], description=f["description"],
                        price_note=("davon 18 € für dein Frühstück im Hotel, 7 € Organisationsbeitrag" if key == "hub" else ""),
                        details=("Jede:r stellt sich kurz vor: wer sie ist, wo ihre größte Herausforderung liegt, wobei sie "
                                 "anderen weiterhelfen kann und wonach sie gerade konkret sucht. Unser Ziel: Niemand verlässt "
                                 "ein Hub-Treffen ohne mindestens eine wirklich wertvolle Verbindung." if key == "hub" else ""),
                        featured=key != "community", system=key == "community", sort=i))
    return out


def _mainpower_faq() -> list[tuple[str, str, bool]]:
    from .seed import FAQ
    return FAQ


COMMUNITY_FORMAT = dict(key="community", name="Community-Treffen", short="Community", tagline="Von Mitgliedern organisiert",
                        image="img/neutral/format-themenabend.png", price_cents=0, rhythm="Individuell",
                        description="Treffen, die Mitglieder selbst initiieren — thematisch, klein, konkret.",
                        price_note="", details="", featured=False, system=True, sort=99)

NEUTRAL = {
    "label": "Neutraler Business-Klub (Demo)",
    "settings": {
        "name": "Business-Klub", "full_name": "Business-Klub Demo", "subtitle": "Demo-Konfiguration",
        "tagline": "Gemeinsam weiterkommen.", "city": "Musterstadt",
        "operator": "Business-Klub Demo — Betreiber bitte in den Klub-Einstellungen eintragen",
        "contact_email": "kontakt@example.org", "main_site_url": "", "impressum_url": "", "privacy_main_url": "",
        "instagram_url": "", "accent": "#14b8a6", "assistant_name": "Aiko",
        "hero_title": "Die richtigen Menschen. Zur richtigen Zeit.",
        "hero_lead": "Unser Klub bringt Unternehmer:innen, Selbstständige und Fachleute zusammen. Du erzählst, wo du stehst und "
                     "was du suchst — die KI schlägt dir Menschen vor, die dir wirklich weiterhelfen. Schon vor dem ersten Treffen.",
        "meta_description": "Business-Klub mit KI-Matching: relevante Kontakte für Unternehmer:innen und Fachleute.",
        "about": "Der Business-Klub verbindet Unternehmer:innen, Selbstständige und Fachleute in regelmäßigen, kuratierten "
                 "Treffen. Ziel: wenige, dafür relevante Kontakte, die sich gegenseitig weiterbringen.",
        "assistant_facts": "",
        "steps_meet_text": "Beim Business-Frühstück, im Themenabend, beim Walk & Talk oder online — mit dem Wissen, wen du "
                           "treffen solltest.",
        "formats_title": "Vier Formate für echte Begegnungen.",
        "formats_intro": "Vom Frühstück bis zur Online-Runde: Wähle, was zu deinem Kalender passt.",
        "values_label": "Ich akzeptiere die Werte des Klubs und bringe mich respektvoll und konstruktiv ein.",
        "events_sync_url": "", "events_sync_label": "",
        "hero_images": [{"src": "img/neutral/hero-1.jpg", "alt": "Abstraktes Netz aus Verbindungen"},
                        {"src": "img/neutral/hero-2.jpg", "alt": ""}, {"src": "img/neutral/hero-3.jpg", "alt": ""}],
        "band_images": [{"src": f"img/neutral/band-{i}.jpg", "alt": ""} for i in (1, 2, 3)],
        "testimonials": [],  # keine erfundenen Stimmen — echte Zitate trägt der Klub selbst ein
        "logo": "",
    },
    "formats": [
        dict(key="fruehstueck", name="Business-Frühstück", short="Frühstück", tagline="Relevante Kontakte",
             image="img/neutral/format-fruehstueck.png", price_cents=2000, rhythm="Einmal im Monat",
             description="Kuratiertes Frühstück mit kurzer Vorstellungsrunde und KI-gestütztem Matching vorab.",
             price_note="inklusive Frühstück", details="", featured=True, system=False, sort=0),
        dict(key="themenabend", name="Themenabend", short="Themenabend", tagline="Wissen teilen",
             image="img/neutral/format-themenabend.png", price_cents=0, rhythm="Alle zwei Monate",
             description="Ein Mitglied gibt Einblick in sein Fachgebiet, danach offene Diskussion.",
             price_note="", details="", featured=True, system=False, sort=1),
        dict(key="walk", name="Walk & Talk", short="Walk & Talk", tagline="Bewegung und Gespräch",
             image="img/neutral/format-walk.png", price_cents=0, rhythm="Einmal im Monat, samstags",
             description="Gemeinsam gehen, zu zweit reden — die entspannteste Form des Netzwerkens.",
             price_note="", details="", featured=True, system=False, sort=2),
        dict(key="online", name="Online-Runde", short="Online", tagline="Von überall",
             image="img/neutral/format-online.png", price_cents=0, rhythm="Einmal im Monat, abends",
             description="Kurze Videorunde mit Vorstellung und gezielten Breakout-Gesprächen.",
             price_note="", details="", featured=True, system=False, sort=3),
        COMMUNITY_FORMAT,
    ],
    "faq": [
        ("Wie werde ich Mitglied?", "Registriere dich kostenlos und beantworte in deinem Profil vier Fragen. Danach "
         "schlägt dir die KI passende Menschen vor.", True),
        ("Was kostet die Teilnahme?", "Die Mitgliedschaft auf der Plattform ist kostenlos. Einzelne Formate können einen "
         "Kostenbeitrag haben; er steht jeweils beim Termin.", True),
        ("Wie funktioniert das Matching?", "Du beschreibst, was du machst, wo es hakt, womit du helfen kannst und wonach du "
         "suchst. Die KI vergleicht Bedarf und Angebot inhaltlich und schlägt dir Menschen vor — freiwillig und jederzeit "
         "abschaltbar.", True),
        ("Wer sieht meine Kontaktdaten?", "Niemand ohne deine Zustimmung. Erst wenn du eine Kontaktanfrage annimmst, sehen "
         "beide Seiten E-Mail, Telefon und Profil-Links.", True),
        ("Muss ich beim Treffen etwas erzählen?", "Nein. Erzählen, zuhören, einfach dabei sein — alles ist willkommen.", True),
        ("Kann ich selbst ein Treffen organisieren?", "Ja. Unter „Termine“ kannst du ein Community-Treffen vorschlagen; nach "
         "kurzer Prüfung erscheint es im Kalender.", False),
    ],
}

PRESETS = {
    "mainpower": {"label": "Main Power (Frankfurt)", "settings": None, "formats": None, "faq": None},
    "neutral": NEUTRAL,
}


def preset_formats(name: str) -> list[dict]:
    p = PRESETS[name]
    return p["formats"] if p["formats"] is not None else _mainpower_formats()


def preset_faq(name: str) -> list[tuple[str, str, bool]]:
    p = PRESETS[name]
    return p["faq"] if p["faq"] is not None else _mainpower_faq()


def apply_formats(formats: list[dict]) -> None:
    """Formate des aktuellen Klubs auf die Liste setzen; nicht enthaltene werden deaktiviert (Termine bleiben gültig)."""
    existing = {f.key: f for f in MeetingFormat.query.all()}
    keys = set()
    for spec in formats:
        f = existing.get(spec["key"]) or MeetingFormat(key=spec["key"])
        for k, v in spec.items():
            setattr(f, k, v)
        f.active = True
        db.session.add(f)
        keys.add(spec["key"])
    for key, f in existing.items():
        if key not in keys and not f.system:
            f.active = False
    if "community" not in keys and "community" not in existing:
        db.session.add(MeetingFormat(**COMMUNITY_FORMAT))
    invalidate_formats()


def apply_faq(items: list[tuple[str, str, bool]]) -> None:
    KnowledgeItem.query.delete()
    for i, (q, a, public) in enumerate(items):
        db.session.add(KnowledgeItem(question=q, answer=a, public=public, sort=i))


def apply_preset(name: str, replace_faq: bool = False) -> None:
    """Vorlage auf den aktuellen Klub anwenden. Aufrufer committet."""
    if name not in PRESETS:
        raise KeyError(name)
    Setting.query.filter(Setting.key.like(club_settings.PREFIX + "%")).delete(synchronize_session=False)
    club_settings.invalidate()
    if PRESETS[name]["settings"]:
        club_settings.save(PRESETS[name]["settings"])
    apply_formats(preset_formats(name))
    if replace_faq or KnowledgeItem.query.count() == 0:
        apply_faq(preset_faq(name))


def export_config() -> dict:
    """Branding, Formate und FAQ des aktuellen Klubs als JSON (zum Übertragen auf einen anderen Klub)."""
    return {"version": 1, "settings": club_settings.export(),
            "formats": [f.as_dict() | {"sort": f.sort} for f in MeetingFormat.query.order_by(MeetingFormat.sort)],
            "faq": [(k.question, k.answer, k.public) for k in KnowledgeItem.query.order_by(KnowledgeItem.sort)]}


FORMAT_FIELDS = {"key", "name", "short", "tagline", "image", "price_cents", "price_note", "rhythm", "description",
                 "details", "featured", "system", "sort"}


def import_config(data: dict, with_faq: bool = True) -> None:
    """Gegenstück zu export_config. Hochgeladene Bilder anderer Klubs werden nicht übernommen (Verweise entfallen)."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Unbekanntes Format der Konfigurationsdatei.")
    s = dict(data.get("settings") or {})
    for key in ("hero_images", "band_images"):
        s[key] = [i for i in (s.get(key) or []) if isinstance(i, dict) and not str(i.get("src", "")).startswith("upload:")]
    if str(s.get("logo", "")).startswith("upload:"):
        s["logo"] = ""
    Setting.query.filter(Setting.key.like(club_settings.PREFIX + "%")).delete(synchronize_session=False)
    club_settings.save(s)
    formats = []
    for f in data.get("formats") or []:
        spec = {k: v for k, v in f.items() if k in FORMAT_FIELDS}
        if not spec.get("key") or not spec.get("name"):
            continue
        if str(spec.get("image", "")).startswith("upload:"):
            spec["image"] = ""
        formats.append(spec)
    if formats:
        apply_formats(formats)
    if with_faq and data.get("faq"):
        apply_faq([(str(q)[:300], str(a), bool(p)) for q, a, p in data["faq"]])
