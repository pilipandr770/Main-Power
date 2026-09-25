"""Smoke-Tests: laufen offline (lokale Embeddings, Aiko im Demo-Modus, kein Stripe/Telegram).

    pytest -q
"""
import json
import os

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Event, IntroRequest, Registration, User


class Cfg(TestConfig):
    EVENTS_SYNC_URL = "http://127.0.0.1:9/offline"


@pytest.fixture()
def app():
    app = create_app(Cfg)
    with app.app_context():
        from app.seed import seed
        seed(demo=True)
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, email, pw):
    return client.post("/login", data={"email": email, "password": pw}, follow_redirects=True)


def register(client, email="neu@example.com", matching=True):
    data = {"first_name": "Nina", "last_name": "Neu", "email": email, "password": "sehr-sicheres-pw",
            "consent_privacy": "1", "consent_values": "1", "consent_directory": "1"}
    if matching:
        data["consent_matching"] = "1"
    return client.post("/registrieren", data=data, follow_redirects=True)


def test_public_pages(client):
    for url in ["/", "/termine", "/formate/hub", "/formate/laufen", "/datenschutz", "/ki-hinweis", "/healthz",
                "/login", "/registrieren", "/passwort-vergessen"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert client.get("/formate/unbekannt").status_code == 404
    assert "Content-Security-Policy" in client.get("/").headers


def test_public_aiko_fallback(client):
    r = client.post("/api/aiko", json={"history": [{"role": "user", "content": "Was kostet der Hub?"}]})
    assert r.status_code == 200
    assert "25 €" in r.get_json()["reply"]


def test_member_protected(client):
    r = client.get("/app/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/admin/").status_code == 302


def test_full_member_flow(client, app):
    r = register(client)
    assert r.status_code == 200 and "Die vier Fragen" in r.get_data(as_text=True)

    r = client.post("/app/profil", data={
        "first_name": "Nina", "headline": "Gründerin, E-Commerce-Start-up", "industry": "Handel",
        "q_focus": "Onlineshop für nachhaltige Mode", "q_challenge": "Steuerliche Fragen zum EU-Versand",
        "q_can_help": "Shopify, Performance-Marketing, Produktfotografie organisieren",
        "q_looking_for": "Steuerberatung für Umsatzsteuer im EU-Ausland", "expertise": "E-Commerce, Shopify",
        "linkedin_url": "javascript:alert(1)", "website_url": "nina-shop.de", "formats": ["hub", "laufen"],
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        u = User.query.filter_by(email="neu@example.com").first()
        assert u.profile.linkedin_url == ""  # javascript:-URL verworfen
        assert u.profile.website_url == "https://nina-shop.de"
        assert u.profile.embed_need

    r = client.get("/app/matches")
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Passung" in body or "Ergänzung" in body
    assert "Julia" in body  # Steuerberaterin sollte oben landen

    for url in ["/app/", "/app/mitglieder", "/app/mitglieder?q=Arbeitsrecht", "/app/termine", "/app/aiko",
                "/app/leistungen", "/app/leistungen/ki-chatbots", "/app/community", "/app/privatsphaere",
                "/app/kontakte", "/app/termine/neu"]:
        assert client.get(url).status_code == 200, url

    r = client.post("/app/api/aiko", json={"message": "Wie funktioniert das Matching?"})
    assert r.status_code == 200 and r.get_json()["reply"]

    with app.app_context():
        julia = User.query.filter(User.email.like("julia.%")).first()
        julia_id = julia.id
        hub = Event.query.filter_by(format="hub").first()
        hub_id = hub.id
    assert client.get(f"/app/mitglieder/{julia_id}").status_code == 200
    r = client.post(f"/app/kontakt/{julia_id}", data={"message": "Hallo Julia!"}, follow_redirects=True)
    assert "Anfrage an Julia gesendet" in r.get_data(as_text=True)

    r = client.post(f"/app/termine/{hub_id}/anmelden", data={"note": "vegetarisch"}, follow_redirects=True)
    assert "angemeldet" in r.get_data(as_text=True)
    assert client.get(f"/app/termine/{hub_id}/ics").mimetype == "text/calendar"
    r = client.post(f"/app/termine/{hub_id}/abmelden", follow_redirects=True)
    assert r.status_code == 200

    r = client.post("/app/termine/neu", data={"title": "Frühstück KI im Mittelstand", "date": "2030-05-05",
                                              "time": "08:00", "location": "Frankfurt"}, follow_redirects=True)
    assert "Freigabe" in r.get_data(as_text=True)

    r = client.post("/app/leistungen/cybersecurity-check", data={"message": "Bitte Check"}, follow_redirects=True)
    assert "Anfrage ist angekommen" in r.get_data(as_text=True)

    r = client.get("/app/privatsphaere/export")
    data = json.loads(r.get_data(as_text=True))
    assert data["konto"]["email"] == "neu@example.com"

    r = client.post("/app/privatsphaere", data={"directory": "1"}, follow_redirects=True)
    with app.app_context():
        u = User.query.filter_by(email="neu@example.com").first()
        assert not u.profile.allow_matching

    # Julia nimmt an
    client.post("/logout")
    login(client, "julia.wagner@demo.main-power.local", "demo-passwort-123")
    with app.app_context():
        ir = IntroRequest.query.filter_by(to_user_id=julia_id, status="pending").first()
        ir_id = ir.id
    r = client.post(f"/app/kontakte/{ir_id}/annehmen", follow_redirects=True)
    assert "neu@example.com" in r.get_data(as_text=True)

    # Nina löscht ihr Konto
    client.post("/logout")
    login(client, "neu@example.com", "sehr-sicheres-pw")
    r = client.post("/app/privatsphaere/loeschen", data={"password": "sehr-sicheres-pw"}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert User.query.filter_by(email="neu@example.com").first() is None
        assert IntroRequest.query.filter_by(to_user_id=julia_id, status="accepted", message="Hallo Julia, ich gründe gerade und suche eine Steuerberaterin.").count() == 1  # Demo-Anfrage bleibt
        assert IntroRequest.query.filter(IntroRequest.to_user_id == julia_id, IntroRequest.message.like("%Nina%")).count() == 0


def test_admin(client, app):
    register(client, "mitglied@example.com")
    client.post("/logout")
    assert "gesperrt" not in login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
                                    os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern")).get_data(as_text=True)
    with app.app_context():
        hub = Event.query.filter_by(format="hub").first()
        member = User.query.filter_by(email="mitglied@example.com").first()
        reg = Registration.query.filter_by(event_id=hub.id).first()
        ids = dict(hub=hub.id, member=member.id, reg=reg.id)
    for url in ["/admin/", "/admin/mitglieder", "/admin/mitglieder?q=julia", "/admin/mitglieder/export.csv",
                f"/admin/mitglieder/{ids['member']}", "/admin/suche?q=Steuerberatung", "/admin/termine",
                "/admin/termine?show=pending", "/admin/termine?show=past", "/admin/termine/neu",
                f"/admin/termine/{ids['hub']}", f"/admin/termine/{ids['hub']}/bearbeiten",
                f"/admin/termine/{ids['hub']}/matching", f"/admin/termine/{ids['hub']}/teilnehmende.csv",
                "/admin/kontakte", "/admin/leistungen", "/admin/leistungen/neu", "/admin/wissen",
                "/admin/einwilligungen", "/admin/protokoll", "/admin/einstellungen"]:
        assert client.get(url).status_code == 200, url

    r = client.get(f"/admin/termine/{ids['hub']}/matching")
    assert "Beste Gesprächspartner" in r.get_data(as_text=True)

    r = client.post("/admin/termine/neu", data={"title": "Hub Spezial", "format": "hub", "date": "2030-01-10",
                                               "time": "08:30", "price": "25", "status": "published"},
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert Event.query.filter_by(title="Hub Spezial").first().price_cents == 2500

    client.post(f"/admin/termine/{ids['hub']}/anmeldung/{ids['reg']}", data={"status": "paid"})
    client.post("/admin/wissen", data={"question": "Testfrage?", "answer": "Testantwort", "public": "1",
                                       "active": "1", "sort": "99"})
    client.post("/admin/einstellungen", data={"announcement": "Hub am Mittwoch ausgebucht",
                                              "telegram_group_title": "MP"})
    client.post(f"/admin/mitglieder/{ids['member']}/sperren")
    with app.app_context():
        assert db.session.get(User, ids["member"]).status == "blocked"
    r = client.post(f"/admin/mitglieder/{ids['member']}/export")
    assert r.mimetype == "application/json"
    client.post(f"/admin/mitglieder/{ids['member']}/loeschen")
    with app.app_context():
        assert db.session.get(User, ids["member"]) is None


def test_member_cannot_access_admin(client):
    register(client, "x@example.com")
    assert client.get("/admin/").status_code == 403


def test_admin_edit_member_and_token_counter(client, app):
    from app.models import LLMUsage
    register(client, "edit@example.com")
    client.post("/logout")
    login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
          os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))
    with app.app_context():
        uid = User.query.filter_by(email="edit@example.com").first().id
        db.session.add(LLMUsage(purpose="aiko_public", model="test", input_tokens=1200, output_tokens=300))
        db.session.commit()
    assert client.get(f"/admin/mitglieder/{uid}/bearbeiten").status_code == 200
    r = client.post(f"/admin/mitglieder/{uid}/bearbeiten",
                    data={"first_name": "Erika", "last_name": "Muster", "email": "edit@example.com",
                          "q_can_help": "Steuern", "headline": "Beraterin", "linkedin_url": "javascript:alert(1)"},
                    follow_redirects=True)
    assert "Mitglied gespeichert" in r.get_data(as_text=True)
    with app.app_context():
        u = db.session.get(User, uid)
        assert u.first_name == "Erika" and u.profile.headline == "Beraterin" and u.profile.linkedin_url == ""
    dash = client.get("/admin/").get_data(as_text=True)
    assert "KI-Verbrauch" in dash and "1.500" in dash


def test_demo_scenarios_and_ai_act_rules(app):
    from app.models import Registration
    from app.services.llm import AI_ACT_RULES
    with app.app_context():
        assert User.query.filter_by(status="blocked").count() == 1
        assert User.query.filter_by(role="admin").count() == 1
        assert IntroRequest.query.filter_by(status="accepted").count() == 1
        assert Registration.query.filter_by(status="paid").count() == 1
    assert "Art. 50" in AI_ACT_RULES and "kein Mensch" in AI_ACT_RULES


def test_llm_gets_ai_act_rules_and_counts_tokens(app, monkeypatch):
    """Jeder KI-Aufruf trägt die KI-VO-Regeln an erster Stelle und schreibt den Token-Verbrauch mit."""
    import types

    import anthropic

    from app.models import LLMUsage
    from app.services import llm
    sent = {}

    class FakeClient:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Hallo")],
                                         usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with app.app_context():
        app.config["ANTHROPIC_API_KEY"] = "test"
        assert llm.complete("Persona", [{"role": "user", "content": "Hi"}], purpose="aiko_public") == "Hallo"
        assert sent["system"].startswith(llm.AI_ACT_RULES) and sent["system"].endswith("Persona")
        row = LLMUsage.query.one()
        assert (row.purpose, row.input_tokens, row.output_tokens) == ("aiko_public", 10, 5)
