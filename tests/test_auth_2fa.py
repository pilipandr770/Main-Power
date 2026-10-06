"""E-Mail-Bestätigung, Passwort-Reset und Zwei-Faktor-Anmeldung."""
import re
import time

import pytest

from app.extensions import db
from app.models import User
from app.services import totp
from app.tenancy import default_club, use_club
from tests.test_review_fixes import DEMO, by
from tests.test_security import PW, _logged_in, _no_cached_user  # noqa: F401  (autouse-Fixture)
from tests.test_smoke import app, client, login  # noqa: F401  (Fixtures)

ADMIN = ("admin@test.local", "AdminPass12345")


@pytest.fixture()
def mails(monkeypatch):
    """Fängt verschickte Mails ab (auth und member nutzen send_mail aus services.mailer)."""
    sent = []

    def fake(to, subject, template, **ctx):
        sent.append({"to": to, "subject": subject, "template": template, **ctx})
        return True
    import app.blueprints.auth as auth_bp
    import app.services.mailer as mailer
    monkeypatch.setattr(auth_bp, "send_mail", fake)
    monkeypatch.setattr(mailer, "send_mail", fake)
    return sent


def _token(link: str) -> str:
    return link.rsplit("/", 1)[1]


def _reg(email="neu@example.com"):
    return {"first_name": "Nina", "last_name": "Neu", "email": email, "password": "sehr-sicheres-pw",
            "consent_privacy": "1", "consent_values": "1"}


def test_totp_matches_rfc6238_vector_and_blocks_replay():
    s = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # Testschlüssel aus RFC 6238 (ASCII „12345678901234567890“)
    assert totp.code_at(s, 59) == "287082"
    step = totp.verify(s, "287082", 0, now=59)
    assert step == 1 and totp.verify(s, "287082", step, now=59) is None   # derselbe Code gilt nur einmal
    assert totp.verify(s, "000000", 0, now=59) is None and totp.verify(s, "12", 0, now=59) is None


def test_email_verification_flow(app, client, mails):
    client.post("/registrieren", data=_reg(), follow_redirects=True)
    verify = next(m for m in mails if m["template"] == "verify")
    with app.app_context():
        assert User.query.filter_by(email="neu@example.com").one().email_verified_at is None
    r = client.get(f"/e-mail-bestaetigen/{_token(verify['link'])}", follow_redirects=True)
    assert "bestätigt" in r.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(email="neu@example.com").one().email_verified_at is not None
    # Link einer alten Adresse gilt nicht mehr, wenn die Adresse geändert wurde
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        u.email = "geaendert@example.com"
        db.session.commit()
    db.session.expire_all()                       # die Fixture teilt ihre Sitzung mit den Requests
    r = client.get(f"/e-mail-bestaetigen/{_token(verify['link'])}", follow_redirects=True)
    assert "anderen E-Mail-Adresse" in r.get_data(as_text=True)
    assert "ungültig" in client.get("/e-mail-bestaetigen/kaputt", follow_redirects=True).get_data(as_text=True)


def test_invite_to_same_address_counts_as_verified(app, client, mails):
    from app.services import invites
    with app.app_context(), use_club(default_club()):
        inv = invites.create("eingeladen@example.com", "Hallo", 7, 1)
        db.session.commit()
        tok = inv.token
    client.post("/registrieren", data=dict(_reg("eingeladen@example.com"), einladung=tok), follow_redirects=True)
    with app.app_context():
        assert User.query.filter_by(email="eingeladen@example.com").one().email_verified_at is not None
    assert not [m for m in mails if m["template"] == "verify"]


def test_password_reset_flow_verifies_mailbox_and_ends_sessions(app, client, mails):
    app.config["SMTP_HOST"] = "smtp.test"          # blendet „Passwort vergessen“ ein
    other = app.test_client()
    login(other, f"julia.wagner@{DEMO}", PW)
    assert _logged_in(other)
    assert "Passwort vergessen" in client.get("/login").get_data(as_text=True)
    client.post("/passwort-vergessen", data={"email": f"julia.wagner@{DEMO}"})
    link = next(m["link"] for m in mails if m["template"] == "reset")
    r = client.post("/passwort-vergessen", data={"email": "gibt-es-nicht@example.com"}, follow_redirects=True)
    assert "Wenn ein Konto" in r.get_data(as_text=True)      # unbekannte Adresse verrät nichts
    assert len([m for m in mails if m["template"] == "reset"]) == 1
    assert "mindestens 10" in client.post(f"/passwort-neu/{_token(link)}", data={"password": "kurz"}).get_data(as_text=True)
    r = client.post(f"/passwort-neu/{_token(link)}", data={"password": "ganz-neues-passwort"}, follow_redirects=True)
    assert "Passwort geändert" in r.get_data(as_text=True)
    assert any(m["template"] == "password_changed" for m in mails)
    assert not _logged_in(other)                                                # alte Sitzung beendet
    assert "bereits verwendet" in client.get(f"/passwort-neu/{_token(link)}", follow_redirects=True).get_data(as_text=True)
    login(client, f"julia.wagner@{DEMO}", "ganz-neues-passwort")
    assert _logged_in(client)


