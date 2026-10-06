"""Mandantenfähigkeit: Datentrennung zwischen Klubs, Branding je Domain, Plattform-Konsole, Klub-Admin."""
import io
import json

import pytest

from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.models import Club, Event, KnowledgeItem, LLMUsage, MeetingFormat, Profile, Setting, User
from app.tenancy import use_club

B_HOST = "berlin.klubs.test"


class Cfg(TestConfig):
    EVENTS_SYNC_URL = "http://127.0.0.1:9/offline"
    PLATFORM_DOMAIN = "klubs.test"
    PLATFORM_ADMIN_EMAIL = "betreiber@example.com"
    PLATFORM_ADMIN_PASSWORD = "plattform-passwort-123"


@pytest.fixture()
def app():
    """Ohne dauerhaften App-Kontext: jeder Request bekommt wie in Produktion eigenes g und eigene Session."""
    app = create_app(Cfg)
    with app.app_context():
        from app.seed import create_club, seed
        seed(demo=False)
        create_club("berlin", "Founders Berlin", "chefin@berlin.example", "berlin-passwort-123",
                    admin_first="Bea", preset="neutral")
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def _b(client):
    """Client-Aufrufe gegen den zweiten Klub (Subdomain der Plattform-Domain)."""
    return lambda method, url, **kw: getattr(client, method)(url, base_url=f"http://{B_HOST}", **kw)


def register(client, email, base="http://localhost"):
    data = {"first_name": "Nina", "last_name": "Neu", "email": email, "password": "sehr-sicheres-pw",
            "consent_privacy": "1", "consent_values": "1", "consent_matching": "1", "consent_directory": "1"}
    return client.post("/registrieren", data=data, base_url=base, follow_redirects=True)


def login(client, email, pw, base="http://localhost"):
    return client.post("/login", data={"email": email, "password": pw}, base_url=base, follow_redirects=True)


def test_branding_per_domain(client):
    a = client.get("/").get_data(as_text=True)
    b = client.get("/", base_url=f"http://{B_HOST}").get_data(as_text=True)
    assert "Die richtigen Menschen" in a and "Founders Berlin" not in a
    assert "Founders Berlin" in b
    assert 'data-scene="sphere"' in a and 'data-scene="wave"' in b and "<img class=\"tall\"" not in a + b  # 3D statt Fotos
    assert "Main Power" not in a + b and "Roland" not in a + b  # keine Reste des früheren Kunden
    assert "Business-Frühstück" in b
    assert "#14b8a6" in client.get("/club.css", base_url=f"http://{B_HOST}").get_data(as_text=True)
    assert "#14b8a6" in client.get("/club.css").get_data(as_text=True)


def test_data_isolation_between_clubs(client, app):
    register(client, "gleich@example.com")                                  # Klub A
    register(client, "gleich@example.com", base=f"http://{B_HOST}")        # gleiche E-Mail in Klub B erlaubt
    with app.app_context():
        users = User.query.filter_by(email="gleich@example.com").all()
        assert len(users) == 2 and len({u.club_id for u in users}) == 2

    # Admin von B sieht nur B-Mitglieder und kommt nicht an A-Daten heran
    client.post("/logout", base_url=f"http://{B_HOST}")
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=f"http://{B_HOST}")
    with app.app_context():
        a_admin = User.query.filter_by(email="admin@test.local").first() or \
            User.query.filter(User.role == "superadmin", User.email != "chefin@berlin.example").first()
        a_event_id = Event.query.filter_by(club_id=a_admin.club_id).first().id
    members = client.get("/admin/mitglieder", base_url=f"http://{B_HOST}").get_data(as_text=True)
    assert "chefin@berlin.example" in members and "admin@test.local" not in members
    assert client.get(f"/admin/mitglieder/{a_admin.id}", base_url=f"http://{B_HOST}").status_code == 404
    assert client.get(f"/admin/termine/{a_event_id}", base_url=f"http://{B_HOST}").status_code == 404
    assert client.get(f"/app/termine/{a_event_id}", base_url=f"http://{B_HOST}").status_code == 404

    # Konto aus Klub B kann sich in Klub A nicht anmelden
    client.post("/logout")
    r = login(client, "chefin@berlin.example", "berlin-passwort-123")
    assert r.request.path == "/login" and client.get("/app/").status_code == 302

    # Einstellungen und FAQ je Klub getrennt
    with app.app_context():
        clubs = {c.slug: c.id for c in Club.query.all()}
        assert KnowledgeItem.query.filter_by(club_id=clubs["berlin"]).count() > 0
        assert MeetingFormat.query.filter_by(club_id=clubs["berlin"], key="fruehstueck").count() == 1


