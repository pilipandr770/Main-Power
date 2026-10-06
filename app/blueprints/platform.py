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
from ..models import Club, ClubRequest, Event, LLMUsage, User, utcnow

bp = Blueprint("platform", __name__)
SESSION_KEY = "platform_admin"
SESSION_TTL = 4 * 3600
SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
RESERVED = {"www", "admin", "app", "api", "plattform", "platform", "static", "mail", "klubs", "ftp", "smtp", "imap", "pop",
            "ns1", "ns2", "dev", "test", "staging", "cdn", "status", "shop", "autodiscover", "webmail", "blog", "docs",
            "support", "login", "auth", "assets"}


def reserved_slugs() -> set[str]:
    extra = {s.strip().lower() for s in (current_app.config.get("PLATFORM_RESERVED_SLUGS") or "").split(",") if s.strip()}
    return RESERVED | extra


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
                           platform_domain=current_app.config.get("PLATFORM_DOMAIN", ""), club_url=_club_url,
                           new_requests=ClubRequest.query.filter_by(status="new").count())


def _club_url(club: Club) -> str:
    """Öffentliche Adresse eines Klubs oder "" (keine eigene Domain und keine PLATFORM_DOMAIN: nicht erreichbar)."""
    from ..services import club as club_settings
    from ..tenancy import default_slug
    if club.domain_list or (current_app.config.get("PLATFORM_DOMAIN") and club.slug != default_slug()):
        return club_settings.base_url(club)
    return current_app.config["BASE_URL"] if club.slug == default_slug() else ""


def _slugify(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))
    t = re.sub(r"[^a-z0-9]+", "-", t.encode("ascii", "ignore").decode()).strip("-")[:40].strip("-")
    return t if SLUG.match(t) else ""


@bp.route("/anfragen")
@platform_required
def requests():
    rows = ClubRequest.query.order_by(ClubRequest.status == "new", ClubRequest.created_at.desc()).limit(200).all()
    from .public import SIZES
    return render_template("platform/requests.html", rows=rows, sizes=SIZES)


@bp.route("/anfragen/<int:rid>/ablehnen", methods=["POST"])
@platform_required
def request_reject(rid):
    row = db.session.get(ClubRequest, rid) or abort(404)
    row.status, row.handled_at = "rejected", utcnow()
    db.session.commit()
    flash("Anfrage abgelehnt.", "info")
    return redirect(url_for("platform.requests"))


def _validate(form, club: Club | None) -> dict:
    errors = {}
    slug = form.get("slug", "").strip().lower()
    if club is None:
        if not SLUG.match(slug) or slug in reserved_slugs():
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
    req = db.session.get(ClubRequest, request.args.get("anfrage", type=int) or 0)
    if request.method == "GET" and req:  # Vorbelegung aus der Anfrage
        first, _, last = req.contact_name.partition(" ")
        form = {"name": req.club_name, "slug": _slugify(req.club_name), "admin_email": req.email, "admin_first": first,
                "admin_last": last, "contact_email": req.email}
    if request.method == "POST":
        errors = _validate(form, None)
        email = form.get("admin_email", "").strip().lower()
        pw = form.get("admin_password", "")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors["admin_email"] = "Bitte eine gültige E-Mail angeben."
        if pw and len(pw) < 10:
            errors["admin_password"] = "Mindestens 10 Zeichen — oder leer lassen, dann bekommt die Person einen Link, um es selbst festzulegen."
        preset = form.get("preset") if form.get("preset") in PRESETS else "neutral"
        if not errors:
            import secrets
            from ..seed import create_club
            from ..services.mailer import send_mail
            from ..tenancy import use_club
            domains = ",".join(d.strip().lower() for d in form.get("domains", "").replace(";", ",").split(",") if d.strip())
            club = create_club(form["slug"].strip().lower(), form["name"].strip(), email, pw or secrets.token_urlsafe(24),
                               admin_first=form.get("admin_first", "").strip() or "Admin",
                               admin_last=form.get("admin_last", "").strip(), domains=domains, preset=preset,
                               demo=bool(form.get("demo")), contact_email=form.get("contact_email", "").strip())
            if req and req.status == "new":
                req.status, req.club_id, req.handled_at = "approved", club.id, utcnow()
                db.session.commit()
            url = _club_url(club)
            mailed, setup_link = False, ""
            if not pw:  # Klubleitung legt ihr Passwort selbst fest (Link 7 Tage gültig)
                from .auth import password_token
                with use_club(club):
                    owner = User.query.filter_by(email=email).one()
                    link = setup_link = f"{url or current_app.config['BASE_URL']}/passwort-neu/{password_token(owner, setup=True)}"
                    mailed = send_mail(email, f"Dein Klub „{club.name}“ ist eingerichtet", "owner_welcome", user=owner,
                                       club_name=club.name, url=url, link=link,
                                       admin_url=(url or current_app.config["BASE_URL"]) + "/admin/")
            flash(f"Klub „{club.name}“ angelegt. Erste Klubleitung: {email}."
                  + (f" Adresse: {url}." if url else " Achtung: Der Klub hat noch keine erreichbare Adresse — "
                     "eigene Domain eintragen oder PLATFORM_DOMAIN setzen.")
                  + (" Einrichtungslink per E-Mail verschickt." if not pw and mailed else
                     f" Kein E-Mail-Versand eingerichtet: Gib diesen Einrichtungslink (7 Tage gültig) selbst weiter: {setup_link}"
                     if not pw else ""), "success")
            return redirect(url_for("platform.dashboard"))
    return render_template("platform/club_form.html", klub=None, errors=errors, form=form, presets=PRESETS,
                           club_plans=_club_plans(), req=req)


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
            if form.get("plan") == "pilot" or any(p.key == form.get("plan") for p in _club_plans()):
                club.plan = form["plan"]
            club.contact_email = form.get("contact_email", "").strip()[:255]
            club.notes = form.get("notes", "").strip()[:4000]
            db.session.commit()
            flash("Gespeichert.", "success")
            return redirect(url_for("platform.club_edit", cid=club.id))
    return render_template("platform/club_form.html", klub=club, errors=errors, form=form, presets=PRESETS,
                           club_plans=_club_plans(), req=None)


