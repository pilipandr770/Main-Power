"""Erweiterter Fragebogen: Speichern, Sichtbarkeit privater Angaben, hybrides Matching und Ausschlüsse."""
from types import SimpleNamespace

from app.extensions import db
from app.models import User
from app.questionnaire import structured_fit
from app.tenancy import default_club, use_club
from tests.test_smoke import app, client, login, register  # noqa: F401  (Fixtures)

DEMO = "demo.main-power.local"


def _profile(**kw):
    base = dict(role="", stage="", partner_types="", resources="", help_mode="", goal_category="", languages="",
                meeting_mode="", city="", work_values="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_profile_saves_questionnaire_and_drops_invalid_values(client, app):
    register(client)
    body = client.get("/app/profil?welcome=1").get_data(as_text=True)
    assert "2 · Dein Ziel" in body and "Wen suchst du gerade?" in body and "Matching-Qualität" in body
    client.post("/app/profil", data={
        "first_name": "Nina", "role": "gruender", "stage": "erfunden", "languages": ["de", "en", "xx"],
        "partner_types": ["investor", "mentor", "peers", "kunden", "team"], "goal_category": "finanzierung",
        "goal_12m": "Seed-Runde abschließen", "not_wanted": "Versicherungen", "public_fields": ["goal_12m", "bio"],
        "resources": ["knowhow"], "q_can_help": "Produktentwicklung"})
    with app.app_context():
        p = User.query.filter_by(email="neu@example.com").one().profile
        assert p.role == "gruender" and p.stage == ""              # unbekannte Option verworfen
        assert p.languages == "de,en"
        assert p.partner_types == "investor,mentor,peers,kunden"   # höchstens vier
        assert p.public_fields == "goal_12m"                        # nur freigebbare Felder
        assert p.embed_avoid is not None and p.is_matchable
        from app.services.gdpr import export_user
        assert export_user(p.user)["profil"]["goal_12m"] == "Seed-Runde abschließen"


def test_private_fields_stay_hidden_from_other_members_and_ai_cards(client, app):
    with app.app_context(), use_club(default_club()):
        t = User.query.filter_by(email=f"tobias.kern@{DEMO}").one()
        tid = t.id
        assert t.profile.goal_12m and t.profile.milestone_90d and t.profile.not_wanted
        from app.services.matching import card
        other_view, own_view = card(t.profile), card(t.profile, own=True)
        assert "ziel" in other_view                     # freigegeben (public_fields)
        assert "herausforderung" not in other_view and "schon_versucht" not in other_view
        assert own_view["herausforderung"] and own_view["schon_versucht"]
        assert all("Versicherungsmakler" not in str(v) for v in own_view.values())  # nie im KI-Kontext
    login(client, f"julia.wagner@{DEMO}", "demo-passwort-123")
    html = client.get(f"/app/mitglieder/{tid}").get_data(as_text=True)
    assert "Seed-Runde" in html                          # Ziel freigegeben
    assert "Pitch-Deck fertig" not in html               # Meilenstein privat
    assert "Förderanträge" not in html                   # „Schon versucht“ privat
    assert "Network-Marketing" not in html               # Ausschlüsse nie sichtbar
    assert "Gründer/in" in html and "Investor/in, Finanzierung" in html


def test_structured_fit_prefers_complementary_partners():
    founder = _profile(role="gruender", stage="gruendung", partner_types="investor,mentor",
                       goal_category="finanzierung", languages="de,en")
    investor = _profile(role="investor", stage="etabliert", partner_types="startups", resources="kapital",
                        help_mode="mentoring", languages="de")
    designer = _profile(role="selbstaendig", stage="gruendung", partner_types="kunden", languages="fr")
    assert structured_fit(founder, investor) > 0.9
    assert structured_fit(founder, designer) < 0.4
    assert structured_fit(_profile(), investor) is None  # ohne Angaben kein struktureller Anteil


def test_matching_uses_questionnaire_and_respects_exclusions(app):
    from app.services import matching
    with app.app_context(), use_club(default_club()):
        tobias = User.query.filter_by(email=f"tobias.kern@{DEMO}").one()
        names = [m.other.first_name for m in matching.top_matches(tobias, k=20, explain=False)]
        assert names[0] == "Nadine"          # Investorin: Gründer sucht Finanzierung
        assert "Stefan" not in names         # Versicherungsmakler: von Tobias ausgeschlossen
        stefan = User.query.filter_by(email=f"stefan.richter@{DEMO}").one()
        assert "Tobias" not in [m.other.first_name for m in matching.top_matches(stefan, k=20, explain=False)]
        # Begründung ohne KI nennt strukturelle Gründe
        m = matching.top_matches(tobias, k=1)[0]
        assert "Außerdem: Nadine passt zu „Investor/in, Finanzierung“" in m.reason
        db.session.rollback()