def test_tenant_filter_on_all_query_types(app):
    """Der automatische Filter greift bei query, get, Aggregaten, Joins und Bulk-Updates."""
    from sqlalchemy import func
    with app.app_context():
        a = Club.query.filter_by(slug="klub").one()
        b = Club.query.filter_by(slug="berlin").one()
        b_user = User.query.filter_by(club_id=b.id).first()
        with use_club(a):
            assert db.session.get(User, b_user.id) is None  # trotz Identity-Map: Objekte von B wurden verworfen
            assert User.query.filter_by(email=b_user.email).first() is None
            n_a = db.session.query(func.count(User.id)).scalar()
            assert n_a == User.query.count() == User.query.execution_options(all_clubs=True).filter_by(club_id=a.id).count()
            assert all(p.user.club_id == a.id for p in Profile.query.join(User).all())
            User.query.update({"phone": "123"})
            db.session.commit()
        assert db.session.get(User, b_user.id).phone != "123"
        with use_club(a):
            with pytest.raises(RuntimeError):
                db.session.add(Setting(key="x", value="y", club_id=b.id))
                db.session.flush()
            db.session.rollback()


def test_platform_console(client, app):
    assert client.get("/plattform/").status_code == 302
    r = client.post("/plattform/login", data={"email": "betreiber@example.com", "password": "falsch"})
    assert "falsch" in r.get_data(as_text=True)
    client.post("/plattform/login", data={"email": "betreiber@example.com", "password": "plattform-passwort-123"})
    page = client.get("/plattform/").get_data(as_text=True)
    assert "Founders Berlin" in page and "klub" in page

    r = client.post("/plattform/klub/neu", data={"name": "Hamburg Hub", "slug": "hamburg", "preset": "neutral",
                                                 "admin_email": "hh@example.com", "admin_password": "hamburg-passwort-1",
                                                 "domains": "netzwerk-hamburg.de"}, follow_redirects=True)
    assert "Hamburg Hub" in r.get_data(as_text=True)
    assert "Hamburg Hub" in client.get("/", base_url="http://netzwerk-hamburg.de").get_data(as_text=True)
    # doppelte Slugs und fremde Domains werden abgelehnt
    r = client.post("/plattform/klub/neu", data={"name": "X", "slug": "hamburg", "admin_email": "x@example.com",
                                                 "admin_password": "xxxxxxxxxxxx", "domains": "netzwerk-hamburg.de"})
    assert "vergeben" in r.get_data(as_text=True)

    with app.app_context():
        hid = Club.query.filter_by(slug="hamburg").one().id
    client.post(f"/plattform/klub/{hid}/status")
    assert client.get("/", base_url="http://netzwerk-hamburg.de").status_code == 503
    client.post(f"/plattform/klub/{hid}/status")
    assert client.get("/", base_url="http://netzwerk-hamburg.de").status_code == 200

    # Ein Klub-Superadmin hat keinen Zugang zur Konsole
    client.post("/plattform/logout")
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=f"http://{B_HOST}")
    assert client.get("/plattform/", base_url=f"http://{B_HOST}").status_code == 302


def test_platform_console_disabled_without_credentials():
    class NoPlatform(Cfg):
        PLATFORM_ADMIN_EMAIL = ""
    app = create_app(NoPlatform)
    with app.app_context():
        db.create_all()
        assert app.test_client().get("/plattform/login").status_code == 404
        db.drop_all()


