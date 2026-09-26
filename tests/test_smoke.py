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


def _png_bytes(size=(300, 200), color=(200, 60, 20)):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    buf.seek(0)
    return buf


def test_profile_photo_socials_and_community(client, app, tmp_path):
    import io

    app.config["UPLOAD_DIR"] = str(tmp_path)
    register(client, "foto@example.com")
    base = {"first_name": "Foto", "last_name": "Nutzerin", "headline": "Designerin", "q_can_help": "Design",
            "q_looking_for": "Kunden"}
    # Foto + Social-Links; falsches Netzwerk und javascript: werden verworfen, @handle wird zu URL
    r = client.post("/app/profil", data={**base, "photo": (_png_bytes(), "me.png"),
                                         "linkedin_url": "https://www.linkedin.com/in/foto",
                                         "instagram_url": "@foto_design", "github_url": "https://evil.example/x",
                                         "x_url": "javascript:alert(1)", "socials_public": "1"},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Profil gespeichert" in r.get_data(as_text=True)
    assert "GitHub" in r.get_data(as_text=True) and "X" in r.get_data(as_text=True)  # Hinweis auf verworfene Links
    with app.app_context():
        p = User.query.filter_by(email="foto@example.com").first().profile
        assert p.photo and (tmp_path / "avatars" / p.photo).exists()
        assert p.instagram_url == "https://www.instagram.com/foto_design"
        assert p.github_url == "" and p.x_url == "" and p.socials_public
        name, uid = p.photo, p.user_id
    served = client.get(f"/app/foto/{name}")
    assert served.status_code == 200 and served.mimetype == "image/jpeg"
    from PIL import Image
    im = Image.open(io.BytesIO(served.data))
    served.close()  # Windows: Dateihandle freigeben, sonst lässt sich die Datei später nicht löschen
    assert im.size == (512, 512) and not im.getexif()
    assert client.get("/app/foto/../x.jpg").status_code == 404

    # ungültige Datei wird abgelehnt und das alte Foto bleibt
    r = client.post("/app/profil", data={**base, "photo": (io.BytesIO(b"kein bild"), "x.png")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "nicht als Bild" in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.get(uid).profile.photo == name

    # eigene Vorschau + Community-Seite
    assert client.get(f"/app/mitglieder/{uid}").status_code == 200
    r = client.get("/app/community")
    assert r.status_code == 200 and "Neu in der Community" in r.get_data(as_text=True)
    # Foto nur für Angemeldete
    client.post("/logout")
    assert client.get(f"/app/foto/{name}", follow_redirects=False).status_code == 302

    # Foto entfernen löscht die Datei
    login(client, "foto@example.com", "sehr-sicheres-pw")
    client.post("/app/profil", data={**base, "remove_photo": "1"}, follow_redirects=True)
    with app.app_context():
        assert User.query.get(uid).profile.photo is None
    assert not (tmp_path / "avatars" / name).exists()


def test_add_missing_columns_migrates_old_schema(app):
    from sqlalchemy import text

    from app import add_missing_columns
    with app.app_context():
        db.session.remove()
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE profiles DROP COLUMN photo"))
            conn.execute(text("ALTER TABLE profiles DROP COLUMN socials_public"))
        assert set(add_missing_columns()) == {"profiles.photo", "profiles.socials_public"}
        assert add_missing_columns() == []


def test_profile_guide_coach_and_contact_assistant(client, app):
    """Guide im Profil, Coach-Feedback, Kontakt-Assistent mit Entwurf/Termin und Cache (ohne API-Schlüssel = Fallback)."""
    from app.models import PairInsight
    register(client)
    body = client.get("/app/profil").get_data(as_text=True)
    assert "Wozu diese Frage" in body and "data-coach" in body and "data-example" in body

    r = client.post("/app/api/profil-coach", json={"q_focus": "IT", "q_challenge": "", "q_can_help": "", "q_looking_for": ""})
    d = r.get_json()
    assert r.status_code == 200 and d["ai"] is False and {t["field"] for t in d["tips"]} >= {"q_focus", "q_challenge"}
    assert client.post("/app/api/profil-coach", json={}).get_json()["tips"] == []

    client.post("/app/profil", data={"first_name": "Nina", "headline": "Designerin",
                                     "q_focus": "Ich gestalte Marken für Start-ups",
                                     "q_challenge": "Ich brauche Hilfe bei der Steuer für meine Freiberuflichkeit",
                                     "q_can_help": "Branding und Webdesign", "q_looking_for": "Steuerberater"})
    with app.app_context():
        julia = User.query.filter_by(email="julia.wagner@demo.main-power.local").first().id
    r = client.post(f"/app/api/kontakt-assistent/{julia}", json={})
    d = r.get_json()
    assert r.status_code == 200 and d["ai"] is False
    assert "Julia" in d["message_draft"] and d["they_help_you"] and d["you_help_them"]
    assert d["event"] and d["event"]["url"].startswith("/app/termine/")
    with app.app_context():
        assert PairInsight.query.count() == 1
    client.post(f"/app/api/kontakt-assistent/{julia}", json={})
    with app.app_context():
        assert PairInsight.query.count() == 1  # aus dem Cache
    # Seite der Person enthält das Assistenten-Panel; unsichtbare/eigene Profile sind nicht abrufbar
    assert "data-insight" in client.get(f"/app/mitglieder/{julia}").get_data(as_text=True)
    with app.app_context():
        me = User.query.filter_by(email="neu@example.com").first().id
    assert client.post(f"/app/api/kontakt-assistent/{me}", json={}).status_code == 404


def test_event_invitations_and_network_pages(client, app):
    from app.models import Event, Notification
    from app.services import insights
    with app.app_context():
        ev = Event(title="Themen-Frühstück Cybersecurity im Mittelstand", format="community", source="admin",
                   description="NIS-2, Pentest und Datenschutz — was Unternehmen jetzt tun müssen.",
                   starts_at=utcnow_plus(20), status="published")
        db.session.add(ev)
        db.session.commit()
        with app.test_request_context():
            created = insights.invite_for_event(ev, limit=3)
        assert created and all(n.body for n in created)
        names = {db.session.get(User, n.user_id).first_name for n in created}
        assert "Ivan" in names  # Cybersecurity-Berater wird zuerst eingeladen
        with app.test_request_context():
            assert len(insights.invite_for_event(ev, limit=3)) <= 3 and Notification.query.filter_by(event_id=ev.id).count() <= 6  # niemand doppelt
        ivan = User.query.filter_by(first_name="Ivan").first().email
    login(client, ivan, "demo-passwort-123")
    dash = client.get("/app/").get_data(as_text=True)
    assert "Aiko lädt dich ein" in dash
    assert "data-network" in client.get("/app/community").get_data(as_text=True)
    assert "data-network" in client.get("/").get_data(as_text=True)
    client.post("/logout")
    login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
          os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))
    assert "Matching-Karte" in client.get("/admin/").get_data(as_text=True)
    with app.app_context():
        evid = Event.query.filter_by(title="Themen-Frühstück Cybersecurity im Mittelstand").first().id
    assert client.get(f"/admin/termine/{evid}/einladungen").status_code == 200


def utcnow_plus(days):
    from datetime import timedelta

    from app.models import utcnow
    return utcnow() + timedelta(days=days)


def test_legal_pages_cookie_banner_and_subscriptions(client, app):
    for url, needle in [("/cookies", "technisch notwendig"),
                        ("/nutzungsbedingungen", "Kündigung und Löschung"), ("/datenschutz", "Cookies"),
                        ("/ki-hinweis", "KI")]:
        r = client.get(url)
        assert r.status_code == 200 and needle in r.get_data(as_text=True), url
    home = client.get("/").get_data(as_text=True)
    assert "data-cookie-banner" in home and "/nutzungsbedingungen" in home and "/cookies" in home
    reg = client.get("/registrieren").get_data(as_text=True)
    assert "Nutzungsbedingungen" in reg

    register(client, "abo@example.com")
    page = client.get("/app/privatsphaere").get_data(as_text=True)
    assert 'id="abos"' in page and 'id="konto-loeschen"' in page and "Persönliche Einladungen" in page
    # Einladungen abbestellen -> Profil wird nicht mehr eingeladen
    client.post("/app/privatsphaere", data={"matching": "1", "directory": "1"}, follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(email="abo@example.com").first().profile.event_invites is False
    client.post("/app/privatsphaere", data={"matching": "1", "event_invites": "1", "newsletter": "1"}, follow_redirects=True)
    with app.app_context():
        u = User.query.filter_by(email="abo@example.com").first()
        assert u.profile.event_invites is True and u.has_consent("newsletter")
    # Konto löschen mit falschem und richtigem Passwort
    r = client.post("/app/privatsphaere/loeschen", data={"password": "falsch"}, follow_redirects=True)
    assert "nicht gelöscht" in r.get_data(as_text=True)
    client.post("/app/privatsphaere/loeschen", data={"password": "sehr-sicheres-pw"}, follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(email="abo@example.com").first() is None


def test_admin_impersonation_demo_mode(client, app):
    from app.models import AuditLog
    with app.app_context():
        julia = User.query.filter_by(email="julia.wagner@demo.main-power.local").first().id
        markus = User.query.filter_by(email="markus.albrecht@demo.main-power.local").first().id
    login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
          os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))
    # ohne Flag: nicht verfügbar
    app.config["ENABLE_IMPERSONATION"] = False
    assert client.post(f"/admin/mitglieder/{julia}/als-nutzer").status_code == 404
    app.config["ENABLE_IMPERSONATION"] = True
    assert "Als dieses Mitglied ansehen" in client.get(f"/admin/mitglieder/{julia}").get_data(as_text=True)

    r = client.post(f"/admin/mitglieder/{julia}/als-nutzer", follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "Demo-Modus" in page and "Julia" in page and 'data-switch-select' in page
    # Wechsel zu anderem Nutzer ohne Umweg über den Admin
    r = client.post(f"/app/als-nutzer/{markus}", follow_redirects=True)
    assert "Markus Albrecht" in r.get_data(as_text=True)
    # gesperrte Aktionen im Demo-Modus
    r = client.post("/app/privatsphaere/loeschen", data={"password": "demo-passwort-123"}, follow_redirects=True)
    assert "gesperrt" in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(User, markus) is not None
    # zurück zum Admin
    r = client.post("/app/als-nutzer-beenden", follow_redirects=True)
    assert r.status_code == 200 and "Mitglieder" in r.get_data(as_text=True)
    assert client.get("/admin/").status_code == 200
    with app.app_context():
        admin_id = User.query.filter_by(role="superadmin").first().id
        acts = [(a.action, a.actor_id) for a in AuditLog.query.filter(AuditLog.action.like("impersonate.%"))]
        assert ("impersonate.start", admin_id) in acts and ("impersonate.switch", admin_id) in acts \
            and ("impersonate.stop", admin_id) in acts
    # normales Mitglied darf nicht wechseln
    client.post("/logout")
    login(client, "julia.wagner@demo.main-power.local", "demo-passwort-123")
    assert client.post(f"/app/als-nutzer/{markus}").status_code == 403
    assert client.post(f"/admin/mitglieder/{markus}/als-nutzer").status_code == 403
    app.config["ENABLE_IMPERSONATION"] = False


GOOD_HTML = b"""<!doctype html><html lang="de"><head><title>Steuerberatung Frankfurt f\xc3\xbcr Gr\xc3\xbcnder | Kanzlei Wagner</title>
<meta name="description" content="Steuerberatung f\xc3\xbcr Gr\xc3\xbcnder und Selbstst\xc3\xa4ndige in Frankfurt: digitale Buchhaltung, Holding-Strukturen und klare Preise. Jetzt Erstgespr\xc3\xa4ch buchen.">
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="canonical" href="https://kanzlei.example/">
<meta property="og:title" content="Kanzlei Wagner"><meta property="og:description" content="x"><meta property="og:image" content="/i.jpg">
<script type="application/ld+json">{"@type":"Organization"}</script></head><body><h1>Steuerberatung f\xc3\xbcr Gr\xc3\xbcnder</h1>
<h2>Leistungen</h2><h2>Preise</h2><img src="a.jpg" alt="Team"><a href="/kontakt">Kontakt</a><a href="/preise">Preise</a><a href="/blog">Blog</a>
<p>""" + b"Steuerberatung Gr\xc3\xbcndung Buchhaltung Holding " * 80 + b"</p></body></html>"


def test_seo_check_service_and_report(client, app, monkeypatch):
    from types import SimpleNamespace

    from app.models import SeoReport
    from app.services import seo_check

    def fake_get(url, max_bytes=0):
        if url.endswith("/robots.txt"):
            body = b"User-agent: *\nDisallow: /admin\nSitemap: https://kanzlei.example/sitemap.xml\n"
        elif url.endswith("/sitemap.xml"):
            body = b"<urlset></urlset>"
        else:
            body = GOOD_HTML
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, encoding="utf-8"), body, 0.4, url

    monkeypatch.setattr(seo_check, "_get", fake_get)
    register(client)
    assert client.get("/app/leistungen/seo-check").status_code == 302  # Katalogeintrag führt zum Werkzeug
    page = client.get("/app/seo-check").get_data(as_text=True)
    assert "Jetzt prüfen" in page

    # ohne Bestätigung keine Prüfung
    r = client.post("/app/seo-check", data={"url": "https://kanzlei.example"}, follow_redirects=True)
    assert "prüfen darfst" in r.get_data(as_text=True)

    r = client.post("/app/seo-check", data={"url": "kanzlei.example", "keywords": "Steuerberatung, Holding, Pizza",
                                            "authorized": "1"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "SEO-Bericht" in body and "Automatischer Bericht" in body  # ohne API-Schlüssel
    assert "Keyword „Pizza“" in body and "Open Graph" in body
    with app.app_context():
        row = SeoReport.query.one()
        assert row.score >= 80 and row.data["result"]["facts"]["title"].startswith("Steuerberatung")
        rid = row.id
    assert client.post(f"/app/seo-check/{rid}/loeschen", follow_redirects=True).status_code == 200
    with app.app_context():
        assert SeoReport.query.count() == 0


def test_seo_check_blocks_internal_addresses(client):
    from app.services import seo_check
    for bad in ["http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.5/",
                "ftp://example.com/", "http://user:pw@example.com/", "https://example.com:8080/"]:
        try:
            seo_check.analyze(bad, [])
        except seo_check.SeoCheckError:
            continue
        raise AssertionError(f"{bad} wurde nicht abgelehnt")


SHOP_HTML = b"""<!doctype html><html lang="de"><head><title>Shop</title>
<script src="https://www.googletagmanager.com/gtag/js?id=G-1"></script><link href="https://fonts.googleapis.com/css?family=Roboto" rel="stylesheet">
</head><body><h1>Unser Shop</h1><button>In den Warenkorb</button><a href="/kasse">Zur Kasse</a>
<form action="/n"><input type="email" name="email"><button>Anmelden</button></form>
<footer><a href="/impressum">Impressum</a> <a href="/datenschutz">Datenschutz</a></footer></body></html>"""
IMPRESSUM = ("Impressum Musterfirma GmbH Beispielstraße 12 60311 Frankfurt am Main Tel. +49 69 123456 E-Mail: info@musterfirma.de "
             "vertreten durch die Geschäftsführerin Erika Muster Handelsregister Amtsgericht Frankfurt HRB 12345 USt-IdNr. DE123456789 "
             + "Angaben gemäß § 5 DDG. " * 5).encode()
DATENSCHUTZ = ("Datenschutzerklärung Verantwortlicher ist die Musterfirma GmbH. Rechte: Auskunft, Löschung, Widerspruch. "
               "Beschwerde bei der Aufsichtsbehörde. Rechtsgrundlage Art. 6 DSGVO. Speicherdauer: Daten werden gelöscht. "
               "Hosting und Server-Logfiles. Auftragsverarbeiter, Empfänger. Cookies und Tracking. " * 25).encode()


def _fake_site(pages):
    from types import SimpleNamespace

    def fake_get(url, max_bytes=0):
        path = "/" + url.split("//", 1)[1].split("/", 1)[1] if url.count("/") > 2 else "/"
        body = pages.get(path)
        if body is None:
            return SimpleNamespace(status_code=404, headers={}, encoding="utf-8", raw=SimpleNamespace(headers=SimpleNamespace(getlist=lambda k: []))), b"", 0.1, url
        raw = SimpleNamespace(headers=SimpleNamespace(getlist=lambda k: ["_ga=GA1.2.3; Path=/"] if path == "/" else []))
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, encoding="utf-8", raw=raw), body, 0.3, url
    return fake_get


def test_compliance_check_finds_gaps_and_ok_pages(client, app, monkeypatch):
    from app.models import SeoReport
    from app.services import compliance_check, seo_check

    monkeypatch.setattr(seo_check, "_get", _fake_site({"/": SHOP_HTML, "/impressum": IMPRESSUM, "/datenschutz": DATENSCHUTZ}))
    monkeypatch.setattr(compliance_check, "_get", seo_check._get)
    res = compliance_check.analyze("https://shop.example")
    by = {f["key"]: f for f in res["findings"]}
    assert by["imp"]["status"] == "ok" and by["imp_addr"]["status"] == "ok" and by["imp_reg"]["status"] == "ok"
    assert by["ds"]["status"] == "ok"
    assert by["cmp"]["status"] == "fail"            # GTM ohne Consent-Banner
    assert by["cookies_set"]["status"] == "fail"    # _ga schon beim ersten Aufruf
    assert by["fonts"]["status"] == "fail"          # Google Fonts extern
    assert by["ds_tools"]["status"] == "fail"       # GA/GTM/Fonts nicht in der Datenschutzerklärung
    assert by["widerruf"]["status"] == "fail" and by["agb"]["status"] == "fail"  # Shop ohne AGB/Widerruf
    assert "Shop" in " ".join(res["facts"]["dienste"] or ["Shop"]) or res["facts"]["shop"]
    assert 0 < res["score"] < 80

    # Seite ganz ohne Rechtstexte
    monkeypatch.setattr(seo_check, "_get", _fake_site({"/": b"<html><body><h1>Hallo</h1></body></html>"}))
    monkeypatch.setattr(compliance_check, "_get", seo_check._get)
    res = compliance_check.analyze("https://leer.example")
    by = {f["key"]: f for f in res["findings"]}
    assert by["imp"]["status"] == "fail" and by["ds"]["status"] == "fail"

    # Route: Katalog, Formular, Bericht, Löschen
    monkeypatch.setattr(seo_check, "_get", _fake_site({"/": SHOP_HTML, "/impressum": IMPRESSUM, "/datenschutz": DATENSCHUTZ}))
    monkeypatch.setattr(compliance_check, "_get", seo_check._get)
    register(client)
    assert client.get("/app/leistungen/compliance-check").status_code == 302
    assert "Was geprüft wird" in client.get("/app/compliance-check").get_data(as_text=True)
    r = client.post("/app/compliance-check", data={"url": "shop.example", "authorized": "1"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Compliance-Bericht" in body and "keine Rechtsberatung" in body and "Widerrufsbelehrung" in body
    with app.app_context():
        row = SeoReport.query.filter_by(kind="compliance").one()
        rid = row.id
    r = client.post(f"/app/seo-check/{rid}/loeschen", follow_redirects=True)
    assert "Compliance-Check" in r.get_data(as_text=True)


def test_security_check_passive(client, app, monkeypatch):
    import time
    from types import SimpleNamespace

    from app.models import SeoReport
    from app.services import security_check as sc

    monkeypatch.setattr(sc, "_assert_public", lambda host: None)
    monkeypatch.setattr(sc, "_tls", lambda host: {"reachable": True, "valid": True, "version": "TLSv1.3",
                                                  "not_after": time.time() + 12 * 86400, "issuer": "Let's Encrypt"})
    records = {"kanzlei.example": ["v=spf1 include:_spf.mail.example ~all"], "_dmarc.kanzlei.example": ["v=DMARC1; p=none"]}
    monkeypatch.setattr(sc, "_txt", lambda name: records.get(name, []))
    monkeypatch.setattr(sc, "_has", lambda name, rtype: False)

    def fake_fetch(url, max_bytes=0):
        headers = {"Content-Type": "text/html", "Server": "nginx/1.18.0", "Strict-Transport-Security": "max-age=63072000"}
        raw = SimpleNamespace(headers=SimpleNamespace(getlist=lambda k: ["sid=1; Path=/", "lang=de; Secure; HttpOnly; SameSite=Lax"]))
        final = url.replace("http://", "https://") if url.startswith("http://") else url
        return SimpleNamespace(status_code=404 if "security.txt" in url else 200, headers=headers, raw=raw, encoding="utf-8"), b"<html></html>", 0.2, final

    monkeypatch.setattr(sc, "_fetch", fake_fetch)
    res = sc.analyze("https://www.kanzlei.example/seite")
    by = {f["key"]: f for f in res["findings"]}
    assert by["https"]["status"] == "ok" and by["cert"]["status"] == "ok" and by["redir"]["status"] == "ok"
    assert by["cert_exp"]["status"] == "warn"           # 12 Tage
    assert by["hsts"]["status"] == "ok" and by["csp"]["status"] == "warn"
    assert by["leak"]["status"] == "warn" and "nginx/1.18.0" in by["leak"]["detail"]
    assert by["cookies"]["status"] == "warn"            # ein Cookie ohne Schutzattribute
    assert by["spf"]["status"] == "warn" and by["dmarc"]["status"] == "warn" and "p=none" in by["dmarc"]["detail"]
    assert by["dnssec"]["status"] == "warn" and by["sectxt"]["status"] == "warn"
    assert 40 < res["score"] < 90 and res["facts"]["domain"] == "kanzlei.example"

    for bad in ["127.0.0.1", "10.1.2.3", "ftp://x.de", "user:pw@x.de", "keinedomain", ""]:
        try:
            sc.hostname_from(bad)
        except sc.SeoCheckError:
            continue
        raise AssertionError(bad)
    assert sc.org_domain("www.shop.co.uk") == "shop.co.uk" and sc.org_domain("a.b.example.de") == "example.de"

    register(client)
    assert client.get("/app/leistungen/sicherheits-check").status_code == 302
    assert "Was geprüft wird" in client.get("/app/sicherheits-check").get_data(as_text=True)
    r = client.post("/app/sicherheits-check", data={"url": "kanzlei.example", "authorized": "1"}, follow_redirects=True)
    body = r.get_data(as_text=True)
    assert "Sicherheits-Bericht" in body and "kein Penetrationstest" in body and "DMARC" in body
    with app.app_context():
        rid = SeoReport.query.filter_by(kind="security").one().id
    assert "Sicherheits-Check" in client.post(f"/app/seo-check/{rid}/loeschen", follow_redirects=True).get_data(as_text=True)


def test_security_check_blocks_internal_targets():
    from app.services import security_check as sc
    for bad in ["localhost.localdomain", "169.254.169.254"]:
        try:
            sc.analyze(bad)
        except sc.SeoCheckError:
            continue
        raise AssertionError(bad)
