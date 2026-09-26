from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user
from sqlalchemy import func

from ..extensions import db
from ..models import (CONSENT_KINDS, FORMATS, ROLES, SOCIALS, AuditLog, ChatMessage, Consent, Event, IntroRequest,
                      KnowledgeItem, LLMUsage, Notification, Profile, Registration, Service, ServiceInquiry, Setting, User, utcnow)
from ..services import insights, matching, media, telegram
from ..services.audit import audit
from ..services.events_sync import sync_events
from ..services.gdpr import delete_user, export_user
from ..utils import admin_required, clean_social, fmt_dt, fmt_event_date, local_to_utc, superadmin_required, to_local

bp = Blueprint("admin", __name__)


@bp.before_request
@login_required
@admin_required
def _guard():
    return None


# --------------------------------------------------------------------------- Übersicht
@bp.route("/")
def dashboard():
    now = utcnow()
    week = now - timedelta(days=7)
    stats = {
        "members": User.query.filter_by(status="active").count(),
        "new_week": User.query.filter(User.created_at >= week).count(),
        "matchable": Profile.query.filter(Profile.allow_matching.is_(True), Profile.embed_need.isnot(None)).count(),
        "directory": Profile.query.filter_by(visible_in_directory=True).count(),
        "events": Event.query.filter(Event.status == "published", Event.starts_at >= now).count(),
        "pending_events": Event.query.filter_by(status="pending").count(),
        "registrations": Registration.query.filter(Registration.status.in_(["registered", "paid", "reserved"])).count(),
        "intros": IntroRequest.query.count(),
        "intros_accepted": IntroRequest.query.filter_by(status="accepted").count(),
        "chats_week": ChatMessage.query.filter(ChatMessage.created_at >= week, ChatMessage.role == "user").count(),
        "inquiries_new": ServiceInquiry.query.filter_by(status="new").count(),
        "telegram": User.query.filter(User.telegram_user_id.isnot(None)).count(),
    }
    # Profil-Vollständigkeit & Wachstum (letzte 8 Wochen)
    growth = []
    for i in range(7, -1, -1):
        end = now - timedelta(days=7 * i)
        growth.append({"label": f"{to_local(end):%d.%m.}", "n": User.query.filter(User.created_at <= end).count()})
    maxn = max([g["n"] for g in growth] + [1])
    upcoming_events = (Event.query.filter(Event.status == "published", Event.starts_at >= now - timedelta(hours=6))
                       .order_by(Event.starts_at).limit(5).all())
    recent_users = User.query.order_by(User.created_at.desc()).limit(6).all()
    return render_template("admin/dashboard.html", s=stats, growth=growth, maxn=maxn,
                           upcoming_events=upcoming_events, recent_users=recent_users, usage=_llm_usage(now),
                           graph=_match_graph())


def _match_graph(limit: int = 60) -> dict:
    """Karte aller Matching-Profile; Linien = die zwei stärksten Matches je Person (nur für die Organisation)."""
    pool = matching._pool()[:limit]
    nodes = [{"l": p.user.full_name, "u": url_for("admin.user_detail", user_id=p.user_id), "h": 0} for p in pool]
    edges, seen = [], set()
    for i, a in enumerate(pool):
        ranked = sorted(((matching.pair_score(a, b), j) for j, b in enumerate(pool) if j != i), reverse=True)[:2]
        for score, j in ranked:
            key = (min(i, j), max(i, j))
            if key not in seen and score > 0.05:
                seen.add(key)
                edges.append([i, j, round(min(1.0, score * 2), 2)])
    degree: dict[int, int] = {}
    for a, b, _ in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    for i, d in degree.items():
        nodes[i]["h"] = 1 if d >= 3 else 0  # stark vernetzte Personen hervorheben
    return {"nodes": nodes, "edges": edges}