def test_club_admin_branding_formats_and_export(client, app):
    B = _b(client)
    login(client, "chefin@berlin.example", "berlin-passwort-123", base=f"http://{B_HOST}")
    assert "Klub &amp; Branding" in B("get", "/admin/klub").get_data(as_text=True)
    B("post", "/admin/klub", data={"name": "Berlin Founders", "full_name": "Berlin Founders Club", "accent": "#2255aa",
                                   "hero_title": "Gründer:innen treffen Gründer:innen", "impressum_url": "https://bf.example/impressum",
                                   "testimonials": "Kai | Endlich relevante Gespräche.\nungültige Zeile"})
    home = B("get", "/").get_data(as_text=True)
    assert "Gründer:innen treffen Gründer:innen" in home and "Endlich relevante Gespräche." in home
    assert "#2255aa" in B("get", "/club.css").get_data(as_text=True)
    assert "Berlin Founders" not in client.get("/").get_data(as_text=True)  # Klub A unberührt

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", (600, 200), (0, 128, 255, 255)).save(buf, "PNG")
    buf.seek(0)
    B("post", "/admin/klub/bild/logo", data={"image": (buf, "logo.png")}, content_type="multipart/form-data")
    home = B("get", "/").get_data(as_text=True)
    assert 'class="logo-img"' in home
    src = home.split('class="logo-img" src="')[1].split('"')[0]
    assert B("get", src).status_code == 200 and client.get(src).status_code == 404  # Bild nur im eigenen Klub

    # Format anlegen -> erscheint auf der Startseite
    B("post", "/admin/formate/neu", data={"name": "Pitch-Abend", "price": "15", "rhythm": "Quartalsweise",
                                          "description": "Drei Start-ups pitchen.", "featured": "1", "active": "1"})
    assert "Pitch-Abend" in B("get", "/").get_data(as_text=True)
    assert "Pitch-Abend" not in client.get("/").get_data(as_text=True)

    # Export -> Import in einen anderen Klub
    data = json.loads(B("get", "/admin/klub/export").get_data(as_text=True))
    assert data["settings"]["name"] == "Berlin Founders" and data["settings"]["logo"].startswith("upload:")
    with app.app_context():
        from app.seed import create_club
        create_club("koeln", "Köln Klub", "k@example.com", "koeln-passwort-12", preset="neutral")
    K = "http://koeln.klubs.test"
    login(client, "k@example.com", "koeln-passwort-12", base=K)
    client.post("/admin/klub/import", base_url=K, content_type="multipart/form-data",
                data={"config": (io.BytesIO(json.dumps(data).encode()), "k.json"), "with_faq": "1"})
    home = client.get("/", base_url=K).get_data(as_text=True)
    assert "Gründer:innen treffen Gründer:innen" in home and "Pitch-Abend" in home
    assert 'class="logo-img"' not in home  # hochgeladene Bilder werden nicht übernommen

    # Vorlage anwenden setzt Branding auf die neutralen Standardtexte zurück
    B("post", "/admin/klub/vorlage", data={"preset": "neutral"})
    home = B("get", "/").get_data(as_text=True)
    assert "Die richtigen Menschen" in home and "Gründer:innen treffen Gründer:innen" not in home


def test_ai_prompts_use_club_name(app, monkeypatch):
    import types

    import anthropic

    from app.services import llm
    sent = {}

    class FakeClient:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            sent.update(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="ok")],
                                         usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    with app.test_request_context("/", base_url=f"http://{B_HOST}"):
        app.preprocess_request()
        app.config["ANTHROPIC_API_KEY"] = "x"
        from app.services import aiko
        aiko.answer_public([{"role": "user", "content": "Hallo"}])
        assert "Founders Berlin" in sent["system"] and "Main Power" not in sent["system"]  # Name des Klubs, nicht des Standardklubs
        assert LLMUsage.query.count() == 1 and LLMUsage.query.first().club_id == Club.query.filter_by(slug="berlin").one().id


def test_migration_is_idempotent(app):
    from app.migrations_mt import migrate_multitenant
    with app.app_context():
        migrate_multitenant()
        assert migrate_multitenant() == []


def test_landing_uses_3d_scenes_until_club_uploads_photos(client):
    home = client.get("/").get_data(as_text=True)
    assert home.count("<canvas class=\"scene") == 6 and "js/scenes.js" in home
    assert client.get("/static/js/scenes.js").status_code == 200


def test_impressum_page_placeholder_and_external_link(client, app):
    page = client.get("/impressum").get_data(as_text=True)
    assert "Musterklub" in page and "Platzhalter" in page
    assert 'href="/impressum"' in client.get("/").get_data(as_text=True)
    with app.app_context(), use_club(Club.query.filter_by(slug="klub").one()):
        from app.services import club as club_settings
        club_settings.save({"impressum_url": "https://klub.example/impressum"})
        db.session.commit()
    r = client.get("/impressum")
    assert r.status_code == 302 and r.headers["Location"] == "https://klub.example/impressum"


