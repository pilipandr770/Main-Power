"""Plattform-Startseite für Klub-Betreiber, Anfragen, Anlegen eines neuen Klubs und die Admin-Seiten der Klubleitung."""
import re

import pytest

from app.extensions import db
from app.models import Club, ClubRequest, Event, KnowledgeItem, Registration, User, utcnow
from app.tenancy import default_club, use_club
from tests.test_multiclub import Cfg, app, client, login  # noqa: F401  (Fixtures)

ROOT = "http://klubs.test"           # PLATFORM_DOMAIN der Testkonfiguration = Hauptdomain der Plattform
OP = {"email": Cfg.PLATFORM_ADMIN_EMAIL, "password": Cfg.PLATFORM_ADMIN_PASSWORD}


@pytest.fixture()
def mails(monkeypatch):
    sent = []

    def fake(to, subject, template, **ctx):
        sent.append({"to": to, "subject": subject, "template": template, **ctx})
        return True
    import app.blueprints.auth as auth_bp
    import app.services.mailer as mailer
    monkeypatch.setattr(mailer, "send_mail", fake)
    monkeypatch.setattr(auth_bp, "send_mail", fake)
    return sent


def _req(**kw):
    data = {"club_name": "Founders Hamburg", "contact_name": "Hanna Hansen", "email": "hanna@example.com",
            "city": "Hamburg", "size": "250", "message": "Wir wollen unser Netzwerk digitalisieren.",
            "consent_privacy": "1", "csrf_token": ""}
    data.update(kw)
    return data


def test_platform_host_shows_operator_landing_and_nothing_of_a_club(client):
    page = client.get("/", base_url=ROOT).get_data(as_text=True)
    assert "Klub-Plattform" in page and "Klub anfragen" in page and 'data-scene="sphere"' in page
    assert "Starter" in page and "Verband" in page                 # Tarife aus der Datenbank
    assert "Mitglied werden" not in page and "Die richtigen Menschen. Zur richtigen Zeit." not in page
    assert client.get("/impressum", base_url=ROOT).status_code == 200
    for path in ("/login", "/registrieren", "/app/", "/admin/", "/termine"):
        assert client.get(path, base_url=ROOT).status_code == 404   # keine Klub-Inhalte auf der Hauptdomain
    # auf einem Klub-Host bleibt „/“ der Klub; die Betreiber-Seite ist trotzdem unter /fuer-klubs erreichbar
    assert "Die richtigen Menschen" in client.get("/").get_data(as_text=True)
    assert "Klub anfragen" in client.get("/fuer-klubs").get_data(as_text=True)


def test_club_request_validation_honeypot_and_notifications(client, app, mails):
    r = client.post("/fuer-klubs/anfrage", base_url=ROOT, data={"club_name": "", "email": "kaputt"})
    page = r.get_data(as_text=True)
    assert r.status_code == 400 and "Wie heißt dein Klub?" in page and "gültige E-Mail" in page and "Verarbeitung" in page
    client.post("/fuer-klubs/anfrage", base_url=ROOT, data=_req(website="http://spam.example"))   # Honeypot
    with app.app_context():
        assert ClubRequest.query.count() == 0
    r = client.post("/fuer-klubs/anfrage", base_url=ROOT, data=_req(), follow_redirects=True)
    assert "Danke für deine Anfrage" in r.get_data(as_text=True) and "Anfrage senden" not in r.get_data(as_text=True)
    with app.app_context():
        row = ClubRequest.query.one()
        assert (row.club_name, row.status, row.size) == ("Founders Hamburg", "new", "250")
    to = {m["to"]: m for m in mails}
    assert Cfg.PLATFORM_ADMIN_EMAIL in to and "hanna@example.com" in to      # Betreiber und Anfragende werden informiert
    assert "Founders Hamburg" in to[Cfg.PLATFORM_ADMIN_EMAIL]["subject"]