def _llm_usage(now: datetime) -> dict:
    """Token-Zähler (gesamt / 30 Tage / heute) inkl. Kostenschätzung und Aufschlüsselung nach Zweck."""
    from flask import current_app
    cfg = current_app.config

    def agg(since=None):
        q = db.session.query(func.coalesce(func.sum(LLMUsage.input_tokens), 0),
                             func.coalesce(func.sum(LLMUsage.output_tokens), 0), func.count(LLMUsage.id))
        if since:
            q = q.filter(LLMUsage.created_at >= since)
        i, o, n = q.one()
        cost = (i * cfg["LLM_PRICE_IN"] + o * cfg["LLM_PRICE_OUT"]) / 1_000_000
        return {"in": int(i), "out": int(o), "total": int(i + o), "calls": int(n), "cost": cost}

    labels = {"aiko_public": "Aiko (Website)", "aiko_member": "Aiko (Mitglieder)", "matching": "Matching-Begründungen"}
    rows = (db.session.query(LLMUsage.purpose, func.sum(LLMUsage.input_tokens), func.sum(LLMUsage.output_tokens),
                             func.count(LLMUsage.id)).group_by(LLMUsage.purpose).all())
    by_purpose = [{"label": labels.get(pu, pu or "sonstige"), "total": int(i or 0) + int(o or 0), "calls": n}
                  for pu, i, o, n in rows]
    return {"all": agg(), "d30": agg(now - timedelta(days=30)), "today": agg(now.replace(hour=0, minute=0, second=0,
                                                                                         microsecond=0)),
            "by_purpose": sorted(by_purpose, key=lambda r: -r["total"]), "model": cfg["ANTHROPIC_MODEL"]}


# --------------------------------------------------------------------------- Mitglieder
@bp.route("/mitglieder")
def users():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    query = User.query.outerjoin(Profile)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(func.lower(User.email).like(like) | func.lower(User.first_name).like(like) |
                             func.lower(User.last_name).like(like) | func.lower(Profile.headline).like(like) |
                             func.lower(Profile.industry).like(like))
    if status in ("active", "blocked"):
        query = query.filter(User.status == status)
    users_ = query.order_by(User.created_at.desc()).limit(500).all()
    return render_template("admin/users.html", users=users_, q=q, status=status)


@bp.route("/mitglieder/export.csv")
def users_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "vorname", "nachname", "email", "rolle", "status", "branche", "rolle_unternehmen",
                "matching", "verzeichnis", "profil_%", "registriert"])
    for u in User.query.order_by(User.id):
        p = u.profile or Profile()
        w.writerow([u.id, u.first_name, u.last_name, u.email, u.role, u.status, p.industry, p.headline,
                    int(bool(p.allow_matching)), int(bool(p.visible_in_directory)), p.completeness, fmt_dt(u.created_at)])
    audit("users.export_csv")
    db.session.commit()
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=mitglieder.csv"})


@bp.route("/mitglieder/<int:user_id>")
def user_detail(user_id):
    u = db.session.get(User, user_id) or abort(404)
    intros = IntroRequest.query.filter((IntroRequest.from_user_id == u.id) | (IntroRequest.to_user_id == u.id)) \
        .order_by(IntroRequest.created_at.desc()).all()
    matches = matching.top_matches(u, k=5, explain=False) if u.profile and u.profile.embed_need else []
    return render_template("admin/user_detail.html", u=u, p=u.profile, intros=intros, matches=matches,
                           roles=ROLES, consent_kinds=CONSENT_KINDS)


MEMBER_TEXT_FIELDS = {"headline": 160, "company": 160, "industry": 120, "city": 120, "bio": 2000, "q_focus": 800,
                      "q_challenge": 800, "q_can_help": 800, "q_looking_for": 800, "expertise": 500}


@bp.route("/mitglieder/<int:user_id>/bearbeiten", methods=["GET", "POST"])
def user_edit(user_id):
    """Stammdaten und Profiltexte korrigieren. Einwilligungen bleiben unberührt (append-only, nur durch die Person)."""
    u = db.session.get(User, user_id) or abort(404)
    if u.is_superadmin and not current_user.is_superadmin:
        abort(403)
    if u.profile is None:
        u.profile = Profile()
    p = u.profile
    errors: dict[str, str] = {}
    if request.method == "POST":
        f = request.form
        email = f.get("email", "").strip().lower()
        first = f.get("first_name", "").strip()
        if not first:
            errors["first_name"] = "Vorname fehlt."
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors["email"] = "Ungültige E-Mail-Adresse."
        elif User.query.filter(User.email == email, User.id != u.id).first():
            errors["email"] = "Diese E-Mail wird bereits verwendet."
        if not errors:
            u.first_name, u.last_name = first[:80], f.get("last_name", "").strip()[:80]
            u.email, u.phone = email, f.get("phone", "").strip()[:40]
            for field, limit in MEMBER_TEXT_FIELDS.items():
                setattr(p, field, f.get(field, "").strip()[:limit])
            p.preferred_formats = ",".join(k for k in f.getlist("formats") if k in FORMATS)
            for key in SOCIALS:
                setattr(p, f"{key}_url", clean_social(key, f.get(f"{key}_url", "")))
            if f.get("remove_photo") and p.photo:  # Moderation: unpassendes Foto entfernen
                media.delete_avatar(p.photo)
                p.photo = None
            matching.refresh_embeddings(p, commit=False)
            audit("user.edit", f"user:{u.id}", u.email)
            db.session.commit()
            flash("Mitglied gespeichert.", "success")
            return redirect(url_for("admin.user_detail", user_id=u.id))
    return render_template("admin/user_edit.html", u=u, p=p, errors=errors, formats=FORMATS, socials=SOCIALS,
                           form=request.form if request.method == "POST" else None)


