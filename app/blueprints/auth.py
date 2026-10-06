from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from flask import (Blueprint, current_app, flash, redirect, render_template, request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..services import club as club_settings
from ..extensions import db, limiter
from ..models import Consent, Profile, User, utcnow
from ..services.audit import audit
from ..services import totp
from ..services.mailer import send_mail

bp = Blueprint("auth", __name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


PRE2FA_TTL = 300  # so lange gilt der Zwischenschritt zwischen Passwort und Code
VERIFY_TTL = 7 * 24 * 3600


def _verify_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="email-verify")


def send_verification(user: User) -> bool:
    """Bestätigungslink an die E-Mail-Adresse schicken (gilt nur für genau diese Adresse)."""
    token = _verify_serializer().dumps({"uid": user.id, "e": user.email})
    return send_mail(user.email, f"Bitte bestätige deine E-Mail-Adresse — {club_settings.settings()['name']}", "verify",
                     user=user, link=url_for("auth.verify_email", token=token, _external=True))


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pw-reset")


SETUP_TTL = 7 * 24 * 3600


def password_token(user: User, setup: bool = False) -> str:
    """Token für „Passwort festlegen“. Reset: 1 Stunde gültig; Einrichtung (neue Klubleitung): 7 Tage."""
    return _serializer().dumps({"uid": user.id, "h": user.password_hash[-12:], "k": "setup" if setup else "reset"})


def _safe_next(target: str | None) -> str | None:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


def record_consent(user: User, kind: str, granted: bool, source: str = "web") -> None:
    db.session.add(Consent(user=user, kind=kind, granted=granted,
                           version=current_app.config["CONSENT_VERSION"], source=source))


@bp.route("/einladung/<token>")
def invite_link(token):
    return redirect(url_for("auth.register", einladung=token))


@bp.route("/registrieren", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("member.dashboard"))
    from ..services import invites
    mode = club_settings.settings()["registration"]
    token = (request.form.get("einladung") or request.args.get("einladung") or "").strip()
    invite = invites.find(token)
    form = request.form if request.method == "POST" else {"email": invite.email if invite else "", "einladung": token}
    errors: dict[str, str] = {}
    if request.method == "POST":
        email = form.get("email", "").strip().lower()
        first = form.get("first_name", "").strip()
        last = form.get("last_name", "").strip()
        pw = form.get("password", "")
        if not first:
            errors["first_name"] = "Bitte gib deinen Vornamen an."
        if mode == "invite" and not invite:
            errors["einladung"] = ("Diese Einladung ist ungültig oder abgelaufen." if token else
                                   "Die Registrierung ist nur mit Einladung möglich. Bitte gib deinen Einladungscode ein.")
        elif invite and invite.email and invite.email != email:
            errors["email"] = "Diese Einladung gilt für eine andere E-Mail-Adresse."
        if not EMAIL_RE.match(email):
            errors["email"] = "Bitte gib eine gültige E-Mail-Adresse an."
        elif User.query.filter_by(email=email).first():
            errors["email"] = "Mit dieser E-Mail gibt es bereits ein Konto. Melde dich an oder setze dein Passwort zurück."
        if len(pw) < 10:
            errors["password"] = "Das Passwort braucht mindestens 10 Zeichen."
        if not form.get("consent_privacy"):
            errors["consent_privacy"] = "Ohne Zustimmung zur Datenverarbeitung können wir kein Konto anlegen."
        if not form.get("consent_values"):
            errors["consent_values"] = "Bitte bestätige die Werte der Community."
        if not errors:
            pending = mode == "approval" and not invite  # eine gültige Einladung überspringt die Freigabe
            user = User(email=email, first_name=first[:80], last_name=last[:80],
                        phone=form.get("phone", "").strip()[:40], status="pending" if pending else "active")
            user.set_password(pw)
            matching = bool(form.get("consent_matching"))
            user.profile = Profile(allow_matching=matching, visible_in_directory=bool(form.get("consent_directory")))
            db.session.add(user)
            record_consent(user, "privacy", True)
            record_consent(user, "values", True)
            record_consent(user, "matching", matching)
            record_consent(user, "directory", bool(form.get("consent_directory")))
            record_consent(user, "newsletter", bool(form.get("consent_newsletter")))
            if invite:
                invite.uses += 1
                audit("invite.used", f"invite:{invite.id}", email)
                if invite.email and invite.email == email:  # Einladung ging an diese Adresse: Postfach ist nachgewiesen
                    user.email_verified_at = utcnow()
            if pending:
                audit("user.registered", f"user:{user.id}", "wartet auf Freigabe", actor_id=None)
                db.session.commit()
                name = club_settings.settings()["name"]
                send_mail(user.email, f"Deine Anmeldung bei {name}", "reg_received", user=user)
                if not user.email_verified_at:
                    send_verification(user)
                for admin in User.query.filter(User.role.in_(("admin", "superadmin")), User.status == "active").all():
                    send_mail(admin.email, "Neue Anmeldung wartet auf Freigabe", "reg_pending", user=user, admin=admin,
                              link=url_for("admin.user_detail", user_id=user.id, _external=True))
                flash("Danke! Wir prüfen deine Anmeldung und melden uns per E-Mail, sobald dein Konto freigeschaltet ist.",
                      "info")
                return redirect(url_for("auth.login"))
            user.last_login_at = utcnow()
            db.session.commit()
            login_user(user)
            send_mail(user.email, f"Willkommen bei {club_settings.settings()['name']}", "welcome", user=user,
                      link=url_for("member.profile", _external=True))
            if not user.email_verified_at:
                send_verification(user)
            flash("Willkommen in der Community! Erzähl uns kurz von dir — daraus entstehen deine Matches.", "success")
            return redirect(url_for("member.profile", welcome=1))
    return render_template("auth/register.html", errors=errors, form=form, mode=mode, invite=invite)


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("8 per minute; 40 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("member.dashboard"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(request.form.get("password", "")):
            error = "E-Mail oder Passwort stimmen nicht."
        elif user.status == "pending":
            error = "Dein Konto wartet noch auf die Freigabe durch den Klub. Du bekommst eine E-Mail, sobald es soweit ist."
        elif user.status != "active":
            error = f"Dieses Konto ist gesperrt. Schreib uns an {club_settings.settings()['contact_email']}."
        else:
            nxt = _safe_next(request.args.get("next"))
            if user.has_2fa:  # Passwort stimmt, aber angemeldet ist man erst nach dem Code
                session["pre2fa"] = {"uid": user.id, "at": time.time(), "tries": 0, "next": nxt,
                                     "remember": bool(request.form.get("remember"))}
                return redirect(url_for("auth.login_2fa"))
            return _finish_login(user, bool(request.form.get("remember")), nxt)
    return render_template("auth/login.html", error=error)


def _finish_login(user: User, remember: bool, nxt: str | None):
    login_user(user, remember=remember)
    user.last_login_at = utcnow()
    db.session.commit()
    if nxt:
        return redirect(nxt)
    return redirect(url_for("admin.dashboard" if user.is_admin else "member.dashboard"))


@bp.route("/anmelden/2fa", methods=["GET", "POST"])
@limiter.limit("10 per minute; 40 per hour", methods=["POST"])
def login_2fa():
    pre = session.get("pre2fa")
    if not pre or time.time() - pre.get("at", 0) > PRE2FA_TTL:
        session.pop("pre2fa", None)
        flash("Bitte melde dich erneut an.", "info")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pre["uid"])
    if not user or user.status != "active" or not user.has_2fa:
        session.pop("pre2fa", None)
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        code = request.form.get("code", "")
        secret = totp.unseal(user.totp_secret_enc)
        step = totp.verify(secret, code, user.totp_last_step) if secret else None
        left = None if step else totp.use_recovery_code(user.recovery_hashes, code)
        if step or left is not None:
            if step:
                user.totp_last_step = step
            else:
                user.recovery_codes = json.dumps(left)
                audit("auth.recovery_code_used", f"user:{user.id}", f"{len(left)} übrig", actor_id=user.id)
                flash(f"Wiederherstellungscode verwendet. Es bleiben {len(left)} Codes. Richte bei Gelegenheit "
                      "dein Gerät neu ein.", "info")
            session.pop("pre2fa", None)
            return _finish_login(user, pre.get("remember", False), pre.get("next"))
        pre["tries"] = pre.get("tries", 0) + 1
        session["pre2fa"] = pre
        if pre["tries"] >= 5:
            session.pop("pre2fa", None)
            flash("Zu viele falsche Codes. Bitte melde dich neu an.", "error")
            return redirect(url_for("auth.login"))
        error = "Der Code stimmt nicht. Prüfe die Uhrzeit deines Geräts oder nutze einen Wiederherstellungscode."
    return render_template("auth/login_2fa.html", error=error)


# --------------------------------------------------------------------------- E-Mail bestätigen
@bp.route("/e-mail-bestaetigen/<token>")
def verify_email(token):
    try:
        data = _verify_serializer().loads(token, max_age=VERIFY_TTL)
    except (SignatureExpired, BadSignature):
        flash("Der Bestätigungslink ist abgelaufen oder ungültig. Du kannst nach der Anmeldung einen neuen anfordern.",
              "error")
        return redirect(url_for("auth.login"))
    user = db.session.get(User, data.get("uid"))
    if not user or user.email != data.get("e"):
        flash("Dieser Link gehört zu einer anderen E-Mail-Adresse.", "error")
        return redirect(url_for("auth.login"))
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
        audit("auth.email_verified", f"user:{user.id}", actor_id=user.id)
        db.session.commit()
    flash("Danke, deine E-Mail-Adresse ist bestätigt.", "success")
    return redirect(url_for("member.dashboard" if current_user.is_authenticated else "auth.login"))


@bp.route("/e-mail-bestaetigen", methods=["POST"])
@login_required
@limiter.limit("3 per hour")
def resend_verification():
    if current_user.email_verified_at:
        flash("Deine E-Mail-Adresse ist schon bestätigt.", "info")
    elif not current_app.config.get("SMTP_HOST"):
        flash("Der E-Mail-Versand ist auf dieser Installation nicht eingerichtet.", "error")
    else:
        send_verification(current_user)
        flash("Wir haben dir einen neuen Bestätigungslink geschickt.", "success")
    return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                    else url_for("member.dashboard"))


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    session.pop("impersonator_id", None)
    logout_user()
    flash("Du bist abgemeldet.", "info")
    return redirect(url_for("public.index"))