def test_legacy_branding_is_neutralised_idempotently(app):
    from app.migrations_neutral import neutralize_legacy_branding
    from app.models import Event, Setting, utcnow
    with app.app_context():
        club = Club.query.filter_by(slug="klub").one()
        with use_club(club):
            club.name = "Main Power"
            Setting.set("club.hero_title", "Nicht mehr Kontakte. Relevantere Kontakte. Main Power")
            for f in MeetingFormat.query.all():
                f.active = False
            db.session.add(MeetingFormat(key="hub", name="Main Power Hub", image="img/format-hub.png", rhythm="x",
                                         description="x", tagline="x", short="Hub"))
            db.session.add(Event(title="Main Power Hub", format="hub", source="sync", external_id="main-power-hub-1",
                                 starts_at=utcnow(), status="published"))
            db.session.add(KnowledgeItem(question="Q", answer="Schreib uns an hallo@main-power.org", public=True))
            db.session.commit()
        first = neutralize_legacy_branding()
        assert first and neutralize_legacy_branding() == []
        club = Club.query.filter_by(slug="klub").one()
        assert club.name == "Klub"
        with use_club(club):
            assert not Event.query.filter_by(source="sync").count()
            assert MeetingFormat.query.filter_by(key="hub").count() == 0
            assert MeetingFormat.query.filter_by(key="fruehstueck", active=True).count() == 1
            assert not any("main-power" in k.answer for k in KnowledgeItem.query.all())
            from app.services import club as club_settings
            assert "Main Power" not in club_settings.settings()["hero_title"]


def test_legacy_sync_events_removed_even_when_formats_already_neutral(app):
    """Wie auf einer Einzelklub-Installation: Formate kamen schon neutral aus der Mandanten-Migration, Termine sind alt."""
    from app.migrations_neutral import neutralize_legacy_branding
    from app.models import Event, utcnow
    with app.app_context():
        club = Club.query.filter_by(slug="klub").one()
        with use_club(club):
            for title, fmt in (("Main Power Laufen", "laufen"), ("Event1", "frauenkreis")):
                db.session.add(Event(title=title, format=fmt, source="sync", external_id=title.lower(),
                                     starts_at=utcnow(), status="published"))
            db.session.commit()
        assert neutralize_legacy_branding()
        with use_club(Club.query.filter_by(slug="klub").one()):
            assert Event.query.filter_by(source="sync").count() == 0
            assert Event.query.filter_by(status="published").count() >= 1  # Beispieltermine der neutralen Formate


def test_site_banner_only_when_configured(app, client):
    assert "site-banner" not in client.get("/").get_data(as_text=True)
    app.config["SITE_BANNER"] = "Projekt in Entwicklung: Testdaten"
    page = client.get("/").get_data(as_text=True)
    assert 'class="site-banner"' in page and "Projekt in Entwicklung: Testdaten" in page
    assert 'class="site-banner"' in client.get("/login").get_data(as_text=True)


# --------------------------------------------------------------------------- Registrierungsmodi und Einladungen
def _set_mode(app, mode, slug="klub"):
    from app.services import club as club_settings
    with app.app_context(), use_club(Club.query.filter_by(slug=slug).one()):
        club_settings.save({"registration": mode})
        db.session.commit()


def _reg_data(email, **extra):
    return {"first_name": "Nina", "last_name": "Neu", "email": email, "password": "sehr-sicheres-pw",
            "consent_privacy": "1", "consent_values": "1", **extra}


