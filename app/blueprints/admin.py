from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user
from sqlalchemy import func

from .. import questionnaire
from ..extensions import db, limiter
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

    name = club_settings.settings()["assistant_name"]
    labels = {"aiko_public": f"{name} (Website)", "aiko_member": f"{name} (Mitglieder)", "matching": "Matching-Begründungen",
              "profile_interview": "Profil-Interview", "goal_checkin": "Ziel-Check-ins", "club_insights": "Klub-Auswertung",
              "seo_report": "SEO-/Compliance-Check", "market_panel": "Markt-Panel", "profile_coach": "Profil-Coach", "pair_insight": "Kontakt-Assistent",
              "event_invite": "Termin-Einladungen", "law_search": "Gesetzes-Suche"}
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


# Fragebogen-Freitexte, die die Klubleitung korrigieren darf (private Zielangaben der Person bleiben außen vor)
ADMIN_QUESTIONNAIRE_TEXT = ("markets", "asked_for", "proud_of", "talk_topics", "unknown_fact")
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
        elif email != u.email and not current_user.is_superadmin:
            # Kontoübernahme verhindern: mit fremder Adresse ließe sich später das Passwort zurücksetzen
            errors["email"] = "Die E-Mail-Adresse kann nur die Klubleitung ändern."
        if not errors:
            u.first_name, u.last_name = first[:80], f.get("last_name", "").strip()[:80]
            if email != u.email:
                audit("user.email", f"user:{u.id}", f"{u.email} -> {email}")
                u.end_sessions()
            u.email, u.phone = email, f.get("phone", "").strip()[:40]
            for field, limit in MEMBER_TEXT_FIELDS.items():
                setattr(p, field, f.get(field, "").strip()[:limit])
            values = {field: f.get(field, "") for field in ADMIN_QUESTIONNAIRE_TEXT}
            values.update({field: f.getlist(field) for field in questionnaire.CHOICE_FIELDS})
            values.update({f"custom:{q.key}": (f.getlist(f"custom_{q.key}") if q.kind == "multi" else
                                               f.get(f"custom_{q.key}", "")) for q in questionnaire.club_questions()})
            questionnaire.apply_values(p, values)
            _assign_member_plan(u, f)
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
    from ..models import Plan as _Plan
    from ..services import plans as _plans
    return render_template("admin/user_edit.html", u=u, p=p, errors=errors, formats=FORMATS, socials=SOCIALS,
                           member_plans=_Plan.query.filter_by(kind="member").order_by(_Plan.sort).all(),
                           stripe_managed=u.plan_source == "stripe" and u.plan_status in _plans.PAID_STATUS,
                           form=request.form if request.method == "POST" else None)