@bp.route("/passwort-vergessen", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email, status="active").first()
        if user:
            token = _serializer().dumps({"uid": user.id, "h": user.password_hash[-12:]})
            send_mail(user.email, "Passwort zurücksetzen", "reset", user=user,
                      link=url_for("auth.reset", token=token, _external=True))
        flash("Wenn ein Konto zu dieser E-Mail existiert, haben wir dir einen Link geschickt (gültig 1 Stunde).", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html")


@bp.route("/passwort-neu/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        data, issued = _serializer().loads(token, max_age=SETUP_TTL, return_timestamp=True)
        if data.get("k") != "setup" and (datetime.now(timezone.utc) - issued).total_seconds() > 3600:
            raise SignatureExpired("Reset-Link nach einer Stunde abgelaufen")
    except (SignatureExpired, BadSignature):
        flash("Der Link ist abgelaufen oder ungültig. Fordere einen neuen an.", "error")
        return redirect(url_for("auth.forgot"))
    user = db.session.get(User, data.get("uid"))
    if not user or user.password_hash[-12:] != data.get("h"):
        flash("Der Link wurde bereits verwendet.", "error")
        return redirect(url_for("auth.forgot"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) < 10:
            error = "Das Passwort braucht mindestens 10 Zeichen."
        else:
            user.set_password(pw)
            if not user.email_verified_at:  # der Link kam per E-Mail: Postfach ist damit nachgewiesen
                user.email_verified_at = utcnow()
            audit("auth.password_reset", f"user:{user.id}", actor_id=user.id)
            db.session.commit()
            send_mail(user.email, "Dein Passwort wurde geändert", "password_changed", user=user)
            flash("Passwort geändert. Du kannst dich jetzt anmelden.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", error=error)