def _club_plans():
    from ..models import Plan
    return Plan.query.filter_by(kind="club").order_by(Plan.sort).all()


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


# --------------------------------------------------------------------------- Tarife, Pakete, Kalkulation, Umsatz
from ..models import AddOn, Payment, Plan, PlatformSetting  # noqa: E402
from ..services import plans as plan_svc  # noqa: E402


def _euro_to_cents(raw: str) -> int | None:
    try:
        v = float((raw or "").replace("€", "").replace(",", ".").strip() or "0")
    except ValueError:
        return None
    return int(round(v * 100)) if 0 <= v < 100_000 else None


def _int_or_none(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _revenue_by_club() -> list[dict]:
    """Umsatz (netto), Provision an den Klub, KI-Kosten und Ergebnis je Klub im laufenden Monat."""
    month = plan_svc.month_start()

    def q(*cols):
        return db.session.query(*cols).execution_options(all_clubs=True)

    pay: dict[int, dict] = {}
    for cid, kind, total in (q(Payment.club_id, Payment.kind, func.sum(Payment.amount_cents))
                             .filter(Payment.created_at >= month).group_by(Payment.club_id, Payment.kind)):
        pay.setdefault(cid, {})[kind] = int(total or 0)
    cfg = current_app.config
    cost = {cid: ((i or 0) * cfg["LLM_PRICE_IN"] + (o or 0) * cfg["LLM_PRICE_OUT"]) / 1_000_000 * 100
            for cid, i, o in q(LLMUsage.club_id, func.sum(LLMUsage.input_tokens), func.sum(LLMUsage.output_tokens))
            .filter(LLMUsage.created_at >= month).group_by(LLMUsage.club_id)}
    vat, comm = plan_svc.vat_rate() / 100, plan_svc.commission_pct() / 100
    rows = []
    for club in Club.query.order_by(Club.id).all():
        p = pay.get(club.id, {})
        member_net = (p.get("member_plan", 0) + p.get("addon", 0)) / (1 + vat)
        club_net = p.get("club_plan", 0) / (1 + vat)
        commission = p.get("member_plan", 0) / (1 + vat) * comm
        ai = cost.get(club.id, 0.0)
        rows.append({"club": club, "member": member_net, "club_plan": club_net, "commission": commission, "ai": ai,
                     "result": member_net + club_net - commission - ai})
    return rows


@bp.route("/tarife")
@platform_required
def tariffs():
    plan_svc.ensure_defaults()
    all_plans = Plan.query.order_by(Plan.kind.desc(), Plan.sort).all()
    return render_template("platform/tariffs.html", plans=all_plans,
                           calc={p.id: plan_svc.calculation(p) for p in all_plans},
                           addons=AddOn.query.order_by(AddOn.sort).all(), features=plan_svc.FEATURES,
                           unit_costs={f: plan_svc.unit_cost(f) for f in plan_svc.FEATURES},
                           measured=plan_svc.measured_costs(), vat=plan_svc.vat_rate(),
                           commission=plan_svc.commission_pct(), revenue=_revenue_by_club(),
                           daily_cap=plan_svc.daily_cap_cents(), spent_today=plan_svc.free_spend_today_cents())


@bp.route("/tarife/einstellungen", methods=["POST"])
@platform_required
def tariff_settings():
    f = request.form
    for key, lo, hi in (("vat_rate", 0, 30), ("commission_pct", 0, 80), ("ai_daily_cap_cents", 0, 1_000_000)):
        try:
            v = float(f.get(key, "").replace(",", "."))
            if lo <= v <= hi:
                PlatformSetting.set(key, f"{v:g}")
        except ValueError:
            pass
    for feat in plan_svc.FEATURES:
        try:
            v = float(f.get(f"cost.{feat}", "").replace(",", "."))
            if 0 <= v < 1000:
                PlatformSetting.set(f"cost.{feat}", f"{v:g}")
        except ValueError:
            pass
    db.session.commit()
    flash("Einstellungen gespeichert. Die Kalkulation ist neu berechnet.", "success")
    return redirect(url_for("platform.tariffs"))


@bp.route("/tarife/neu", methods=["GET", "POST"])
@bp.route("/tarife/<int:pid>", methods=["GET", "POST"])
@platform_required
def tariff_edit(pid=None):
    plan = db.session.get(Plan, pid) if pid else Plan(kind=request.args.get("kind", "member"), active=True, limits={})
    if plan is None:
        abort(404)
    errors = {}
    f = request.form
    if request.method == "POST":
        if not plan.id:
            key = re.sub(r"[^a-z0-9-]+", "-", f.get("key", "").strip().lower()).strip("-")[:30]
            if not key or key == "pilot" or Plan.query.filter_by(key=key).first():
                errors["key"] = "Eindeutiger Schlüssel nötig (Kleinbuchstaben, Ziffern, Bindestrich)."
            plan.key = key
            plan.kind = f.get("kind") if f.get("kind") in Plan.KINDS else "member"
        price = _euro_to_cents(f.get("price", ""))
        if price is None:
            errors["price"] = "Bitte einen Preis in Euro angeben, z. B. 9 oder 29,00."
        if not f.get("name", "").strip():
            errors["name"] = "Name fehlt."
        limits = {}
        for feat in plan_svc.FEATURES:
            raw = f.get(f"limit.{feat}", "").strip()
            if raw == "":
                limits[feat] = None
            elif raw.isdigit():
                limits[feat] = int(raw)
            else:
                errors[f"limit.{feat}"] = "Ganze Zahl oder leer (= unbegrenzt)."
        if not errors:
            plan.name, plan.tagline = f["name"].strip()[:80], f.get("tagline", "").strip()[:200]
            plan.price_cents, plan.limits = price, limits
            plan.features = f.get("features", "").strip()[:2000]
            plan.active = bool(f.get("active"))
            plan.sort = _int_or_none(f.get("sort")) or 0
            if plan.kind == "club":
                plan.members_included = _int_or_none(f.get("members_included"))
                plan.max_club_questions = _int_or_none(f.get("max_club_questions"))
                plan.extra_member_cents = _euro_to_cents(f.get("extra_member", "")) or 0
                budget = f.get("ai_budget", "").strip()
                plan.ai_budget_cents = _euro_to_cents(budget) if budget else None
            db.session.add(plan)
            db.session.commit()
            c = plan_svc.calculation(plan)
            if c["unbounded"]:
                flash("Gespeichert. Achtung: Mindestens ein Kontingent ist unbegrenzt – die Kosten sind nach oben offen.",
                      "error")
            elif c["margin_pct"] is not None and c["margin_pct"] < 30:
                flash(f"Gespeichert. Achtung: Marge im ungünstigsten Fall nur {c['margin_pct']:.0f} %.", "error")
            else:
                flash("Tarif gespeichert.", "success")
            return redirect(url_for("platform.tariff_edit", pid=plan.id))
    return render_template("platform/tariff_form.html", plan=plan, errors=errors, form=f, features=plan_svc.FEATURES,
                           calc=plan_svc.calculation(plan) if plan.id else None, vat=plan_svc.vat_rate())


@bp.route("/pakete/neu", methods=["GET", "POST"])
@bp.route("/pakete/<int:aid>", methods=["GET", "POST"])
@platform_required
def addon_edit(aid=None):
    addon = db.session.get(AddOn, aid) if aid else AddOn(active=True, units=1, feature="site_check")
    if addon is None:
        abort(404)
    errors = {}
    f = request.form
    if request.method == "POST":
        if not addon.id:
            key = re.sub(r"[^a-z0-9-]+", "-", f.get("key", "").strip().lower()).strip("-")[:40]
            if not key or AddOn.query.filter_by(key=key).first():
                errors["key"] = "Eindeutiger Schlüssel nötig."
            addon.key = key
        price = _euro_to_cents(f.get("price", ""))
        if not price:
            errors["price"] = "Bitte einen Preis angeben."
        if f.get("feature") not in plan_svc.QUOTA_FEATURES:
            errors["feature"] = "Bitte eine Funktion wählen."
        if not _int_or_none(f.get("units")):
            errors["units"] = "Mindestens 1."
        if not f.get("name", "").strip():
            errors["name"] = "Name fehlt."
        if not errors:
            addon.name, addon.description = f["name"].strip()[:120], f.get("description", "").strip()[:300]
            addon.price_cents, addon.feature, addon.units = price, f["feature"], _int_or_none(f.get("units"))
            addon.personas = _int_or_none(f.get("personas"))
            addon.active = bool(f.get("active"))
            addon.sort = _int_or_none(f.get("sort")) or 0
            db.session.add(addon)
            db.session.commit()
            flash("Paket gespeichert.", "success")
            return redirect(url_for("platform.tariffs") + "#pakete")
    return render_template("platform/addon_form.html", addon=addon, errors=errors, form=f, features=plan_svc.FEATURES,
                           quota_features=plan_svc.QUOTA_FEATURES)
