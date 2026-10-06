from __future__ import annotations

import re

from flask import (Blueprint, current_app, flash, redirect, render_template, request, session, url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..services import club as club_settings
from ..extensions import db, limiter
from ..models import Consent, Profile, User, utcnow
from ..services.audit import audit
from ..services.mailer import send_mail

bp = Blueprint("auth", __name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="pw-reset")


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
            if pending:
                audit("user.registered", f"user:{user.id}", "wartet auf Freigabe", actor_id=None)
                db.session.commit()
                name = club_settings.settings()["name"]
                send_mail(user.email, f"Deine Anmeldung bei {name}", "reg_received", user=user)
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
            login_user(user, remember=bool(request.form.get("remember")))
            user.last_login_at = utcnow()
            db.session.commit()
            nxt = _safe_next(request.args.get("next"))
            if nxt:
                return redirect(nxt)
            return redirect(url_for("admin.dashboard" if user.is_admin else "member.dashboard"))
    return render_template("auth/login.html", error=error)


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
        data = _serializer().loads(token, max_age=3600)
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
            db.session.commit()
            flash("Passwort geändert. Du kannst dich jetzt anmelden.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", error=error)
