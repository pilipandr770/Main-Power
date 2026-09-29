"""KI-Pfade des Fragebogens mit simuliertem Modell: Antworten werden geprüft, bereinigt und robust verarbeitet."""
import json

from app.models import User
from app.tenancy import default_club, use_club
from tests.test_smoke import app, client, login, register  # noqa: F401  (Fixtures)

DEMO = "demo.main-power.local"


def fake_llm(monkeypatch, module, answer, seen=None):
    def complete(system, messages, **kw):
        if seen is not None:
            seen.append({"system": system, "content": messages[-1]["content"], **kw})
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(module, "complete", complete)


def test_interview_ai_extracts_several_fields_and_drops_invalid(client, app, monkeypatch):
    from app.services import interview
    fake_llm(monkeypatch, interview, {
        "q_focus": "Plattform, die Handwerksbetriebe mit Azubis zusammenbringt.",
        "role": "gruender", "partner_types": ["investor", "mentor", "astronaut"], "stage": "raketenphase",
        "email": "hack@example.com", "goal_12m": ""})
    register(client)
    d = client.post("/app/api/interview/antwort",
                    json={"field": "q_focus", "answer": "Ich bin Gründer … suche Investorin und Mentor"}).get_json()
    assert d["ai"] is True
    got = {p["field"]: p["value"] for p in d["proposals"]}
    assert got == {"q_focus": "Plattform, die Handwerksbetriebe mit Azubis zusammenbringt.", "role": "gruender",
                   "partner_types": "investor,mentor"}
    assert d["proposals"][0]["field"] == "q_focus"   # gefragtes Feld zuerst


def test_interview_ai_garbage_falls_back_to_asked_field(client, app, monkeypatch):
    from app.services import interview
    fake_llm(monkeypatch, interview, "Das kann ich leider nicht beantworten.")
    register(client)
    d = client.post("/app/api/interview/antwort", json={"field": "q_can_help", "answer": "Buchhaltung"}).get_json()
    assert d["ai"] is False and d["proposals"][0]["value"] == "Buchhaltung"


def test_goal_feedback_uses_ai_and_only_own_private_data(client, app, monkeypatch):
    from app.services import goals
    seen = []
    fake_llm(monkeypatch, goals, "  Stark! Nächster Schritt: Sprich mit Nadine.  ", seen)
    login(client, f"tobias.kern@{DEMO}", "demo-passwort-123")
    client.post("/app/ziel", data={"status": "auf_kurs", "note": "Drei Gespräche geführt"})
    with app.app_context(), use_club(default_club()):
        from app.models import GoalCheckin
        u = User.query.filter_by(email=f"tobias.kern@{DEMO}").one()
        assert GoalCheckin.query.filter_by(user_id=u.id).one().ai_feedback == "Stark! Nächster Schritt: Sprich mit Nadine."
    payload = json.loads(seen[0]["content"])
    assert payload["check_in"]["notiz"] == "Drei Gespräche geführt"
    assert payload["profil"]["schon_versucht"]            # eigene private Angaben: ja
    others = json.dumps(payload["passende_mitglieder"], ensure_ascii=False)
    assert "herausforderung" not in others and "ziel" not in others  # fremde private Angaben: nein
    assert seen[0]["purpose"] == "goal_checkin"


def test_club_report_ai_parsed_and_payload_anonymous(client, app, monkeypatch):
    from app.services import club_insights
    seen = []
    fake_llm(monkeypatch, club_insights, "```json\n" + json.dumps({
        "nachfrage": [{"thema": "Finanzierung", "anzahl": 3}], "angebot": [{"thema": "Steuern", "anzahl": 2}],
        "luecken": [{"thema": "Kapital", "hinweis": "Wenige Investor:innen."}],
        "ideen": [{"titel": "Investor:innen-Abend", "format": "hub", "warum": "Nachfrage nach Kapital."}]}) + "\n```",
        seen)
    with app.app_context(), use_club(default_club()):
        report = club_insights.build_report()
    assert report["ai"] is True and report["ideen"][0]["titel"] == "Investor:innen-Abend"
    content = seen[0]["content"]
    for name in ("Tobias", "Julia", "Nadine", "Kern", "demo.main-power.local"):
        assert name not in content                      # keine Namen/E-Mails an das Modell
    assert "Förderanträge" not in content and "Network-Marketing" not in content  # keine privaten Felder


def test_interview_ai_labels_are_mapped_to_keys(client, app, monkeypatch):
    from app.services import interview
    fake_llm(monkeypatch, interview, {"role": "Gründer/in", "partner_types": ["Investor/in, Finanzierung", "Mentorin"],
                                      "languages": "Deutsch, Englisch"})
    register(client)
    d = client.post("/app/api/interview/antwort", json={"field": "role", "answer": "…"}).get_json()
    got = {p["field"]: p["value"] for p in d["proposals"]}
    assert got["role"] == "gruender" and got["partner_types"] == "investor,mentor" and got["languages"] == "de,en"