def _enable_2fa(client, password_page="/app/sicherheit"):
    """Richtet 2FA für den angemeldeten Client ein, gibt (Secret, Wiederherstellungscodes) zurück."""
    page = client.get(password_page + "?einrichten=1").get_data(as_text=True)
    assert "data:image/svg+xml" in page                                          # QR-Code
    secret = re.search(r"<code>([A-Z2-7 ]+)</code>", page).group(1).replace(" ", "")
    r = client.post("/app/sicherheit", data={"action": "enable", "code": "000000"})
    assert "Der Code stimmt nicht" in r.get_data(as_text=True)
    r = client.post("/app/sicherheit", data={"action": "enable", "code": totp.code_at(secret)})
    codes = re.findall(r"\b([A-Z2-9]{5}-[A-Z2-9]{5})\b", r.get_data(as_text=True))
    assert len(codes) == 8
    return secret, codes


def test_two_factor_login_with_code_replay_protection_and_recovery(app, client):
    login(client, f"julia.wagner@{DEMO}", PW)
    secret, codes = _enable_2fa(client)
    assert _logged_in(client)                       # dieses Gerät bleibt angemeldet
    client.post("/logout")
    r = client.post("/login", data={"email": f"julia.wagner@{DEMO}", "password": PW})   # Passwort allein reicht nicht
    assert r.headers["Location"].endswith("/anmelden/2fa") and not _logged_in(client)
    r = client.post("/anmelden/2fa", data={"code": "123456"})
    assert "Der Code stimmt nicht" in r.get_data(as_text=True) and not _logged_in(client)
    code = totp.code_at(secret, time.time() + 30)   # der Code des aktuellen Schritts wurde bei der Einrichtung verbraucht
    client.post("/anmelden/2fa", data={"code": code})
    assert _logged_in(client)
    client.post("/logout")                          # derselbe Code funktioniert kein zweites Mal
    client.post("/login", data={"email": f"julia.wagner@{DEMO}", "password": PW})
    client.post("/anmelden/2fa", data={"code": code})
    assert not _logged_in(client)
    client.post("/anmelden/2fa", data={"code": codes[0].lower()})        # Wiederherstellungscode: einmal nutzbar
    assert _logged_in(client)
    client.post("/logout")
    client.post("/login", data={"email": f"julia.wagner@{DEMO}", "password": PW})
    client.post("/anmelden/2fa", data={"code": codes[0]})
    assert not _logged_in(client)
    for _ in range(5):                              # nach fünf Fehlversuchen beginnt die Anmeldung von vorn
        r = client.post("/anmelden/2fa", data={"code": "999999"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert client.get("/anmelden/2fa").status_code == 302


def test_two_factor_secret_is_encrypted_and_disable_needs_password(app, client):
    login(client, f"julia.wagner@{DEMO}", PW)
    secret, _codes = _enable_2fa(client)
    with app.app_context(), use_club(default_club()):
        u = by("julia")
        assert u.totp_secret_enc and secret not in u.totp_secret_enc and totp.unseal(u.totp_secret_enc) == secret
        assert "-" not in (u.recovery_codes or "")        # nur Hashes, keine Klartext-Codes
    r = client.post("/app/sicherheit", data={"action": "disable", "password": "falsch"})
    assert "Das Passwort stimmt nicht" in r.get_data(as_text=True)
    client.post("/app/sicherheit", data={"action": "disable", "password": PW})
    with app.app_context(), use_club(default_club()):
        assert not by("julia").has_2fa


def test_two_factor_stays_required_after_password_reset(app, client, mails):
    app.config["SMTP_HOST"] = "smtp.test"
    login(client, f"julia.wagner@{DEMO}", PW)
    _enable_2fa(client)
    client.post("/logout")
    client.post("/passwort-vergessen", data={"email": f"julia.wagner@{DEMO}"})
    link = next(m["link"] for m in mails if m["template"] == "reset")
    client.post(f"/passwort-neu/{_token(link)}", data={"password": "ganz-neues-passwort"})
    r = client.post("/login", data={"email": f"julia.wagner@{DEMO}", "password": "ganz-neues-passwort"})
    assert r.headers["Location"].endswith("/anmelden/2fa") and not _logged_in(client)   # Reset umgeht 2FA nicht


def test_club_can_require_two_factor_for_admins_and_reset_it(app, client):
    from app.services import club as club_settings
    with app.app_context(), use_club(default_club()):
        club_settings.save({"admin_2fa": "1"})
        db.session.commit()
        uid = by("julia").id
    login(client, *ADMIN)
    r = client.get("/admin/", follow_redirects=False)
    assert r.status_code == 302 and "/app/sicherheit" in r.headers["Location"]       # gesperrt bis zur Einrichtung
    _enable_2fa(client)
    assert client.get("/admin/").status_code == 200
    r = client.post("/app/sicherheit", data={"action": "disable", "password": ADMIN[1]})
    assert "verlangt 2FA" in r.get_data(as_text=True)
    m = app.test_client()                           # Klubleitung setzt die 2FA eines Mitglieds zurück
    login(m, f"julia.wagner@{DEMO}", PW)
    _enable_2fa(m)
    client.post(f"/admin/mitglieder/{uid}/2fa-zuruecksetzen")
    with app.app_context(), use_club(default_club()):
        assert not by("julia").has_2fa
