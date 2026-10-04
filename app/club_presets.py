"""Vorlage für Klubs: Branding-Texte, Veranstaltungsformate und FAQ auf einen Klick.

Es gibt eine neutrale Vorlage („Klub“) — Ausgangspunkt für jeden neuen Klub. Name, Texte, Logo, Bilder, Impressum und
Formate passt der Klub anschließend in seinen Einstellungen an; die Funktionen für Mitglieder sind für alle gleich.
Weitere Vorlagen: Eintrag in PRESETS anlegen.
"""
from __future__ import annotations

from .extensions import db
from .models import DEFAULT_FORMATS, ClubQuestion, KnowledgeItem, MeetingFormat, Setting, invalidate_formats
from .services import club as club_settings

# Zusatztexte der Standardformate (Preishinweis, Details) — die Grunddaten stehen in models.DEFAULT_FORMATS
_FORMAT_EXTRAS = {"fruehstueck": {"price_note": "inklusive Frühstück"}}


def _default_formats() -> list[dict]:
    out = []
    for i, (key, f) in enumerate(DEFAULT_FORMATS.items()):
        out.append(dict(key=key, name=f["name"], short=f["short"], tagline=f["tagline"], image=f["image"],
                        price_cents=f["price_cents"], rhythm=f["rhythm"], description=f["description"],
                        price_note="", details="", featured=key != "community", system=key == "community",
                        sort=99 if key == "community" else i) | _FORMAT_EXTRAS.get(key, {}))
    return out


def _default_faq() -> list[tuple[str, str, bool]]:
    from .seed import FAQ
    return FAQ


PRESETS = {
    "neutral": {"label": "Klub (neutrale Vorlage)", "settings": None, "formats": None, "faq": None},
}


def preset_formats(name: str) -> list[dict]:
    p = PRESETS[name]
    return p["formats"] if p["formats"] is not None else _default_formats()


def preset_faq(name: str) -> list[tuple[str, str, bool]]:
    p = PRESETS[name]
    return p["faq"] if p["faq"] is not None else _default_faq()


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
        db.session.add(MeetingFormat(**next(f for f in _default_formats() if f["key"] == "community")))
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
            "faq": [(k.question, k.answer, k.public) for k in KnowledgeItem.query.order_by(KnowledgeItem.sort)],
            "questions": [q.as_dict() for q in ClubQuestion.query.order_by(ClubQuestion.sort, ClubQuestion.id)]}


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
    for spec in data.get("questions") or []:
        if not isinstance(spec, dict) or not spec.get("key") or not spec.get("label"):
            continue
        q = ClubQuestion.query.filter_by(key=str(spec["key"])[:40]).first() or ClubQuestion(key=str(spec["key"])[:40])
        q.label, q.help = str(spec["label"])[:200], str(spec.get("help", ""))[:300]
        q.kind = spec.get("kind") if spec.get("kind") in ClubQuestion.KINDS else "text"
        q.options = [str(o)[:80] for o in (spec.get("options") or [])][:20] or None
        q.use = spec.get("use") if spec.get("use") in ClubQuestion.USES else "none"
        q.public, q.active = bool(spec.get("public", True)), bool(spec.get("active", True))
        q.sort = int(spec.get("sort") or 0)
        db.session.add(q)
    if with_faq and data.get("faq"):
        apply_faq([(str(q)[:300], str(a), bool(p)) for q, a, p in data["faq"]])