@bp.route("/mitglieder/<int:user_id>/als-nutzer", methods=["POST"])
def impersonate(user_id):
    """Demo-Modus: als dieses Mitglied ansehen (nur mit ENABLE_IMPERSONATION=1)."""
    if not current_app.config.get("ENABLE_IMPERSONATION"):
        abort(404)
    target = db.session.get(User, user_id) or abort(404)
    if target.id == current_user.id or target.status != "active" or (target.is_superadmin and not current_user.is_superadmin):
        flash("Dieses Konto kann nicht übernommen werden.", "error")
        return redirect(url_for("admin.user_detail", user_id=user_id))
    admin_id = current_user.id
    audit("impersonate.start", f"user:{target.id}", target.email)
    db.session.commit()
    login_user(target, remember=False)
    session["impersonator_id"] = admin_id
    return redirect(url_for("member.dashboard"))


@bp.route("/mitglieder/<int:user_id>/<action>", methods=["POST"])
def user_action(user_id, action):
    u = db.session.get(User, user_id) or abort(404)
    if u.id == current_user.id and action in ("sperren", "loeschen", "rolle"):
        flash("Das eigene Konto kann hier nicht geändert werden.", "error")
        return redirect(url_for("admin.user_detail", user_id=u.id))
    if u.is_superadmin and not current_user.is_superadmin:
        abort(403)
    if action == "sperren":
        u.status = "blocked"
        if u.telegram_user_id:
            telegram.kick(u.telegram_user_id)
        audit("user.block", f"user:{u.id}")
        flash(f"{u.full_name} ist gesperrt.", "info")
    elif action == "entsperren":
        u.status = "active"
        audit("user.unblock", f"user:{u.id}")
        flash(f"{u.full_name} ist wieder aktiv.", "success")
    elif action == "rolle":
        if not current_user.is_superadmin:
            abort(403)
        role = request.form.get("role")
        if role not in ROLES:
            abort(400)
        audit("user.role", f"user:{u.id}", f"{u.role} -> {role}")
        u.role = role
        flash("Rolle geändert.", "success")
    elif action == "export":
        audit("user.export", f"user:{u.id}")
        db.session.commit()
        return Response(json.dumps(export_user(u), ensure_ascii=False, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename=export-user-{u.id}.json"})
    elif action == "loeschen":
        if not current_user.is_superadmin:
            abort(403)
        if u.telegram_user_id:
            telegram.kick(u.telegram_user_id)
        audit("user.delete", f"user:{u.id}", u.email)
        db.session.commit()
        delete_user(u)
        flash("Konto und zugehörige Daten gelöscht (Art. 17 DSGVO).", "info")
        return redirect(url_for("admin.users"))
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("admin.user_detail", user_id=u.id))


# --------------------------------------------------------------------------- Expert:innen-Suche
@bp.route("/suche")
def expert_search():
    q = request.args.get("q", "").strip()[:300]
    hits = matching.semantic_search(q, k=20) if q else []
    events = (Event.query.filter(Event.status == "published", Event.starts_at >= utcnow())
              .order_by(Event.starts_at).limit(20).all())
    return render_template("admin/search.html", q=q, hits=hits, events=events)


# --------------------------------------------------------------------------- Termine
@bp.route("/termine")
def events():
    show = request.args.get("show", "upcoming")
    q = Event.query
    if show == "pending":
        q = q.filter_by(status="pending")
    elif show == "past":
        q = q.filter(Event.starts_at < utcnow())
    else:
        q = q.filter(Event.starts_at >= utcnow() - timedelta(hours=6))
    evs = q.order_by(Event.starts_at.desc() if show == "past" else Event.starts_at).limit(200).all()
    return render_template("admin/events.html", events=evs, show=show)


@bp.route("/termine/sync", methods=["POST"])
def events_sync():
    try:
        res = sync_events()
        audit("events.sync", details=str(res))
        db.session.commit()
        flash(f"Kalender synchronisiert: {res['created']} neu, {res['updated']} aktualisiert.", "success")
    except Exception as exc:
        flash(f"Synchronisierung fehlgeschlagen: {exc}", "error")
    return redirect(url_for("admin.events"))


def _event_from_form(ev: Event, f) -> dict:
    errors = {}
    ev.title = f.get("title", "").strip()[:200]
    ev.format = f.get("format") if f.get("format") in FORMATS else "community"
    ev.description = f.get("description", "").strip()[:5000]
    ev.location = f.get("location", "").strip()[:255]
    ev.online_url = f.get("online_url", "").strip()[:400]
    ev.time_pending = bool(f.get("time_pending"))
    ev.all_day = bool(f.get("all_day"))
    ev.status = f.get("status") if f.get("status") in ("published", "pending", "cancelled") else "published"
    try:
        ev.price_cents = int(round(float((f.get("price") or "0").replace(",", ".")) * 100))
    except ValueError:
        errors["price"] = "Preis ungültig."
    ev.capacity = int(f.get("capacity")) if (f.get("capacity") or "").isdigit() else None
    try:
        start = datetime.strptime(f"{f.get('date')} {f.get('time') or '00:00'}", "%Y-%m-%d %H:%M")
        ev.starts_at = local_to_utc(start)
        if f.get("end_time"):
            end = datetime.strptime(f"{f.get('date')} {f.get('end_time')}", "%Y-%m-%d %H:%M")
            ev.ends_at = local_to_utc(end)
    except ValueError:
        errors["date"] = "Bitte ein gültiges Datum angeben."
    if not ev.title:
        errors["title"] = "Titel fehlt."
    return errors


def auto_invite(ev: Event, was_published: bool) -> int:
    """Beim Veröffentlichen eines eigenen Termins passende Mitglieder per KI einladen (abschaltbar in den Einstellungen)."""
    if was_published or ev.status != "published" or ev.source == "sync" or Setting.get("auto_invites") != "1":
        return 0
    if ev.starts_at < utcnow():
        return 0
    n = len(insights.invite_for_event(ev))
    if n:
        audit("event.ai_invites", f"event:{ev.id}", str(n))
        db.session.commit()
    return n


@bp.route("/termine/neu", methods=["GET", "POST"])
@bp.route("/termine/<int:event_id>/bearbeiten", methods=["GET", "POST"])
def event_edit(event_id=None):
    ev = db.session.get(Event, event_id) if event_id else Event(source="admin", created_by_id=current_user.id)
    if ev is None:
        abort(404)
    errors = {}
    if request.method == "POST":
        was_published = bool(ev.id and ev.status == "published")
        errors = _event_from_form(ev, request.form)
        if not errors:
            if not ev.id:
                db.session.add(ev)
            audit("event.save", f"event:{ev.id or 'neu'}", ev.title)
            db.session.commit()
            invited = auto_invite(ev, was_published)
            flash("Termin gespeichert." + (f" Aiko hat {invited} passende Mitglieder eingeladen." if invited else ""),
                  "success")
            return redirect(url_for("admin.event_attendees", event_id=ev.id))
    if not ev.starts_at:
        ev.starts_at = local_to_utc(datetime.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=7))
    if not ev.format:
        ev.format = "hub"
    return render_template("admin/event_form.html", ev=ev, errors=errors, local=to_local(ev.starts_at),
                           local_end=to_local(ev.ends_at) if ev.ends_at else None)