def test_operator_creates_club_from_request_and_owner_sets_password(client, app, mails):
    client.post("/fuer-klubs/anfrage", base_url=ROOT, data=_req())
    assert client.get("/plattform/anfragen").status_code == 302                  # ohne Betreiber-Login gesperrt
    client.post("/plattform/login", data=OP)
    assert "Founders Hamburg" in client.get("/plattform/anfragen").get_data(as_text=True)
    with app.app_context():
        rid = ClubRequest.query.one().id
    form = client.get(f"/plattform/klub/neu?anfrage={rid}").get_data(as_text=True)    # Vorbelegung aus der Anfrage
    assert 'value="Founders Hamburg"' in form and 'value="hanna@example.com"' in form and 'value="founders-hamburg"' in form
    r = client.post(f"/plattform/klub/neu?anfrage={rid}", follow_redirects=True, data={
        "name": "Founders Hamburg", "slug": "founders-hamburg", "preset": "neutral", "admin_first": "Hanna",
        "admin_last": "Hansen", "admin_email": "hanna@example.com", "admin_password": "", "contact_email": "hanna@example.com"})
    text = r.get_data(as_text=True)
    assert "angelegt" in text and "founders-hamburg.klubs.test" in text
    with app.app_context():
        assert ClubRequest.query.one().status == "approved"
        club = Club.query.filter_by(slug="founders-hamburg").one()
        assert User.query.execution_options(all_clubs=True).filter_by(club_id=club.id, role="superadmin").count() == 1
    welcome = next(m for m in mails if m["template"] == "owner_welcome")
    assert welcome["to"] == "hanna@example.com" and welcome["link"].startswith("http://founders-hamburg.klubs.test") and "/passwort-neu/" in welcome["link"]
    # Die Klubleitung legt ihr Passwort selbst fest und meldet sich auf ihrer eigenen Adresse an
    token = welcome["link"].rsplit("/", 1)[1]
    host = "http://founders-hamburg.klubs.test"
    assert client.post(f"/passwort-neu/{token}", base_url=host, data={"password": "mein-eigenes-passwort"}).status_code == 302
    login(client, "hanna@example.com", "mein-eigenes-passwort", base=host)
    assert "Erste Schritte für" in client.get("/admin/", base_url=host).get_data(as_text=True)
    # Der Setup-Link ist einmalig
    assert client.post(f"/passwort-neu/{token}", base_url=host, data={"password": "noch-ein-passwort-1"}).status_code == 302
    client.post("/logout", base_url=host)
    r = client.post("/login", base_url=host, data={"email": "hanna@example.com", "password": "noch-ein-passwort-1"})
    assert "stimmen nicht" in r.get_data(as_text=True)          # das zweite Passwort wurde nicht gesetzt


def test_new_club_looks_neutral_and_isolated(client, app, mails):
    client.post("/plattform/login", data=OP)
    client.post("/plattform/klub/neu", data={"name": "Founders Hamburg", "slug": "founders-hamburg", "preset": "neutral",
                                              "admin_email": "hanna@example.com", "admin_password": "hamburg-passwort-1",
                                              "contact_email": "hanna@example.com"})
    host = "http://founders-hamburg.klubs.test"
    home = client.get("/", base_url=host).get_data(as_text=True)
    assert "Founders Hamburg" in home and 'data-scene="sphere"' in home and "Business-Frühstück" in home
    assert "Main Power" not in home and "Founders Berlin" not in home
    assert "Musterklub" in client.get("/impressum", base_url=host).get_data(as_text=True)     # Platzhalter bis zum Eintrag
    login(client, "hanna@example.com", "hamburg-passwort-1", base=host)
    for path in ("/admin/", "/admin/klub", "/admin/assistent", "/admin/zahlungen", "/admin/tarif", "/admin/einladungen",
                 "/admin/formate", "/admin/wissen", "/admin/mitglieder"):
        assert client.get(path, base_url=host).status_code == 200, path
    members = client.get("/admin/mitglieder", base_url=host).get_data(as_text=True)
    assert "hanna@example.com" in members and "chefin@berlin.example" not in members and "admin@test.local" not in members
    # Operator sieht die Adresse und kann den Klub pausieren
    dash = client.post("/plattform/login", data=OP) and client.get("/plattform/").get_data(as_text=True)
    assert "founders-hamburg.klubs.test" in dash


