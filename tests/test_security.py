"""White-Team-Audit: Sitzungen, Kontoübernahme, KI-Notbremse."""
import pytest
from flask import g

from app.extensions import db
from app.models import LLMUsage, PlatformSetting, User
from app.tenancy import default_club, use_club
from tests.test_review_fixes import DEMO, admin_login, by
from tests.test_smoke import app, client, login  # noqa: F401  (Fixtures)

PW = "demo-passwort-123"


@pytest.fixture(autouse=True)
def _no_cached_user(app):
    """Die Fixture hält einen App-Kontext offen; Flask-Login cacht den Nutzer sonst in g über Requests hinweg."""
    app.before_request_funcs.setdefault(None, []).insert(0, lambda: g.pop("_login_user", None) and None)


def _logged_in(c) -> bool:
    return c.get("/app/", follow_redirects=False).status_code == 200


def test_password_change_ends_other_sessions_but_keeps_current(app):
    a, b = app.test_client(), app.test_client()
    login(a, f"julia.wagner@{DEMO}", PW)
    login(b, f"julia.wagner@{DEMO}", PW)
    assert _logged_in(a) and _logged_in(b)
    a.post("/app/privatsphaere/passwort", data={"current": PW, "new": "ganz-neues-passwort"})
    assert _logged_in(a)
    assert not _logged_in(b)


def test_blocked_member_loses_running_session(app):
    member, admin = app.test_client(), app.test_client()
    login(member, f"julia.wagner@{DEMO}", PW)
    assert _logged_in(member)
    with app.app_context(), use_club(default_club()):
        uid = by("julia").id
    admin_login(admin)
    admin.post(f"/admin/mitglieder/{uid}/sperren")
    assert not _logged_in(member)


def test_legacy_session_id_without_generation_is_rejected(app, client):
    login(client, f"julia.wagner@{DEMO}", PW)
    with client.session_transaction() as s:
        s["_user_id"] = s["_user_id"].split(":")[0]
    assert not _logged_in(client)


def test_moderator_cannot_change_member_email(app, client):
    with app.app_context(), use_club(default_club()):
        julia = by("julia")
        uid, email, first = julia.id, julia.email, julia.first_name
    login(client, f"lisa.team@{DEMO}", PW)
    r = client.post(f"/admin/mitglieder/{uid}/bearbeiten",
                    data={"email": "angreifer@example.com", "first_name": first}, follow_redirects=True)
    assert "nur die Klubleitung" in r.get_data(as_text=True)
    with app.app_context(), use_club(default_club()):
        assert db.session.get(User, uid).email == email


def test_daily_cap_stops_free_ai(app):
    from app.services import plans
    from app.services.llm import LLMUnavailable
    with app.test_request_context(), use_club(default_club()):
        PlatformSetting.set("ai_daily_cap_cents", "1")
        db.session.add(LLMUsage(purpose="aiko_public", input_tokens=10_000, output_tokens=10_000))
        db.session.commit()
        assert plans.free_spend_today_cents() >= 1
        try:
            plans.guard_llm()
            raise AssertionError("Notbremse hat nicht gegriffen")
        except LLMUnavailable:
            pass
        PlatformSetting.set("ai_daily_cap_cents", "0")  # 0 = aus
        db.session.commit()
        plans.guard_llm()
