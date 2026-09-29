"""Schritt 2 des Fragebogens: Profil-Interview, 90-Tage-Check-ins, klubeigene Fragen, Auswertung für die Klubleitung."""
import json
import os
from datetime import timedelta

from app.extensions import db
from app.models import ClubQuestion, GoalCheckin, Notification, Setting, User, utcnow
from app.tenancy import default_club, use_club
from tests.test_smoke import app, client, login, register  # noqa: F401  (Fixtures)

DEMO = "demo.main-power.local"


def admin_login(client):
    return login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
                 os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))


def test_profile_interview_proposes_and_saves_only_confirmed_values(client, app):
    register(client)
    assert "data-interview" in client.get("/app/profil/interview").get_data(as_text=True)
    q = client.post("/app/api/interview/weiter", json={}).get_json()["question"]
    assert q["field"] == "q_focus" and q["done"] == 0

    # Ohne API-Schlüssel: Antwort landet im gefragten Feld, noch nichts gespeichert
    d = client.post("/app/api/interview/antwort", json={"field": "q_focus", "answer": "Ich baue Apps für Arztpraxen."}).get_json()
    assert d["ai"] is False and d["proposals"][0] == {"field": "q_focus", "label": "Was du machst",
                                                      "value": "Ich baue Apps für Arztpraxen.",
                                                      "display": "Ich baue Apps für Arztpraxen.", "private": False}
    with app.app_context():
        assert User.query.filter_by(email="neu@example.com").one().profile.q_focus == ""

    # Auswahlfelder werden über ihre Bezeichnungen erkannt
    d = client.post("/app/api/interview/antwort", json={"field": "role", "answer": "Ich bin Gründerin"}).get_json()
    assert d["proposals"][0]["value"] == "gruender"
    d = client.post("/app/api/interview/antwort",
                    json={"field": "partner_types", "answer": "Eine Investorin und eine Mentorin"}).get_json()
    assert set(d["proposals"][0]["value"].split(",")) == {"investor", "mentor"}

    r = client.post("/app/api/interview/uebernehmen",
                    json={"values": {"q_focus": "Ich baue Apps für Arztpraxen.", "role": "gruender",
                                     "partner_types": ["investor", "mentor"], "stage": "gibt-es-nicht",
                                     "email": "boese@example.com"}, "skip": ["goal_12m"]}).get_json()
    assert set(r["saved"]) == {"q_focus", "role", "partner_types"}
    assert r["question"]["field"] == "q_looking_for"          # goal_12m übersprungen
    with app.app_context():
        u = User.query.filter_by(email="neu@example.com").one()
        assert u.profile.role == "gruender" and u.profile.stage == "" and u.email == "neu@example.com"


def test_goal_checkin_reminder_and_feedback(client, app):
    register(client)
    client.post("/app/profil", data={"first_name": "Nina", "goal_12m": "20 Stammkund:innen",
                                     "milestone_90d": "Drei Pilotkund:innen", "q_can_help": "Design"})
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        assert u.profile.milestone_set_at is not None
        from app.services import goals
        assert goals.days_left(u.profile) >= 89 and not goals.is_due(u.profile)
        u.profile.milestone_set_at = utcnow() - timedelta(days=91)
        db.session.commit()
        assert goals.is_due(u.profile)
        uid = u.id
    runner = app.test_cli_runner()                    # wie der tägliche Cron, ohne Request-Kontext
    first = runner.invoke(args=["goal-checkins"])
    assert first.exit_code == 0 and "mainpower:" in first.output and "0 Erinnerungen" not in first.output
    assert "mainpower: 0 Erinnerungen" in runner.invoke(args=["goal-checkins"]).output  # nur einmal pro Zeitraum
    with app.app_context(), use_club(default_club()):
        assert Notification.query.filter_by(user_id=uid, kind="goal_checkin").count() == 1
    html = client.get("/app/").get_data(as_text=True)
    assert "Check-in fällig" in html
    client.post("/app/ziel", data={"status": "erreicht", "note": "Zwei unterschrieben",
                                   "next_milestone": "Zehn zahlende Kund:innen"})
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        c = GoalCheckin.query.filter_by(user_id=u.id).one()
        assert c.milestone == "Drei Pilotkund:innen" and c.status == "erreicht" and c.ai_feedback
        assert u.profile.milestone_90d == "Zehn zahlende Kund:innen"
        from app.services import goals
        assert not goals.is_due(u.profile)
        assert Notification.query.filter_by(user_id=u.id, kind="goal_checkin", read_at=None).count() == 0
        from app.services.gdpr import export_user
        assert export_user(u)["ziel_check_ins"][0]["status"] == "Erreicht"
    page = client.get("/app/ziel").get_data(as_text=True)
    assert "Zehn zahlende" in page and "Glückwunsch" in page
    # Andere Mitglieder sehen weder Ziel noch Check-ins
    client.post("/logout")
    login(client, f"julia.wagner@{DEMO}", "demo-passwort-123")
    with app.app_context():
        uid = User.query.filter_by(email="neu@example.com").one().id
    other = client.get(f"/app/mitglieder/{uid}").get_data(as_text=True)
    assert "Zehn zahlende" not in other and "Zwei unterschrieben" not in other