def test_operator_can_reject_request(client, app, mails):
    client.post("/fuer-klubs/anfrage", base_url=ROOT, data=_req())
    client.post("/plattform/login", data=OP)
    with app.app_context():
        rid = ClubRequest.query.one().id
    client.post(f"/plattform/anfragen/{rid}/ablehnen")
    with app.app_context():
        assert ClubRequest.query.one().status == "rejected"


def test_assistant_page_saves_settings_toggles_public_chat_and_probes(client, app):
    login(client, "chefin@berlin.example", "berlin-passwort-123", base="http://berlin.klubs.test")
    host = "http://berlin.klubs.test"
    page = client.get("/admin/assistent", base_url=host).get_data(as_text=True)
    assert "Probefrage" in page and "assistant_name" in page and "public_assistant" in page
    assert 'class="aiko-fab"' in client.get("/", base_url=host).get_data(as_text=True)
    client.post("/admin/assistent", base_url=host, data={"assistant_name": "Mira", "about": "Wir sind Berlins Gründerkreis.",
                                                         "assistant_facts": "Treffen: jeden ersten Dienstag.",
                                                         "aiko_extra_instructions": "Duze immer."})   # Chat-Häkchen fehlt = aus
    home = client.get("/", base_url=host).get_data(as_text=True)
    assert "aiko-fab" not in home                                                   # öffentlicher Chat ausgeschaltet
    assert client.post("/api/aiko", base_url=host, json={"history": [{"role": "user", "content": "Hallo"}]}).status_code == 404
    page = client.get("/admin/assistent", base_url=host).get_data(as_text=True)
    assert 'value="Mira"' in page and "jeden ersten Dienstag" in page and "Duze immer." in page
    # Klub „Standard“ ist unberührt
    assert "aiko-fab" in client.get("/").get_data(as_text=True)
    client.post("/admin/assistent", base_url=host, data={"assistant_name": "Mira", "about": "x", "public_assistant": "1",
                                                         "assistant_facts": "", "aiko_extra_instructions": ""})
    assert "aiko-fab" in client.get("/", base_url=host).get_data(as_text=True)
    r = client.post("/admin/assistent", base_url=host, data={"action": "probe", "question": "Wann trefft ihr euch?"})
    assert 'class="msg assistant' in r.get_data(as_text=True)


def test_club_page_keeps_assistant_settings_when_saving_branding(client, app):
    host = "http://berlin.klubs.test"
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=host)
    client.post("/admin/assistent", base_url=host, data={"assistant_name": "Mira", "about": "Gründerkreis Berlin",
                                                         "assistant_facts": "Fakt X", "public_assistant": "1",
                                                         "aiko_extra_instructions": "Anweisung Y"})
    client.post("/admin/klub", base_url=host, data={"name": "Berlin Founders", "accent": "#112233", "registration": "open"})
    page = client.get("/admin/assistent", base_url=host).get_data(as_text=True)
    assert 'value="Mira"' in page and "Gründerkreis Berlin" in page and "Fakt X" in page and "Anweisung Y" in page
    assert "Berlin Founders" in client.get("/", base_url=host).get_data(as_text=True)


def test_payments_page_sums_paid_events(client, app):
    host = "http://berlin.klubs.test"
    with app.app_context():
        berlin = Club.query.filter_by(slug="berlin").one()
        with use_club(berlin):
            admin = User.query.filter_by(role="superadmin").one()
            ev = Event(title="Pitch-Abend", format="community", source="admin", status="published", price_cents=2000,
                       starts_at=utcnow())
            db.session.add(ev)
            db.session.flush()
            db.session.add(Registration(event_id=ev.id, user_id=admin.id, status="paid", amount_cents=2000))
            db.session.commit()
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=host)
    page = client.get("/admin/zahlungen", base_url=host).get_data(as_text=True)
    assert "Pitch-Abend" in page and "20,00 €" in page
    # Moderator ohne Superadmin-Rolle sieht die Seite nicht
    assert client.get("/admin/zahlungen").status_code in (302, 403, 404)