@bp.route("/termine/<int:event_id>/status/<status>", methods=["POST"])
def event_status(event_id, status):
    ev = db.session.get(Event, event_id) or abort(404)
    if status not in ("published", "cancelled", "pending"):
        abort(400)
    was_published = ev.status == "published"
    ev.status = status
    audit("event.status", f"event:{ev.id}", status)
    db.session.commit()
    invited = auto_invite(ev, was_published)
    if invited:
        flash(f"Aiko hat {invited} passende Mitglieder eingeladen.", "success")
    flash({"published": "Termin veröffentlicht.", "cancelled": "Termin abgesagt.", "pending": "Termin zurückgestellt."}[status],
          "success")
    return redirect(request.referrer or url_for("admin.events"))


@bp.route("/termine/<int:event_id>")
def event_attendees(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    regs = sorted(ev.registrations, key=lambda r: r.created_at)
    return render_template("admin/event_attendees.html", ev=ev, regs=regs, tg=telegram.group_enabled())


@bp.route("/termine/<int:event_id>/teilnehmende.csv")
def event_csv(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["name", "email", "telefon", "rolle_unternehmen", "status", "betrag_eur", "notiz", "angemeldet"])
    for r in ev.registrations:
        p = r.user.profile or Profile()
        w.writerow([r.user.full_name, r.user.email, r.user.phone, p.headline, r.status,
                    f"{(r.amount_cents or 0) / 100:.2f}", r.note, fmt_dt(r.created_at)])
    audit("event.export_csv", f"event:{ev.id}")
    db.session.commit()
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=teilnehmende-{ev.id}.csv"})


@bp.route("/termine/<int:event_id>/anmeldung/<int:reg_id>", methods=["POST"])
def registration_update(event_id, reg_id):
    reg = db.session.get(Registration, reg_id) or abort(404)
    status = request.form.get("status")
    if reg.event_id != event_id or status not in ("registered", "paid", "reserved", "cancelled", "pending_payment"):
        abort(400)
    audit("registration.status", f"registration:{reg.id}", f"{reg.status} -> {status}")
    reg.status = status
    db.session.commit()
    flash("Anmeldung aktualisiert.", "success")
    return redirect(url_for("admin.event_attendees", event_id=event_id))


@bp.route("/termine/<int:event_id>/einladungen", methods=["GET", "POST"])
def event_invites(event_id):
    """Vorschau, wen die KI zu diesem Termin einladen würde — und Versand per Klick."""
    ev = db.session.get(Event, event_id) or abort(404)
    if request.method == "POST":
        created = insights.invite_for_event(ev)
        audit("event.ai_invites", f"event:{ev.id}", str(len(created)))
        db.session.commit()
        flash(f"{len(created)} Einladungen versendet." if created else "Keine weiteren passenden Mitglieder gefunden.",
              "success" if created else "info")
        return redirect(url_for("admin.event_invites", event_id=ev.id))
    cands = insights.invite_candidates(ev, limit=12)
    sent = Notification.query.filter_by(event_id=ev.id, kind="event_invite").order_by(Notification.score.desc()).all()
    return render_template("admin/event_invites.html", ev=ev, cands=cands, sent=sent)


@bp.route("/termine/<int:event_id>/matching")
def event_matching(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    ids = [r.user_id for r in ev.active_registrations]
    pairings = matching.event_pairings(ids)
    without = [r.user for r in ev.active_registrations
               if not (r.user.profile and r.user.profile.is_matchable and r.user.profile.embed_need)]
    return render_template("admin/event_matching.html", ev=ev, pairings=pairings, without=without)


@bp.route("/termine/<int:event_id>/ankuendigen", methods=["POST"])
def event_announce(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    text = request.form.get("text", "").strip() or (
        f"{ev.title}\n{fmt_event_date(ev)}\n{ev.location or ''}\n\n{(ev.description or '')[:600]}\n\n"
        f"Anmeldung: {url_for('member.event_detail', event_id=ev.id, _external=True)}")
    try:
        telegram.announce(text)
        audit("event.announce", f"event:{ev.id}")
        db.session.commit()
        flash("Im Telegram-Community-Chat angekündigt.", "success")
    except Exception as exc:
        flash(f"Ankündigung fehlgeschlagen: {exc}", "error")
    return redirect(url_for("admin.event_attendees", event_id=ev.id))


# --------------------------------------------------------------------------- Kontaktanfragen
@bp.route("/kontakte")
def intros():
    items = IntroRequest.query.order_by(IntroRequest.created_at.desc()).limit(300).all()
    return render_template("admin/intros.html", items=items)


# --------------------------------------------------------------------------- Leistungen
@bp.route("/leistungen")
def services():
    return render_template("admin/services.html", items=Service.query.order_by(Service.sort).all(),
                           inquiries=ServiceInquiry.query.order_by(ServiceInquiry.created_at.desc()).limit(100).all())


@bp.route("/leistungen/neu", methods=["GET", "POST"])
@bp.route("/leistungen/<int:sid>", methods=["GET", "POST"])
def service_edit(sid=None):
    s = db.session.get(Service, sid) if sid else Service(active=True)
    if s is None:
        abort(404)
    if request.method == "POST":
        f = request.form
        for field, n in (("title", 160), ("summary", 300), ("description", 5000), ("benefits", 3000),
                         ("member_benefit", 300), ("price_hint", 120), ("provider_name", 160),
                         ("provider_email", 255)):
            setattr(s, field, f.get(field, "").strip()[:n])
        s.slug = re.sub(r"[^a-z0-9-]+", "-", (f.get("slug") or s.title).lower()).strip("-")[:80] or "leistung"
        s.active = bool(f.get("active"))
        s.sort = int(f.get("sort") or 0) if (f.get("sort") or "0").lstrip("-").isdigit() else 0
        clash = Service.query.filter(Service.slug == s.slug, Service.id != (s.id or 0)).first()
        if not s.title or clash:
            flash("Titel fehlt oder Kurzname (Slug) ist bereits vergeben.", "error")
        else:
            if not s.id:
                db.session.add(s)
            audit("service.save", f"service:{s.slug}")
            db.session.commit()
            flash("Leistung gespeichert.", "success")
            return redirect(url_for("admin.services"))
    return render_template("admin/service_form.html", s=s)


@bp.route("/anfragen/<int:iid>", methods=["POST"])
def inquiry_status(iid):
    inq = db.session.get(ServiceInquiry, iid) or abort(404)
    status = request.form.get("status")
    if status not in ("new", "contacted", "won", "lost"):
        abort(400)
    inq.status = status
    db.session.commit()
    return redirect(url_for("admin.services"))


# --------------------------------------------------------------------------- Wissen (FAQ / Aiko)
@bp.route("/wissen", methods=["GET", "POST"])
def knowledge():
    if request.method == "POST":
        f = request.form
        kid = f.get("id")
        k = db.session.get(KnowledgeItem, int(kid)) if kid else KnowledgeItem()
        if f.get("delete") and k.id:
            db.session.delete(k)
            audit("knowledge.delete", f"knowledge:{k.id}")
        else:
            k.question = f.get("question", "").strip()[:300]
            k.answer = f.get("answer", "").strip()[:5000]
            k.public = bool(f.get("public"))
            k.active = bool(f.get("active"))
            k.sort = int(f.get("sort") or 0) if (f.get("sort") or "0").isdigit() else 0
            if not k.question or not k.answer:
                flash("Frage und Antwort sind nötig.", "error")
                return redirect(url_for("admin.knowledge"))
            if not k.id:
                db.session.add(k)
            audit("knowledge.save", k.question[:80])
        db.session.commit()
        flash("Wissensbasis aktualisiert.", "success")
        return redirect(url_for("admin.knowledge"))
    return render_template("admin/knowledge.html", items=KnowledgeItem.query.order_by(KnowledgeItem.sort).all())


# --------------------------------------------------------------------------- Protokolle & Einstellungen
@bp.route("/einwilligungen")
def consents():
    items = Consent.query.order_by(Consent.created_at.desc()).limit(500).all()
    return render_template("admin/consents.html", items=items, kinds=CONSENT_KINDS)


@bp.route("/protokoll")
@superadmin_required
def audit_log():
    items = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return render_template("admin/audit.html", items=items)


@bp.route("/einstellungen", methods=["GET", "POST"])
@superadmin_required
def settings():
    if request.method == "POST":
        f = request.form
        Setting.set("member_events_require_approval", "1" if f.get("member_events_require_approval") else "0")
        Setting.set("auto_invites", "1" if f.get("auto_invites") else "0")
        Setting.set("aiko_extra_instructions", f.get("aiko_extra_instructions", "").strip()[:4000])
        Setting.set("telegram_group_title", f.get("telegram_group_title", "").strip()[:120])
        Setting.set("announcement", f.get("announcement", "").strip()[:500])
        audit("settings.save")
        db.session.commit()
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("admin.settings"))
    values = {k: Setting.get(k) for k in Setting.DEFAULTS}
    return render_template("admin/settings.html", v=values, tg=telegram.enabled(), group=telegram.group_enabled())


@bp.route("/matching/neu-berechnen", methods=["POST"])
@superadmin_required
def reembed():
    n = matching.reembed_all()
    audit("matching.reembed", details=str(n))
    db.session.commit()
    flash(f"{n} Profile neu berechnet.", "success")
    return redirect(url_for("admin.settings"))