def test_club_questions_profile_matching_export(client, app):
    admin_login(client)
    r = client.post("/admin/fragen/neu", data={"label": "Welche Branchenverbände?", "kind": "multi",
                                               "options": "IHK\nBVMW\nBitkom", "use": "offer", "public": "1",
                                               "active": "1"}, follow_redirects=True)
    assert "Frage gespeichert" in r.get_data(as_text=True)
    assert client.post("/admin/fragen/neu", data={"label": "Nur eine Option", "kind": "choice", "options": "A"}
                       ).status_code == 200  # Validierung: mind. zwei Optionen
    with app.app_context(), use_club(default_club()):
        q = ClubQuestion.query.one()
        assert q.key == "welche-branchenverbaende" and q.option_list == ["IHK", "BVMW", "Bitkom"]
        from app.club_presets import export_config
        cfg = export_config()
        assert cfg["questions"][0]["label"] == "Welche Branchenverbände?"
    client.post("/logout")
    register(client)
    body = client.get("/app/profil").get_data(as_text=True)
    assert "Fragen von" in body and "Bitkom" in body
    client.post("/app/profil", data={"first_name": "Nina", "q_can_help": "Design",
                                     "custom_welche-branchenverbaende": ["Bitkom", "Erfunden"]})
    with app.app_context(), use_club(default_club()):
        p = User.query.filter_by(email="neu@example.com").one().profile
        assert p.custom_answers == {"welche-branchenverbaende": ["Bitkom"]}
        assert "Welche Branchenverbände?: Bitkom" in p.offer_text
        uid = p.user_id
    client.post("/logout")
    login(client, f"julia.wagner@{DEMO}", "demo-passwort-123")
    assert "Bitkom" in client.get(f"/app/mitglieder/{uid}").get_data(as_text=True)


def test_club_analytics_counts_and_keeps_private_texts_out(client, app):
    admin_login(client)
    html = client.get("/admin/auswertung").get_data(as_text=True)
    assert "Partnersuche: Nachfrage und Angebot" in html and "Investor/in, Finanzierung" in html
    with app.app_context(), use_club(default_club()):
        from app.services.club_insights import overview
        ov = overview()
        inv = next(r for r in ov["market"] if r["key"] == "investor")
        assert inv["demand"] >= 1 and inv["supply"] >= 1       # Tobias sucht, Nadine bietet
        assert ov["with_orders"] >= 2                           # Daniel, Kerem
    r = client.post("/admin/auswertung/analyse", follow_redirects=True)
    assert "Themen" in r.get_data(as_text=True)
    with app.app_context(), use_club(default_club()):
        raw = Setting.get("insights.club_report")
        report = json.loads(raw)
        assert report["ai"] is False and report["basis"]["mitglieder"] >= 10
        # private Angaben (Schon versucht, Meilenstein, Ausschlüsse) tauchen nicht auf
        assert "Förderanträge" not in raw and "Pitch-Deck fertig" not in raw and "Network-Marketing" not in raw
    idea_page = client.get("/admin/termine/neu?title=Investor%3Ainnen-Abend&format=hub").get_data(as_text=True)
    assert "Investor:innen-Abend" in idea_page


def test_directory_filters_by_role_and_resources(client, app):
    login(client, f"julia.wagner@{DEMO}", "demo-passwort-123")
    html = client.get("/app/mitglieder?rolle=investor").get_data(as_text=True)
    assert "Nadine S." in html and "Markus A." not in html
    html = client.get("/app/mitglieder?bringt=auftraege").get_data(as_text=True)
    assert "Daniel H." in html and "Kerem Y." in html and "Nadine S." not in html
    assert "Nadine S." in client.get("/app/mitglieder?rolle=unsinn").get_data(as_text=True)  # ungültig = kein Filter
    with app.app_context(), use_club(default_club()):
        from app.services.club_insights import overview
        coop = next(r for r in overview()["market"] if r["key"] == "kooperation")
        assert coop["mutual"] and coop["gap"] == 0


def test_admin_edits_questionnaire_but_not_private_goals(client, app):
    with app.app_context(), use_club(default_club()):
        t = User.query.filter_by(email=f"tobias.kern@{DEMO}").one()
        tid, goal, tried = t.id, t.profile.goal_12m, t.profile.q_tried
    admin_login(client)
    page = client.get(f"/admin/mitglieder/{tid}/bearbeiten").get_data(as_text=True)
    assert "Fragebogen" in page and 'name="role"' in page and 'name="goal_12m"' not in page
    client.post(f"/admin/mitglieder/{tid}/bearbeiten", data={
        "first_name": "Tobias", "last_name": "Kern", "email": f"tobias.kern@{DEMO}", "role": "inhaber",
        "partner_types": ["investor"], "proud_of": "Korrigiert", "goal_12m": "Überschrieben?"})
    with app.app_context(), use_club(default_club()):
        p = db.session.get(User, tid).profile
        assert p.role == "inhaber" and p.partner_types == "investor" and p.proud_of == "Korrigiert"
        assert p.goal_12m == goal and p.q_tried == tried        # private Angaben unverändert


def test_init_db_refreshes_only_untouched_default_faq(app):
    from app.models import KnowledgeItem
    from app.seed import OUTDATED_FAQ, refresh_default_faq
    with app.app_context(), use_club(default_club()):
        _, question, old = OUTDATED_FAQ[0]
        item = KnowledgeItem.query.filter_by(question=question).first()
        item.answer = old
        db.session.add(KnowledgeItem(question="Wie werde ich Mitglied?", answer="Eigener Text des Klubs", public=True))
        db.session.commit()
        assert refresh_default_faq() == 1
        assert "vier Fragen" not in KnowledgeItem.query.filter_by(question=question).first().answer
        assert KnowledgeItem.query.filter_by(answer="Eigener Text des Klubs").count() == 1
        assert refresh_default_faq() == 0