def test_registration_approval_flow(client, app):
    _set_mode(app, "approval")
    page = client.get("/registrieren").get_data(as_text=True)
    assert "prüft die Klubleitung" in page
    r = client.post("/registrieren", data=_reg_data("neu@example.com"), follow_redirects=True)
    assert "prüfen deine Anmeldung" in r.get_data(as_text=True) and client.get("/app/").status_code == 302
    with app.app_context():
        u = User.query.filter_by(email="neu@example.com").one()
        assert u.status == "pending"
        uid = u.id
    r = login(client, "neu@example.com", "sehr-sicheres-pw")
    assert "wartet noch auf die Freigabe" in r.get_data(as_text=True)
    # Klubleitung schaltet frei
    client.post("/logout")
    login(client, "admin@test.local", "AdminPass12345")
    assert "wartet auf Freigabe" in client.get("/admin/mitglieder?status=pending").get_data(as_text=True)
    assert "1" in client.get("/admin/").get_data(as_text=True)
    client.post(f"/admin/mitglieder/{uid}/freigeben")
    client.post("/logout")
    assert "Willkommen" in login(client, "neu@example.com", "sehr-sicheres-pw").get_data(as_text=True) or \
        client.get("/app/").status_code == 200
    # Ablehnen löscht die Daten
    client.post("/logout")
    client.post("/registrieren", data=_reg_data("abgelehnt@example.com"), follow_redirects=True)
    with app.app_context():
        rid = User.query.filter_by(email="abgelehnt@example.com").one().id
    login(client, "admin@test.local", "AdminPass12345")
    client.post(f"/admin/mitglieder/{rid}/ablehnen")
    with app.app_context():
        assert User.query.filter_by(email="abgelehnt@example.com").count() == 0


def test_invite_only_registration_and_invite_bypasses_approval(client, app):
    from app.models import Invite
    _set_mode(app, "invite")
    r = client.post("/registrieren", data=_reg_data("ohne@example.com"), follow_redirects=True)
    assert "nur mit Einladung" in r.get_data(as_text=True)
    login(client, "admin@test.local", "AdminPass12345")
    # persönliche Einladung per E-Mail, danach ein Gemeinschaftslink mit zwei Plätzen
    r = client.post("/admin/einladungen", data={"emails": "gast@example.com, kaputt", "note": "Willkommen"}, follow_redirects=True)
    assert "Keine gültige E-Mail-Adresse" in r.get_data(as_text=True)
    client.post("/admin/einladungen", data={"emails": "gast@example.com", "note": "Willkommen aus dem Vorstand"})
    client.post("/admin/einladungen", data={"emails": "", "max_uses": "2", "days": "7"})
    with app.app_context():
        personal = Invite.query.filter_by(email="gast@example.com").one().token
        shared = Invite.query.filter_by(email="").one().token
    client.post("/logout")
    # persönliche Einladung gilt nur für die eigene Adresse
    page = client.get(f"/registrieren?einladung={personal}").get_data(as_text=True)
    assert "gast@example.com" in page and "Willkommen aus dem Vorstand" in page
    r = client.post("/registrieren", data=_reg_data("anderer@example.com", einladung=personal))
    assert "andere E-Mail-Adresse" in r.get_data(as_text=True)
    r = client.post("/registrieren", data=_reg_data("gast@example.com", einladung=personal), follow_redirects=True)
    assert client.get("/app/").status_code == 200  # sofort aktiv
    client.post("/logout")
    r = client.post("/registrieren", data=_reg_data("zweiter@example.com", einladung=personal))
    assert "ungültig oder abgelaufen" in r.get_data(as_text=True)  # einmal nutzbar
    # Gemeinschaftslink: zwei Plätze
    for n in (1, 2):
        client.post("/registrieren", data=_reg_data(f"team{n}@example.com", einladung=shared))
        client.post("/logout")
    r = client.post("/registrieren", data=_reg_data("team3@example.com", einladung=shared))
    assert "ungültig oder abgelaufen" in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter(User.email.like("team%@example.com")).count() == 2
        # Einladung im Modus „Freigabe“ überspringt die Prüfung
    _set_mode(app, "approval")
    login(client, "admin@test.local", "AdminPass12345")
    client.post("/admin/einladungen", data={"emails": "", "max_uses": "1"})
    with app.app_context():
        tok = Invite.query.filter_by(email="", uses=0).first().token
    client.post("/logout")
    client.post("/registrieren", data=_reg_data("vip@example.com", einladung=tok))
    with app.app_context():
        assert User.query.filter_by(email="vip@example.com").one().status == "active"