def _assign_member_plan(u: User, f) -> None:
    """Tarif vom Klub vergeben (z. B. Vorstand, Partner, Kulanz). Laufende Stripe-Abos bleiben unangetastet."""
    from ..models import Plan as _Plan
    from ..services import plans as _plans
    key = f.get("plan_key")
    if not key or (u.plan_source == "stripe" and u.plan_status in _plans.PAID_STATUS):
        return
    if not _Plan.query.filter_by(key=key, kind="member").first():
        return
    until = None
    if f.get("plan_until"):
        try:
            until = datetime.strptime(f["plan_until"], "%Y-%m-%d") + timedelta(hours=23, minutes=59)
        except ValueError:
            until = None
    before = (u.plan_key, u.plan_until)
    if key == "basis":
        u.plan_key, u.plan_source, u.plan_until = "basis", "", None
    else:
        u.plan_key, u.plan_source, u.plan_until = key, "admin", until
    if before != (u.plan_key, u.plan_until):
        audit("user.plan", f"user:{u.id}", f"{key} bis {until:%d.%m.%Y}" if until else key)


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
        u.end_sessions()
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
        u.end_sessions()
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
    if not ev.id and request.method == "GET":  # Vorbelegung, z. B. aus einer Terminidee der Auswertung
        ev.title = request.args.get("title", "")[:200]
        ev.description = request.args.get("description", "")[:2000]
        if request.args.get("format") in FORMATS:
            ev.format = request.args["format"]
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
            flash("Termin gespeichert." + (f" {club_settings.settings()['assistant_name']} hat {invited} passende Mitglieder eingeladen." if invited else ""),
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
        flash(f"{club_settings.settings()['assistant_name']} hat {invited} passende Mitglieder eingeladen.", "success")
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


# --------------------------------------------------------------------------- Klub & Branding (White-Label)
from ..club_presets import PRESETS, apply_preset, export_config, import_config  # noqa: E402
from ..models import MeetingFormat, invalidate_formats  # noqa: E402
from ..services import club as club_settings  # noqa: E402

IMAGE_SLOTS = {"logo": "Logo", **{f"hero{i}": f"Startseite oben, Bild {i}" for i in (1, 2, 3)},
               **{f"band{i}": f"Bildleiste, Bild {i}" for i in (1, 2, 3)}}


def _slot_get(c, slot):
    if slot == "logo":
        return c.logo
    key, idx = ("hero_images" if slot.startswith("hero") else "band_images"), int(slot[-1]) - 1
    items = c[key]
    return items[idx]["src"] if idx < len(items) else ""


def _slot_set(slot, ref):
    c = club_settings.settings()
    if slot == "logo":
        club_settings.save({"logo": ref})
        return
    key, idx = ("hero_images" if slot.startswith("hero") else "band_images"), int(slot[-1]) - 1
    items = [dict(i) for i in c[key]]
    while len(items) <= idx:
        items.append({"src": "", "alt": ""})
    items[idx]["src"] = ref
    club_settings.save({key: [i for i in items if i.get("src")]})


@bp.route("/klub", methods=["GET", "POST"])
@superadmin_required
def club_page():
    if request.method == "POST":
        f = request.form
        values = {k: f.get(k, "") for k in club_settings.TEXT_FIELDS}
        if not club_settings.HEX.match(values.get("accent", "")):
            values["accent"] = club_settings.settings()["accent"]
        testimonials = []
        for line in f.get("testimonials", "").splitlines():
            if "|" in line:
                name, text = line.split("|", 1)
                if name.strip() and text.strip():
                    testimonials.append({"name": name.strip()[:60], "text": text.strip()[:400]})
        values["testimonials"] = testimonials[:8]
        c = club_settings.settings()
        old_name, new_name = c["assistant_name"], (values.get("assistant_name") or "").strip()
        for key in ("hero_images", "band_images"):
            items = [dict(i) for i in c[key]]
            for i, item in enumerate(items):
                item["alt"] = f.get(f"{key}_alt_{i}", item.get("alt", "")).strip()[:160]
            values[key] = items
        club_settings.save(values)
        renamed = _rename_assistant(old_name, new_name) if new_name and new_name != old_name else 0
        audit("club.settings", details=f"KI-Assistenz umbenannt in {new_name} ({renamed} Texte)" if renamed else "")
        db.session.commit()
        flash("Klub-Einstellungen gespeichert." + (f" Der neue Name der KI-Assistenz steht jetzt auch in {renamed} "
                                                  "Leistungs- und FAQ-Texten." if renamed else ""), "success")
        return redirect(url_for("admin.club_page"))
    c = club_settings.settings()
    return render_template("admin/club.html", c=c, fields=club_settings.TEXT_FIELDS, presets=PRESETS,
                           slots=IMAGE_SLOTS, slot_value=lambda s: _slot_get(c, s),
                           testimonials="\n".join(f"{t['name']} | {t['text']}" for t in c.testimonials))


def _rename_assistant(old: str, new: str) -> int:
    """Namen der KI-Assistenz in Leistungs- und FAQ-Texten des Klubs nachziehen (ganze Wörter). Gibt die Zahl der
    geänderten Einträge zurück."""
    pattern = re.compile(rf"\b{re.escape(old)}\b")
    changed = 0
    for obj, fields in [(s, ("title", "summary", "description", "benefits", "member_benefit", "provider_name"))
                        for s in Service.query.all()] + \
                       [(k, ("question", "answer")) for k in KnowledgeItem.query.all()]:
        hit = False
        for field in fields:
            value = getattr(obj, field) or ""
            if pattern.search(value):
                setattr(obj, field, pattern.sub(new, value))
                hit = True
        changed += hit
    return changed


@bp.route("/klub/bild/<slot>", methods=["POST"])
@superadmin_required
def club_image(slot):
    if slot not in IMAGE_SLOTS:
        abort(404)
    old = _slot_get(club_settings.settings(), slot)
    if request.form.get("remove"):
        media.delete_club_image(old)
        _slot_set(slot, "")
        flash(f"{IMAGE_SLOTS[slot]} entfernt.", "info")
    else:
        upload = request.files.get("image")
        if not upload or not upload.filename:
            flash("Bitte eine Bilddatei auswählen.", "error")
            return redirect(url_for("admin.club_page") + "#bilder")
        try:
            ref = media.save_club_image(upload, "logo" if slot == "logo" else "photo")
        except media.PhotoError as exc:
            flash(str(exc), "error")
            return redirect(url_for("admin.club_page") + "#bilder")
        media.delete_club_image(old)
        _slot_set(slot, ref)
        flash(f"{IMAGE_SLOTS[slot]} aktualisiert.", "success")
    audit("club.image", slot)
    db.session.commit()
    return redirect(url_for("admin.club_page") + "#bilder")


@bp.route("/klub/vorlage", methods=["POST"])
@superadmin_required
def club_preset():
    name = request.form.get("preset")
    if name not in PRESETS:
        abort(400)
    apply_preset(name, replace_faq=bool(request.form.get("replace_faq")))
    audit("club.preset", name)
    db.session.commit()
    flash(f"Vorlage „{PRESETS[name]['label']}“ angewendet.", "success")
    return redirect(url_for("admin.club_page"))


@bp.route("/klub/export")
@superadmin_required
def club_export():
    data = json.dumps(export_config(), ensure_ascii=False, indent=2)
    audit("club.export")
    db.session.commit()
    name = club_settings.slug(club_settings.settings()["name"])
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=klub-{name}.json"})


@bp.route("/klub/import", methods=["POST"])
@superadmin_required
def club_import():
    upload = request.files.get("config")
    try:
        data = json.loads((upload.read(1_000_000) if upload else b"").decode("utf-8"))
        import_config(data, with_faq=bool(request.form.get("with_faq")))
    except (ValueError, UnicodeDecodeError) as exc:
        db.session.rollback()
        flash(f"Import fehlgeschlagen: {exc}", "error")
        return redirect(url_for("admin.club_page"))
    audit("club.import")
    db.session.commit()
    flash("Konfiguration übernommen. Hochgeladene Bilder bitte neu hochladen.", "success")
    return redirect(url_for("admin.club_page"))


# --------------------------------------------------------------------------- Formate
@bp.route("/formate")
@superadmin_required
def formats():
    if MeetingFormat.query.count() == 0:  # ältere Installation: aus der aktuellen Liste anlegen
        from ..club_presets import apply_formats, preset_formats
        apply_formats(preset_formats("mainpower"))
        db.session.commit()
    items = MeetingFormat.query.order_by(MeetingFormat.sort, MeetingFormat.id).all()
    return render_template("admin/formats.html", items=items)


@bp.route("/formate/neu", methods=["GET", "POST"])
@bp.route("/formate/<int:fid>", methods=["GET", "POST"])
@superadmin_required
def format_edit(fid=None):
    fmt = db.session.get(MeetingFormat, fid) if fid else MeetingFormat(active=True, featured=True)
    if fmt is None:
        abort(404)
    errors = {}
    if request.method == "POST":
        f = request.form
        key = club_settings.slug(f.get("key", "") or f.get("name", ""))[:40] if not fmt.id else fmt.key
        if not f.get("name", "").strip():
            errors["name"] = "Bitte einen Namen angeben."
        if not fmt.id and MeetingFormat.query.filter_by(key=key).first():
            errors["key"] = "Diesen Schlüssel gibt es schon."
        try:
            price = int(round(float((f.get("price") or "0").replace(",", ".")) * 100))
            if price < 0:
                raise ValueError
        except ValueError:
            errors["price"] = "Bitte einen Preis in Euro angeben (z. B. 25 oder 0)."
            price = 0
        if not errors:
            fmt.key = key
            for field, limit in (("name", 120), ("short", 40), ("tagline", 160), ("rhythm", 160), ("price_note", 300)):
                setattr(fmt, field, f.get(field, "").strip()[:limit])
            fmt.description = f.get("description", "").strip()[:2000]
            fmt.details = f.get("details", "").strip()[:4000]
            fmt.price_cents = price
            fmt.sort = int(f.get("sort")) if (f.get("sort") or "").lstrip("-").isdigit() else (fmt.sort or 0)
            fmt.featured = bool(f.get("featured"))
            fmt.active = True if fmt.system else bool(f.get("active"))
            upload = request.files.get("image")
            if upload and upload.filename:
                try:
                    ref = media.save_club_image(upload, "photo")
                    media.delete_club_image(fmt.image)
                    fmt.image = ref
                except media.PhotoError as exc:
                    errors["image"] = str(exc)
            if not errors:
                db.session.add(fmt)
                audit("format.save", f"format:{fmt.key}")
                db.session.commit()
                invalidate_formats()
                flash("Format gespeichert.", "success")
                return redirect(url_for("admin.formats"))
    return render_template("admin/format_form.html", fmt=fmt, errors=errors)


# --------------------------------------------------------------------------- Auswertung (Nachfrage/Angebot, Themen)
from ..models import ClubQuestion  # noqa: E402
from ..services import club_insights  # noqa: E402


@bp.route("/auswertung")
def analytics():
    return render_template("admin/analytics.html", ov=club_insights.overview(), report=club_insights.cached_report())


@bp.route("/auswertung/analyse", methods=["POST"])
@limiter.limit("10 per hour")
def analytics_run():
    report = club_insights.build_report()
    audit("insights.report", details=f"ai={report.get('ai')}")
    db.session.commit()
    flash("Themen neu ausgewertet." if report.get("ai") else
          "Themen per Stichwortzählung ausgewertet (KI gerade nicht verfügbar).", "success")
    return redirect(url_for("admin.analytics") + "#themen")


# --------------------------------------------------------------------------- Klubeigene Fragen im Profil
MAX_QUESTIONS = 8


@bp.route("/fragen")
@superadmin_required
def questions():
    items = ClubQuestion.query.order_by(ClubQuestion.sort, ClubQuestion.id).all()
    return render_template("admin/questions.html", items=items, max_questions=MAX_QUESTIONS)


@bp.route("/fragen/neu", methods=["GET", "POST"])
@bp.route("/fragen/<int:qid>", methods=["GET", "POST"])
@superadmin_required
def question_edit(qid=None):
    q = db.session.get(ClubQuestion, qid) if qid else ClubQuestion(kind="text", use="none", public=True, active=True)
    if q is None:
        abort(404)
    if not q.id and ClubQuestion.query.count() >= MAX_QUESTIONS:
        flash(f"Höchstens {MAX_QUESTIONS} eigene Fragen – kurze Profile werden eher ausgefüllt.", "error")
        return redirect(url_for("admin.questions"))
    errors = {}
    if request.method == "POST":
        f = request.form
        label = f.get("label", "").strip()[:200]
        kind = f.get("kind") if f.get("kind") in ClubQuestion.KINDS else "text"
        options = [o.strip()[:80] for o in f.get("options", "").splitlines() if o.strip()][:20]
        if not label:
            errors["label"] = "Bitte die Frage eingeben."
        if kind != "text" and len(options) < 2:
            errors["options"] = "Mindestens zwei Antwortoptionen, eine pro Zeile."
        if not q.id:
            key = club_settings.slug(label)[:40] or "frage"
            base, i = key, 2
            while ClubQuestion.query.filter_by(key=key).first():
                key, i = f"{base[:36]}-{i}", i + 1
            q.key = key
        if not errors:
            q.label, q.kind, q.help = label, kind, f.get("help", "").strip()[:300]
            q.options = options if kind != "text" else None
            q.use = f.get("use") if f.get("use") in ClubQuestion.USES else "none"
            q.public = bool(f.get("public"))
            q.active = bool(f.get("active"))
            q.sort = int(f.get("sort")) if (f.get("sort") or "").lstrip("-").isdigit() else (q.sort or 0)
            db.session.add(q)
            audit("question.save", f"question:{q.key}", label)
            db.session.commit()
            flash("Frage gespeichert. Sie erscheint im Profil unter „Fragen von " + club_settings.settings()["name"] + "“."
                  if q.active else "Frage gespeichert (inaktiv).", "success")
            return redirect(url_for("admin.questions"))
    return render_template("admin/question_form.html", q=q, errors=errors, form=request.form)


@bp.route("/fragen/<int:qid>/loeschen", methods=["POST"])
@superadmin_required
def question_delete(qid):
    q = db.session.get(ClubQuestion, qid) or abort(404)
    affected = [p for p in Profile.query.filter(Profile.custom_answers.isnot(None))
                if q.key in (p.custom_answers or {})]
    for p in affected:
        p.custom_answers = {k: v for k, v in p.custom_answers.items() if k != q.key}
    label, use = q.label, q.use
    db.session.delete(q)
    db.session.flush()
    if use in ("need", "offer"):
        for p in affected:
            matching.refresh_embeddings(p, commit=False)
    audit("question.delete", f"question:{q.key}", f"{label} ({len(affected)} Antworten entfernt)")
    db.session.commit()
    flash(f"Frage gelöscht, {len(affected)} Antworten entfernt.", "success")
    return redirect(url_for("admin.questions"))


# --------------------------------------------------------------------------- Tarif des Klubs, Budget, Provision
from ..models import Payment, Plan  # noqa: E402
from ..services import billing as billing_service, plans  # noqa: E402
from ..tenancy import current_club  # noqa: E402


@bp.route("/tarif")
def billing():
    club = current_club()
    if request.args.get("portal") and club and club.stripe_subscription_id:
        try:
            billing_service.sync_subscription(club.stripe_subscription_id)
        except Exception:
            current_app.logger.exception("Abo-Abgleich fehlgeschlagen")
            db.session.rollback()
        club = current_club()
    if request.args.get("session_id") and current_user.is_superadmin:
        try:
            billing_service.sync_checkout(request.args["session_id"], club=club)
        except Exception:
            current_app.logger.exception("Checkout-Abgleich fehlgeschlagen")
            db.session.rollback()
        club = current_club()
    month = plans.month_start()
    members = User.query.filter(User.status == "active").count()
    by_plan = dict(db.session.query(User.plan_key, func.count(User.id)).filter(User.status == "active")
                   .group_by(User.plan_key).all())
    paying = [u for u in User.query.filter(User.plan_key != "basis").order_by(User.first_name).all()
              if not plans.user_plan(u).is_free]
    revenue = (db.session.query(func.coalesce(func.sum(Payment.amount_cents), 0))
               .filter(Payment.kind == "member_plan", Payment.created_at >= month).scalar()) or 0
    return render_template("admin/billing.html", club_obj=club, cplan=plans.club_plan(club), budget=plans.club_budget(club),
                           members=members, by_plan=by_plan, paying=paying, revenue=revenue,
                           commission=revenue * plans.commission_pct() / 100, commission_pct=plans.commission_pct(),
                           club_plans=Plan.query.filter_by(kind="club", active=True).order_by(Plan.sort).all(),
                           member_plans={p.key: p for p in Plan.query.filter_by(kind="member")},
                           stripe_on=billing_service.enabled(), vat=plans.vat_rate())




@bp.route("/tarif/<key>", methods=["POST"])
@superadmin_required
def billing_choose(key):
    plan = Plan.query.filter_by(key=key, kind="club", active=True).first_or_404()
    try:
        target = billing_service.checkout_club_plan(current_club(), plan, current_user)
    except billing_service.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.billing"))
    audit("club.plan_checkout", f"plan:{plan.key}")
    db.session.commit()
    return redirect(target)


@bp.route("/tarif/verwalten", methods=["POST"])
@superadmin_required
def billing_portal():
    try:
        return redirect(billing_service.portal_url(current_club().stripe_customer_id,
                                                   url_for("admin.billing", portal=1, _external=True)))
    except billing_service.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.billing"))
