"""Nachbesserungen aus dem Feature-Review: Sichtbarkeit, Datenminimierung, White-Label-Texte, E-Mail-Absender."""
import os
from datetime import timedelta

from app.extensions import db
from app.models import ClubQuestion, KnowledgeItem, Notification, PairInsight, Service, User, utcnow
from app.tenancy import default_club, use_club
from tests.test_smoke import app, client, login, register  # noqa: F401  (Fixtures)

DEMO = "demo.main-power.local"


def admin_login(client):
    return login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
                 os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))


def by(first):
    return User.query.filter(User.email.like(f"{first}.%@{DEMO}")).one()


def test_goal_category_follows_goal_visibility(client, app):
    from app.services.matching import card
    with app.app_context(), use_club(default_club()):
        daniel, nadine = by("daniel"), by("nadine")
        assert daniel.profile.goal_category == "digital" and not daniel.profile.is_public("goal_12m")
        assert "ziel_kategorie" not in card(daniel.profile)          # Ziel privat -> Kategorie auch
        assert card(daniel.profile, own=True)["ziel_kategorie"]
        nadine.profile.goal_category = "kunden"
        db.session.commit()
        assert card(nadine.profile)["ziel_kategorie"]                  # Ziel freigegeben
        did = daniel.id
    login(client, f"julia.wagner@{DEMO}", "demo-passwort-123")
    assert "Digitalisierung / Effizienz" not in client.get(f"/app/mitglieder/{did}").get_data(as_text=True)


def test_club_question_answers_in_ai_card_and_delete_removes_answers(client, app):
    from app.services.matching import card
    with app.app_context(), use_club(default_club()):
        db.session.add_all([
            ClubQuestion(key="verband", label="Verband?", kind="choice", options=["IHK", "BVMW"], use="offer", public=True),
            ClubQuestion(key="intern", label="Intern?", kind="text", use="none", public=False)])
        t = by("tobias")
        t.profile.custom_answers = {"verband": "IHK", "intern": "geheim"}
        db.session.commit()
        assert card(t.profile)["klubfragen"] == {"Verband?": "IHK"}
        assert card(t.profile, own=True)["klubfragen"] == {"Verband?": "IHK", "Intern?": "geheim"}
        qid, tid = ClubQuestion.query.filter_by(key="verband").one().id, t.id
    admin_login(client)
    r = client.post(f"/admin/fragen/{qid}/loeschen", follow_redirects=True)
    assert "1 Antworten entfernt" in r.get_data(as_text=True)
    with app.app_context(), use_club(default_club()):
        assert db.session.get(User, tid).profile.custom_answers == {"intern": "geheim"}
        assert ClubQuestion.query.filter_by(key="verband").count() == 0


def test_goal_reminder_email_can_be_unsubscribed(client, app, monkeypatch):
    sent = []
    import app.services.mailer as mailer
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, *a, **k: sent.append(to) or True)
    login(client, f"markus.albrecht@{DEMO}", "demo-passwort-123")
    client.post("/app/privatsphaere", data={"matching": "1", "directory": "1"})     # goal_reminders abgewählt
    with app.app_context(), use_club(default_club()):
        m = by("markus")
        assert m.profile.goal_reminders is False
        m.profile.milestone_90d = "Zwei Workshops"
        m.profile.milestone_set_at = utcnow() - timedelta(days=100)
        db.session.commit()
        from app.services.goals import send_reminders
        send_reminders()
        assert Notification.query.filter_by(user_id=m.id, kind="goal_checkin").count() == 1  # in der Plattform ja
        assert m.email not in sent                                                            # per E-Mail nein
        assert f"tobias.kern@{DEMO}" in sent                                                  # andere weiterhin


def test_mail_sender_uses_club_name_and_reply_to(app, monkeypatch):
    captured = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): captured.append(msg)

    import app.services.mailer as mailer
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    app.config.update(SMTP_HOST="smtp.test", SMTP_FROM="Plattform <noreply@plattform.test>")
    with app.app_context(), use_club(default_club()):
        from app.services import club as club_settings
        club_settings.save({"name": "Klub Nord", "contact_email": "hallo@klub-nord.test"})
        assert mailer.send_mail("x@example.com", "Test", "welcome", user=by("tobias"), link="http://x")
    assert captured[0]["From"] == "Klub Nord <noreply@plattform.test>"
    assert captured[0]["Reply-To"] == "hallo@klub-nord.test"


def test_renaming_assistant_updates_service_and_faq_texts(client, app):
    admin_login(client)
    page = client.get("/admin/klub").get_data(as_text=True)
    assert "assistant_name" in page
    with app.app_context(), use_club(default_club()):
        from app.services import club as club_settings
        values = {k: club_settings.settings()[k] for k in club_settings.TEXT_FIELDS}
        n_services = Service.query.filter(Service.summary.like("%Aiko%")).count()
        assert n_services >= 1
    values["assistant_name"] = "Mira"
    r = client.post("/admin/klub", data=values, follow_redirects=True)
    assert "Mira" in r.get_data(as_text=True)
    with app.app_context(), use_club(default_club()):
        assert Service.query.filter(Service.summary.like("%Aiko%")).count() == 0
        assert Service.query.filter(Service.summary.like("%Mira%")).count() == n_services
        assert KnowledgeItem.query.filter(KnowledgeItem.answer.like("%Aiko%")).count() == 0
    assert "Mira" in client.get("/app/profil").get_data(as_text=True)


def test_account_deletion_removes_cached_pair_insights(client, app):
    register(client)
    with app.app_context(), use_club(default_club()):
        me, julia = User.query.filter_by(email="neu@example.com").one(), by("julia")
        db.session.add_all([PairInsight(user_id=me.id, other_id=julia.id, version="v", data={}, ai=False),
                            PairInsight(user_id=julia.id, other_id=me.id, version="v", data={}, ai=False)])
        db.session.commit()
        uid = me.id
    client.post("/app/privatsphaere/loeschen", data={"password": "sehr-sicheres-pw"})
    with app.app_context(), use_club(default_club()):
        assert db.session.get(User, uid) is None
        assert PairInsight.query.filter((PairInsight.user_id == uid) | (PairInsight.other_id == uid)).count() == 0