def test_invites_are_club_local_and_revocable(client, app):
    from app.models import Invite
    login(client, "admin@test.local", "AdminPass12345")
    client.post("/admin/einladungen", data={"emails": "", "max_uses": "5"})
    with app.app_context():
        inv = Invite.query.execution_options(all_clubs=True).one()
        tok, iid = inv.token, inv.id
    client.post("/logout")
    _set_mode(app, "invite", slug="berlin")
    # Token aus Klub A gilt in Klub B nicht
    r = client.post("/registrieren", base_url=f"http://{B_HOST}", data=_reg_data("b@example.com", einladung=tok))
    assert "ungültig oder abgelaufen" in r.get_data(as_text=True)
    _set_mode(app, "invite")
    login(client, "admin@test.local", "AdminPass12345")
    client.post(f"/admin/einladungen/{iid}/loeschen")
    client.post("/logout")
    r = client.post("/registrieren", data=_reg_data("c@example.com", einladung=tok))
    assert "ungültig oder abgelaufen" in r.get_data(as_text=True)


def test_welcome_mail_uses_club_address(client, app, monkeypatch):
    sent = []
    import app.blueprints.auth as auth
    monkeypatch.setattr(auth, "send_mail", lambda to, subject, template, **ctx: sent.append((template, ctx)) or True)
    register(client, "hallo-b@example.com", base=f"http://{B_HOST}")
    link = next(ctx["link"] for t, ctx in sent if t == "welcome")
    assert link.startswith(f"http://{B_HOST}/") and link.endswith("/app/profil")


def test_admin_onboarding_checklist_tracks_progress(client, app):
    login(client, "admin@test.local", "AdminPass12345")
    page = client.get("/admin/").get_data(as_text=True)
    assert "Erste Schritte für" in page and "0 von 7 erledigt" in page
    client.post("/admin/klub", data={"name": "Mein Klub", "full_name": "Mein Klub e. V.", "accent": "#112233",
                                     "operator": "Mein Klub e. V., Musterweg 1", "contact_email": "info@mein.example",
                                     "impressum_text": "Impressum Mein Klub e. V.", "registration": "approval"})
    client.post("/admin/einladungen", data={"emails": "", "max_uses": "3"})
    client.post("/admin/assistent", data={"assistant_name": "Mira", "about": "Wir sind ein Klub.", "assistant_facts": "",
                                          "aiko_extra_instructions": ""})
    page = client.get("/admin/").get_data(as_text=True)
    assert "6 von 7 erledigt" in page                      # nur der erste eigene Termin fehlt noch
    client.post("/admin/termine/neu", data={"title": "Eröffnung", "format": "community", "date": "2030-02-01",
                                            "time": "18:00", "status": "published"})
    assert "Erste Schritte für" not in client.get("/admin/").get_data(as_text=True)   # alles erledigt


def test_event_reminders_once_per_registration(app, monkeypatch):
    from datetime import timedelta
    from app.models import Event, Notification, Profile, Registration, utcnow
    from app.services import reminders
    sent = []
    monkeypatch.setattr(reminders, "send_mail", lambda to, subject, template, **ctx: sent.append((to, subject, ctx)) or True)
    with app.app_context(), use_club(Club.query.filter_by(slug="klub").one()):
        from app.seed import _seed_demo_members
        _seed_demo_members()
        users = User.query.filter(User.email.like("%@demo.klub.local")).limit(4).all()
        soon = Event(title="Morgen-Treffen", format="community", source="admin", status="published",
                     starts_at=utcnow() + timedelta(hours=20))
        later = Event(title="Nächste Woche", format="community", source="admin", status="published",
                      starts_at=utcnow() + timedelta(days=6))
        db.session.add_all([soon, later])
        db.session.flush()
        for u in users:
            for e in (soon, later):
                db.session.add(Registration(event_id=e.id, user_id=u.id, status="registered"))
        users[3].profile.event_reminders = False        # abbestellt: Marke ja, Mail nein
        db.session.commit()
        assert reminders.send_event_reminders() == 4
        assert len(sent) == 3 and all(s[2]["ev"].title == "Morgen-Treffen" for s in sent)
        assert all(s[2]["link"].startswith("http") and s[2]["link"].endswith(f"/app/termine/{soon.id}") for s in sent)
        assert reminders.send_event_reminders() == 0 and len(sent) == 3   # keine Doppelmail
        assert Notification.query.filter_by(kind="event_reminder").count() == 4
        # Vorbereitung: höchstens drei andere Angemeldete, nie die Person selbst
        for to, _subject, ctx in sent:
            assert len(ctx["people"]) <= 3 and not any(p.startswith(ctx["user"].first_name + " ") for p in ctx["people"])
