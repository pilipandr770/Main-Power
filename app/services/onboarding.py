"""Startcheckliste für die Klubleitung (Dashboard): was für den Betrieb noch fehlt."""
from __future__ import annotations

from flask import url_for

from ..models import Event, Invite, User
from . import club as club_settings


def steps() -> list[dict]:
    """Je Schritt: label, hint, done, url. Verschwindet im Dashboard, sobald alle erledigt sind."""
    c = club_settings.settings()
    d = club_settings.defaults()
    club_page = url_for("admin.club_page")
    # Beispieltermine aus dem Seed haben keine Erstellerin; eigene, importierte oder Mitglieder-Termine zählen
    own_events = Event.query.filter(Event.created_by_id.isnot(None) | (Event.source == "sync")).count()
    return [
        {"label": "Name und Auftritt festlegen", "hint": "Name, Slogan, Farbe, Logo und Texte der Startseite.",
         "done": c["name"] != d["name"] or bool(c["logo"]), "url": club_page},
        {"label": "KI-Assistent anpassen", "hint": "Name, Persönlichkeit und Wissen deines Chatbots.",
         "done": c["about"] != d["about"] or c["assistant_name"] != d["assistant_name"] or bool(c["assistant_facts"]),
         "url": url_for("admin.assistant")},
        {"label": "Impressum eintragen", "hint": "Pflicht für den öffentlichen Betrieb (§ 5 DDG).",
         "done": bool(c["impressum_url"]) or c["impressum_text"] != d["impressum_text"], "url": club_page},
        {"label": "Betreiber und Kontakt angeben", "hint": "Erscheint in Datenschutz und Nutzungsbedingungen.",
         "done": c["operator"] != d["operator"] and c["contact_email"] != d["contact_email"], "url": club_page},
        {"label": "Registrierung festlegen", "hint": "Offen, mit Freigabe oder nur mit Einladung.",
         "done": c["registration"] != "open" or Invite.query.count() > 0, "url": club_page},
        {"label": "Erste Mitglieder einladen", "hint": "Persönliche Links per E-Mail oder ein Gemeinschaftslink.",
         "done": Invite.query.count() > 0 or User.query.filter_by(status="active").count() > 3,
         "url": url_for("admin.invites")},
        {"label": "Ersten Termin anlegen", "hint": "Damit Mitglieder sich begegnen können.",
         "done": own_events > 0, "url": url_for("admin.event_edit")},
    ]
