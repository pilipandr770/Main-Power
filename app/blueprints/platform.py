"""Plattform-Konsole (/plattform): Klubs anlegen, pausieren und überblicken — für den Betreiber der SaaS.

Eigene Anmeldung (PLATFORM_ADMIN_EMAIL / PLATFORM_ADMIN_PASSWORD in der .env), bewusst unabhängig von den
Klub-Konten: Ein Klub-Admin kann sich so nie selbst Plattform-Rechte geben. Ohne diese Variablen ist die Konsole aus.
Hier gilt kein Klub-Filter (tenancy: kein Klub im Kontext) — Abfragen sehen alle Klubs.
"""
from __future__ import annotations

import hmac
import re
import time
from datetime import timedelta
from functools import wraps

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func
from werkzeug.security import check_password_hash

from ..club_presets import PRESETS
from ..extensions import db, limiter
from ..models import Club, Event, LLMUsage, User, utcnow

bp = Blueprint("platform", __name__)
SESSION_KEY = "platform_admin"
SESSION_TTL = 4 * 3600
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
RESERVED = {"www", "admin", "app", "api", "plattform", "platform", "static", "mail"}


def _enabled() -> bool:
    return bool(current_app.config.get("PLATFORM_ADMIN_EMAIL") and current_app.config.get("PLATFORM_ADMIN_PASSWORD"))


def _logged_in() -> bool:
    data = session.get(SESSION_KEY)
    return bool(data and data.get("email") == current_app.config.get("PLATFORM_ADMIN_EMAIL")
                and time.time() - data.get("at", 0) < SESSION_TTL)


def platform_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not _enabled():
            abort(404)
        if not _logged_in():
            return redirect(url_for("platform.login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def _password_ok(given: str) -> bool:
    stored = current_app.config.get("PLATFORM_ADMIN_PASSWORD", "")
    if stored.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored, given)
    return hmac.compare_digest(stored.encode(), given.encode())


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def login():
    if not _enabled():
        abort(404)
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if hmac.compare_digest(email, current_app.config["PLATFORM_ADMIN_EMAIL"]) and \
                _password_ok(request.form.get("password", "")):
            session[SESSION_KEY] = {"email": email, "at": time.time()}
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/plattform") else url_for("platform.dashboard"))
        error = "E-Mail oder Passwort falsch."
    return render_template("platform/login.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.pop(SESSION_KEY, None)
    return redirect(url_for("platform.login"))


def _stats() -> dict[int, dict]:
    since = utcnow() - timedelta(days=30)
    q = lambda *cols: db.session.query(*cols).execution_options(all_clubs=True)  # noqa: E731
    members = dict(q(User.club_id, func.count(User.id)).group_by(User.club_id).all())
    events = dict(q(Event.club_id, func.count(Event.id)).filter(Event.starts_at >= utcnow())
                  .group_by(Event.club_id).all())
    tokens = {cid: (int(i or 0), int(o or 0)) for cid, i, o in
              q(LLMUsage.club_id, func.sum(LLMUsage.input_tokens), func.sum(LLMUsage.output_tokens))
              .filter(LLMUsage.created_at >= since).group_by(LLMUsage.club_id).all()}
    cfg = current_app.config
    out = {}
    for cid in {*members, *events, *tokens}:
        t_in, t_out = tokens.get(cid, (0, 0))
        out[cid] = {"members": members.get(cid, 0), "events": events.get(cid, 0), "tokens": t_in + t_out,
                    "cost": (t_in * cfg["LLM_PRICE_IN"] + t_out * cfg["LLM_PRICE_OUT"]) / 1_000_000}
    return out


@bp.route("/")
@platform_required
def dashboard():
    clubs = Club.query.order_by(Club.id).all()
    return render_template("platform/dashboard.html", clubs=clubs, stats=_stats(),
                           platform_domain=current_app.config.get("PLATFORM_DOMAIN", ""))


def _validate(form, club: Club | None) -> dict:
    errors = {}
    slug = form.get("slug", "").strip().lower()
    if club is None:
        if not SLUG.match(slug) or slug in RESERVED:
            errors["slug"] = "3–40 Zeichen: Kleinbuchstaben, Ziffern, Bindestrich (nicht am Anfang/Ende)."
        elif Club.query.filter_by(slug=slug).first():
            errors["slug"] = "Dieser Slug ist vergeben."
    if not form.get("name", "").strip():
        errors["name"] = "Bitte einen Namen angeben."
    domains = [d.strip().lower() for d in form.get("domains", "").replace(";", ",").split(",") if d.strip()]
    for d in domains:
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d):
            errors["domains"] = f"Ungültige Domain: {d}"
            break
        other = [c for c in Club.query.all() if d in c.domain_list and (club is None or c.id != club.id)]
        if other:
            errors["domains"] = f"{d} gehört bereits zu {other[0].name}."
            break
    return errors


@bp.route("/klub/neu", methods=["GET", "POST"])
@platform_required
def club_new():
    errors = {}
    form = request.form
    if request.method == "POST":
        errors = _validate(form, None)
        email = form.get("admin_email", "").strip().lower()
        pw = form.get("admin_password", "")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors["admin_email"] = "Bitte eine gültige E-Mail angeben."
        if len(pw) < 10:
            errors["admin_password"] = "Mindestens 10 Zeichen."
        preset = form.get("preset") if form.get("preset") in PRESETS else "neutral"
        if not errors:
            from ..seed import create_club
            domains = ",".join(d.strip().lower() for d in form.get("domains", "").replace(";", ",").split(",") if d.strip())
            club = create_club(form["slug"].strip().lower(), form["name"].strip(), email, pw,
                               admin_first=form.get("admin_first", "").strip() or "Admin",
                               admin_last=form.get("admin_last", "").strip(), domains=domains, preset=preset,
                               demo=bool(form.get("demo")), contact_email=form.get("contact_email", "").strip())
            flash(f"Klub „{club.name}“ angelegt. Erster Superadmin: {email}.", "success")
            return redirect(url_for("platform.dashboard"))
    return render_template("platform/club_form.html", klub=None, errors=errors, form=form, presets=PRESETS)


@bp.route("/klub/<int:cid>", methods=["GET", "POST"])
@platform_required
def club_edit(cid):
    club = db.session.get(Club, cid) or abort(404)
    errors = {}
    form = request.form
    if request.method == "POST":
        errors = _validate(form, club)
        if not errors:
            club.name = form["name"].strip()[:120]
            club.domains = ",".join(d.strip().lower() for d in form.get("domains", "").replace(";", ",").split(",")
                                    if d.strip())
            club.plan = form.get("plan", club.plan)[:30]
            club.contact_email = form.get("contact_email", "").strip()[:255]
            club.notes = form.get("notes", "").strip()[:4000]
            db.session.commit()
            flash("Gespeichert.", "success")
            return redirect(url_for("platform.club_edit", cid=club.id))
    return render_template("platform/club_form.html", klub=club, errors=errors, form=form, presets=PRESETS)


@bp.route("/klub/<int:cid>/status", methods=["POST"])
@platform_required
def club_status(cid):
    club = db.session.get(Club, cid) or abort(404)
    if club.slug == current_app.config.get("DEFAULT_CLUB_SLUG") and club.status == "active":
        flash("Der Standardklub kann nicht pausiert werden.", "error")
    else:
        club.status = "suspended" if club.status == "active" else "active"
        db.session.commit()
        flash(f"„{club.name}“ ist jetzt {'pausiert' if club.status == 'suspended' else 'aktiv'}.", "info")
    return redirect(url_for("platform.dashboard"))
