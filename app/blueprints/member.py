from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import (Blueprint, Response, session, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db, limiter
from ..models import (FORMATS, SOCIALS, ChatMessage, Notification, Event, IntroRequest, Match, Profile, Registration, Service,
                      ServiceInquiry, Setting, User, utcnow)
from ..services import aiko, insights, matching, media, payments, telegram
from ..services.audit import audit
from ..services.gdpr import delete_user, export_user
from ..services.mailer import send_mail
from ..utils import clean_social, local_to_utc
from .auth import record_consent
from .public import upcoming

bp = Blueprint("member", __name__)


# Im Demo-Modus "als Nutzer" gesperrt: Aktionen, die das echte Konto dauerhaft verändern oder Daten herausgeben
IMPERSONATION_BLOCKED = {"member.delete_account", "member.change_password", "member.export", "member.community_unlink"}


@bp.before_request
@login_required
def _guard():
    if session.get("impersonator_id") and request.endpoint in IMPERSONATION_BLOCKED:
        flash("Im Demo-Modus „als Nutzer“ ist diese Aktion gesperrt.", "info")
        return redirect(url_for("member.dashboard"))
    if current_user.profile is None:
        current_user.profile = Profile()
        db.session.commit()


# --------------------------------------------------------------------------- Demo-Modus: Konten wechseln
def _impersonator() -> User | None:
    if not current_app.config.get("ENABLE_IMPERSONATION") or not session.get("impersonator_id"):
        return None
    admin = db.session.get(User, int(session["impersonator_id"]))
    return admin if admin and admin.is_admin and admin.status == "active" else None


@bp.route("/als-nutzer/<int:user_id>", methods=["POST"])
def impersonate_switch(user_id):
    admin = _impersonator()
    if admin is None:
        abort(403)
    target = db.session.get(User, user_id) or abort(404)
    if target.status != "active" or (target.is_superadmin and not admin.is_superadmin):
        abort(403)
    audit("impersonate.switch", f"user:{target.id}", target.email, actor_id=admin.id)
    db.session.commit()
    login_user(target, remember=False)
    session["impersonator_id"] = admin.id
    return redirect(request.referrer if request.referrer and request.host in request.referrer else url_for("member.dashboard"))


@bp.route("/als-nutzer-beenden", methods=["POST"])
def impersonate_stop():
    admin = _impersonator()
    if admin is None:
        session.pop("impersonator_id", None)
        return redirect(url_for("member.dashboard"))
    audit("impersonate.stop", f"user:{current_user.id}", actor_id=admin.id)
    db.session.commit()
    session.pop("impersonator_id", None)
    login_user(admin, remember=False)
    return redirect(url_for("admin.users"))


# --------------------------------------------------------------------------- Übersicht
@bp.route("/")
def dashboard():
    p = current_user.profile
    my_regs = (Registration.query.join(Event)
               .filter(Registration.user_id == current_user.id,
                       Registration.status.in_(["registered", "paid", "reserved", "pending_payment"]),
                       Event.starts_at >= utcnow() - timedelta(hours=6))
               .order_by(Event.starts_at).all())
    matches = matching.top_matches(current_user, k=3) if p.is_matchable and p.embed_need else []
    incoming = IntroRequest.query.filter_by(to_user_id=current_user.id, status="pending").all()
    invites = (Notification.query.join(Event, Notification.event_id == Event.id)
               .filter(Notification.user_id == current_user.id, Notification.kind == "event_invite",
                       Event.status == "published", Event.starts_at >= utcnow() - timedelta(hours=6))
               .order_by(Notification.created_at.desc()).limit(4).all())
    return render_template("member/dashboard.html", p=p, regs=my_regs, matches=matches, incoming=incoming,
                           events=upcoming(3), announcement=Setting.get("announcement"), invites=invites)


# --------------------------------------------------------------------------- Profil
PROFILE_TEXT_FIELDS = {
    "headline": 160, "company": 160, "industry": 120, "city": 120, "bio": 1500,
    "q_focus": 800, "q_challenge": 800, "q_can_help": 800, "q_looking_for": 800, "expertise": 500,
}


# Anleitung zu den vier Fragen: Wozu, worauf achten, Beispiele (im Profil ausklappbar, Beispiele per Klick einfügbar)
PROFILE_GUIDE = {
    "q_focus": {
        "why": "Damit andere in einem Satz verstehen, wer du bist und wofür du stehst. Aiko ordnet dich damit in die Community ein.",
        "tips": "Branche, Zielgruppe, was du konkret lieferst. Kein Werbetext.",
        "examples": ["Ich berate mittelständische Unternehmen beim Markteintritt in der Türkei — von Zoll bis Vertriebspartnern.",
                     "Ich baue eine Software für Logistikunternehmen, die Touren automatisch plant."]},
    "q_challenge": {
        "why": "Das ist der wichtigste Baustein für gute Matches: Aus deiner Herausforderung erkennt die KI, wer dir helfen kann.",
        "tips": "Eine aktuelle, echte Hürde — mit einem Beispiel statt eines Schlagworts.",
        "examples": ["Ich finde keine verlässlichen Vertriebspartner im Ausland und verliere Monate mit Absagen.",
                     "Unsere ersten Kunden springen nach dem Test ab. Ich weiß nicht, ob es am Preis oder am Onboarding liegt."]},
    "q_can_help": {
        "why": "Matching funktioniert nur, wenn beide Seiten profitieren. Hier zeigst du, was du anderen geben kannst.",
        "tips": "2–3 Themen, bei denen dich Leute gern anrufen dürfen: Wissen, Erfahrung, Kontakte.",
        "examples": ["Exportrecht, Zollabwicklung und Kontakte zu Spediteuren in Istanbul.",
                     "Positionierung, LinkedIn-Strategie und Launch-Kampagnen für B2B-Start-ups."]},
    "q_looking_for": {
        "why": "Damit Aiko gezielt nach Menschen sucht, die dir jetzt weiterhelfen — nicht nach allen, die irgendwie passen.",
        "tips": "Rolle, Branche oder Aufgabe nennen. Je genauer, desto besser der Treffer.",
        "examples": ["Eine Anwältin für internationales Vertragsrecht mit Erfahrung in der Türkei.",
                     "Tech-Partner, der eine Landingpage samt Automatisierung baut."]},
}


@bp.route("/profil", methods=["GET", "POST"])
def profile():
    p = current_user.profile
    if request.method == "POST":
        f = request.form
        current_user.first_name = f.get("first_name", current_user.first_name).strip()[:80] or current_user.first_name
        current_user.last_name = f.get("last_name", "").strip()[:80]
        current_user.phone = f.get("phone", "").strip()[:40]
        for field, limit in PROFILE_TEXT_FIELDS.items():
            setattr(p, field, f.get(field, "").strip()[:limit])
        p.preferred_formats = ",".join(k for k in f.getlist("formats") if k in FORMATS)
        rejected = []
        for key, (label, _example, _hosts) in SOCIALS.items():
            raw = f.get(f"{key}_url", "").strip()
            val = clean_social(key, raw)
            if raw and not val:
                rejected.append(label)
            setattr(p, f"{key}_url", val)
        if rejected:
            flash("Diese Links wurden nicht übernommen (ungültig oder falsches Netzwerk): " + ", ".join(rejected),
                  "error")
        p.socials_public = bool(f.get("socials_public"))
        upload = request.files.get("photo")
        if f.get("remove_photo") and p.photo:
            media.delete_avatar(p.photo)
            p.photo = None
        if upload and upload.filename:
            try:
                new_name = media.save_avatar(upload)
            except media.PhotoError as exc:
                flash(str(exc), "error")
            else:
                media.delete_avatar(p.photo)
                p.photo = new_name
        p.updated_at = utcnow()
        matching.refresh_embeddings(p, commit=False)
        db.session.commit()
        flash("Profil gespeichert. Deine Matches werden aktualisiert.", "success")
        if not p.allow_matching:
            flash("Hinweis: Matching ist deaktiviert. Aktiviere es unter „Privatsphäre“, um Empfehlungen zu erhalten.",
                  "info")
        return redirect(url_for("member.matches" if request.args.get("welcome") and p.allow_matching
                                else "member.profile"))
    return render_template("member/profile.html", p=p, welcome=request.args.get("welcome"), socials=SOCIALS,
                           guide=PROFILE_GUIDE)


@bp.route("/api/profil-coach", methods=["POST"])
@limiter.limit("20 per hour")
def profile_coach():
    """KI-Feedback zu den (noch ungespeicherten) Profilantworten."""
    data = request.get_json(silent=True) or {}
    return jsonify(insights.profile_coach({k: str(v) for k, v in data.items() if isinstance(v, (str, int))}))


# --------------------------------------------------------------------------- Matches & Kontakte
@bp.route("/matches")
def matches():
    p = current_user.profile
    items = matching.top_matches(current_user, k=8) if p.is_matchable and p.embed_need else []
    existing = {i.to_user_id: i for i in IntroRequest.query.filter_by(from_user_id=current_user.id)}
    return render_template("member/matches.html", p=p, items=items, existing=existing)


@bp.route("/matches/<int:other_id>/ausblenden", methods=["POST"])
def dismiss_match(other_id):
    m = Match.query.filter_by(user_id=current_user.id, other_id=other_id).first_or_404()
    m.dismissed = True
    db.session.commit()
    flash("Empfehlung ausgeblendet.", "info")
    return redirect(url_for("member.matches"))


def _can_contact(target: User) -> bool:
    tp = target.profile
    return bool(target.status == "active" and tp and (tp.allow_matching or tp.visible_in_directory))


@bp.route("/kontakt/<int:user_id>", methods=["POST"])
@limiter.limit("20 per day")
def request_intro(user_id):
    target = db.session.get(User, user_id)
    if not target or target.id == current_user.id or not _can_contact(target):
        abort(404)
    exists = IntroRequest.query.filter(
        ((IntroRequest.from_user_id == current_user.id) & (IntroRequest.to_user_id == target.id)) |
        ((IntroRequest.from_user_id == target.id) & (IntroRequest.to_user_id == current_user.id)),
        IntroRequest.status.in_(["pending", "accepted"])).first()
    if exists:
        flash("Es gibt bereits eine offene oder angenommene Anfrage zwischen euch.", "info")
    else:
        ir = IntroRequest(from_user_id=current_user.id, to_user_id=target.id,
                          message=request.form.get("message", "").strip()[:1000])
        db.session.add(ir)
        db.session.commit()
        send_mail(target.email, f"{current_user.first_name} möchte dich kennenlernen", "intro_request",
                  target=target, sender=current_user, ir=ir, link=url_for("member.contacts", _external=True))
        if target.telegram_user_id and telegram.enabled():
            telegram.send(target.telegram_user_id, f"{current_user.first_name} möchte dich über Main Power "
                                                   f"kennenlernen. Antworte hier: {url_for('member.contacts', _external=True)}")
        flash(f"Anfrage an {target.first_name} gesendet. Kontaktdaten werden geteilt, sobald {target.first_name} zustimmt.",
              "success")
    return redirect(request.referrer or url_for("member.matches"))


@bp.route("/kontakte")
def contacts():
    incoming = (IntroRequest.query.filter_by(to_user_id=current_user.id)
                .order_by(IntroRequest.created_at.desc()).all())
    outgoing = (IntroRequest.query.filter_by(from_user_id=current_user.id)
                .order_by(IntroRequest.created_at.desc()).all())
    return render_template("member/contacts.html", incoming=incoming, outgoing=outgoing)


@bp.route("/kontakte/<int:ir_id>/<action>", methods=["POST"])
def respond_intro(ir_id, action):
    ir = db.session.get(IntroRequest, ir_id) or abort(404)
    if action in ("annehmen", "ablehnen") and ir.to_user_id == current_user.id and ir.status == "pending":
        ir.status = "accepted" if action == "annehmen" else "declined"
        ir.responded_at = utcnow()
        db.session.commit()
        if ir.status == "accepted":
            send_mail(ir.from_user.email, f"{current_user.first_name} hat deine Anfrage angenommen", "intro_accepted",
                      sender=ir.from_user, target=current_user, link=url_for("member.contacts", _external=True))
            flash("Angenommen. Ihr seht jetzt gegenseitig eure Kontaktdaten.", "success")
        else:
            flash("Abgelehnt. Die andere Person erhält keine Kontaktdaten.", "info")
    elif action == "zurueckziehen" and ir.from_user_id == current_user.id and ir.status == "pending":
        ir.status = "withdrawn"
        db.session.commit()
        flash("Anfrage zurückgezogen.", "info")
    else:
        abort(400)
    return redirect(url_for("member.contacts"))


@bp.route("/mitglieder")
def directory():
    q = request.args.get("q", "").strip()[:200]
    branche = request.args.get("branche", "").strip()[:120]
    if q:
        results = [p for _, p in matching.semantic_search(q, k=24, only_directory=True,
                                                          exclude_user_id=current_user.id)]
        if branche:
            results = [p for p in results if p.industry == branche]
    else:
        query = (Profile.query.join(User).filter(User.status == "active", Profile.visible_in_directory.is_(True),
                                                 Profile.user_id != current_user.id))
        if branche:
            query = query.filter(Profile.industry == branche)
        results = query.order_by(Profile.updated_at.desc()).limit(60).all()
    return render_template("member/directory.html", results=results, q=q, branche=branche,
                           me_visible=current_user.profile.visible_in_directory)


ACTIVE_REG = ("registered", "paid", "reserved")


def _upcoming_of(u: User) -> list[Event]:
    """Kommende Termine, zu denen die Person angemeldet ist (nur bei sichtbarem Profil sinnvoll aufgerufen)."""
    return (Event.query.join(Registration).filter(Registration.user_id == u.id, Registration.status.in_(ACTIVE_REG),
                                                  Event.status == "published",
                                                  Event.starts_at >= utcnow() - timedelta(hours=6))
            .order_by(Event.starts_at).limit(6).all())


@bp.route("/foto/<name>")
def avatar(name):
    """Profilfotos nur für angemeldete Mitglieder; Dateiname ist zufällig und wird vom Server vergeben."""
    if not name.endswith(".jpg") or "/" in name or "\\" in name:
        abort(404)
    resp = send_from_directory(media.avatar_dir(), name, mimetype="image/jpeg", max_age=86400)
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@bp.route("/api/kontakt-assistent/<int:user_id>", methods=["POST"])
@limiter.limit("40 per hour")
def contact_assistant(user_id):
    """KI-Einschätzung zu einem Kontakt: Nutzen für beide Seiten, Nachrichtenentwurf, passender Termin."""
    u = db.session.get(User, user_id)
    if not u or u.status != "active" or u.id == current_user.id or not u.profile:
        abort(404)
    ir = IntroRequest.query.filter(
        ((IntroRequest.from_user_id == current_user.id) & (IntroRequest.to_user_id == u.id)) |
        ((IntroRequest.from_user_id == u.id) & (IntroRequest.to_user_id == current_user.id))).first()
    connected = bool(ir and ir.status == "accepted")
    recommended = Match.query.filter_by(user_id=current_user.id, other_id=u.id).first()
    if not (u.profile.visible_in_directory or connected or (recommended and u.profile.allow_matching)):
        abort(404)
    me = current_user.profile
    if not (me.q_can_help or me.q_looking_for or me.q_challenge or me.q_focus):
        return jsonify(error="Fülle zuerst dein Profil aus, dann kann Aiko dir sagen, was dieser Kontakt dir bringt."), 400
    force = bool((request.get_json(silent=True) or {}).get("refresh"))
    return jsonify(insights.pair_insight(current_user, u, force=force))


@bp.route("/mitglieder/<int:user_id>")
def member_detail(user_id):
    u = db.session.get(User, user_id)
    if not u or u.status != "active":
        abort(404)
    if u.id == current_user.id:  # eigene Seite so ansehen, wie andere sie sehen
        return render_template("member/member_detail.html", u=u, p=u.profile, ir=None, connected=False, match=None,
                               is_self=True, shared_events=[], upcoming_events=_upcoming_of(u))
    ir = IntroRequest.query.filter(
        ((IntroRequest.from_user_id == current_user.id) & (IntroRequest.to_user_id == u.id)) |
        ((IntroRequest.from_user_id == u.id) & (IntroRequest.to_user_id == current_user.id))
    ).order_by(IntroRequest.created_at.desc()).first()
    connected = bool(ir and ir.status == "accepted")
    recommended = Match.query.filter_by(user_id=current_user.id, other_id=u.id).first()
    if not (u.profile and (u.profile.visible_in_directory or connected or (recommended and u.profile.allow_matching))):
        abort(404)
    mine = {r.event_id for r in Registration.query.filter(Registration.user_id == current_user.id,
                                                          Registration.status.in_(ACTIVE_REG))}
    # Anmeldungen anderer bleiben privat: sichtbar sind nur Termine, zu denen beide angemeldet sind.
    shared = [e for e in _upcoming_of(u) if e.id in mine]
    return render_template("member/member_detail.html", u=u, p=u.profile, ir=ir, connected=connected,
                           match=recommended, is_self=False, upcoming_events=[], shared_events=shared)


# --------------------------------------------------------------------------- Termine
@bp.route("/termine")
def events():
    mine = {r.event_id: r for r in Registration.query.filter_by(user_id=current_user.id)}
    fmt = request.args.get("format")
    own_pending = Event.query.filter_by(created_by_id=current_user.id, status="pending").all()
    return render_template("member/events.html", events=upcoming(fmt=fmt if fmt in FORMATS else None),
                           mine=mine, active=fmt, own_pending=own_pending)


@bp.route("/termine/<int:event_id>")
def event_detail(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    if ev.status != "published" and ev.created_by_id != current_user.id and not current_user.is_admin:
        abort(404)
    reg = Registration.query.filter_by(event_id=ev.id, user_id=current_user.id).first()
    if request.args.get("paid") and reg and reg.status == "pending_payment":
        flash("Danke! Deine Zahlung wird bestätigt — das dauert meist nur wenige Sekunden.", "success")
    invite = Notification.query.filter_by(user_id=current_user.id, event_id=ev.id, kind="event_invite").first()
    if invite and not invite.read_at:
        invite.read_at = utcnow()
        db.session.commit()
    attendees = []
    if reg and reg.status in ("registered", "paid", "reserved"):
        attendees = [r.user for r in ev.active_registrations
                     if r.user_id != current_user.id and r.user.profile and r.user.profile.visible_in_directory]
    return render_template("member/event_detail.html", ev=ev, reg=reg, attendees=attendees, invite=invite)


@bp.route("/termine/<int:event_id>/anmelden", methods=["POST"])
@limiter.limit("30 per hour")
def event_register(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    if ev.status != "published" or ev.starts_at < utcnow() - timedelta(hours=6):
        flash("Für diesen Termin ist keine Anmeldung möglich.", "error")
        return redirect(url_for("member.event_detail", event_id=ev.id))
    reg = Registration.query.filter_by(event_id=ev.id, user_id=current_user.id).first()
    if reg and reg.status in ("registered", "paid", "reserved"):
        flash("Du bist bereits angemeldet.", "info")
        return redirect(url_for("member.event_detail", event_id=ev.id))
    if ev.seats_left == 0:
        flash("Dieser Termin ist ausgebucht.", "error")
        return redirect(url_for("member.event_detail", event_id=ev.id))
    if reg is None:
        reg = Registration(event_id=ev.id, user_id=current_user.id)
        db.session.add(reg)
    reg.note = request.form.get("note", "").strip()[:500]
    if ev.is_paid and payments.stripe_enabled():
        reg.status = "pending_payment"
        db.session.commit()
        try:
            return redirect(payments.create_checkout(reg), code=303)
        except Exception:
            current_app.logger.exception("Stripe Checkout fehlgeschlagen")
            flash("Die Zahlung konnte nicht gestartet werden. Versuch es gleich noch einmal.", "error")
            return redirect(url_for("member.event_detail", event_id=ev.id))
    reg.status = "reserved" if ev.is_paid else "registered"
    reg.amount_cents = ev.price_cents or 0
    db.session.commit()
    flash("Du bist angemeldet." + (" Die Teilnahmegebühr zahlst du vor Ort." if ev.is_paid else ""), "success")
    return redirect(url_for("member.event_detail", event_id=ev.id))


@bp.route("/termine/<int:event_id>/bezahlen", methods=["POST"])
def event_pay(event_id):
    reg = Registration.query.filter_by(event_id=event_id, user_id=current_user.id).first_or_404()
    if not (payments.stripe_enabled() and reg.event.is_paid and reg.status in ("pending_payment", "reserved")):
        abort(400)
    return redirect(payments.create_checkout(reg), code=303)


@bp.route("/termine/<int:event_id>/abmelden", methods=["POST"])
def event_cancel(event_id):
    reg = Registration.query.filter_by(event_id=event_id, user_id=current_user.id).first_or_404()
    was_paid = reg.status == "paid"
    reg.status = "cancelled"
    db.session.commit()
    flash("Du bist abgemeldet. Danke, dass du Bescheid gibst — so kann jemand anderes den Platz bekommen."
          + (" Wegen der Erstattung melden wir uns bei dir." if was_paid else ""), "info")
    return redirect(url_for("member.event_detail", event_id=event_id))


@bp.route("/termine/<int:event_id>/ics")
def event_ics(event_id):
    ev = db.session.get(Event, event_id) or abort(404)
    fmt = "%Y%m%dT%H%M%SZ"
    end = ev.ends_at or (ev.starts_at + timedelta(hours=2))
    body = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Main Power//Plattform//DE", "BEGIN:VEVENT",
        f"UID:mp-{ev.id}@main-power.org", f"DTSTAMP:{utcnow():{fmt}}", f"DTSTART:{ev.starts_at:{fmt}}",
        f"DTEND:{end:{fmt}}", f"SUMMARY:{ev.title}", f"LOCATION:{ev.location or 'Frankfurt am Main'}",
        "DESCRIPTION:" + (ev.description or "").replace("\n", "\\n")[:900], "END:VEVENT", "END:VCALENDAR", ""])
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=main-power-{ev.id}.ics"})


@bp.route("/termine/neu", methods=["GET", "POST"])
@limiter.limit("10 per day", methods=["POST"])
def event_create():
    errors = {}
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()
        try:
            start_local = datetime.strptime(f"{f.get('date')} {f.get('time') or '18:00'}", "%Y-%m-%d %H:%M")
        except ValueError:
            errors["date"] = "Bitte Datum und Uhrzeit angeben."
            start_local = None
        if not title:
            errors["title"] = "Bitte gib dem Treffen einen Titel."
        if start_local and local_to_utc(start_local) < utcnow():
            errors["date"] = "Das Datum liegt in der Vergangenheit."
        if not errors:
            needs_approval = Setting.get("member_events_require_approval") == "1" and not current_user.is_admin
            ev = Event(title=title[:200], description=f.get("description", "").strip()[:3000],
                       location=f.get("location", "").strip()[:255], format="community", source="member",
                       starts_at=local_to_utc(start_local),
                       capacity=int(f.get("capacity")) if (f.get("capacity") or "").isdigit() else None,
                       status="pending" if needs_approval else "published", created_by_id=current_user.id)
            db.session.add(ev)
            db.session.flush()
            db.session.add(Registration(event_id=ev.id, user_id=current_user.id, status="registered"))
            audit("event.member_create", f"event:{ev.id}", title)
            db.session.commit()
            from .admin import auto_invite
            invited = 0 if needs_approval else auto_invite(ev, False)
            flash("Dein Treffen ist eingereicht und erscheint nach kurzer Prüfung im Kalender." if needs_approval
                  else "Dein Treffen ist veröffentlicht." + (f" Aiko hat {invited} passende Mitglieder eingeladen."
                                                            if invited else ""), "success")
            return redirect(url_for("member.event_detail", event_id=ev.id))
    return render_template("member/event_form.html", errors=errors, form=request.form)


# --------------------------------------------------------------------------- Aiko
@bp.route("/aiko")
def aiko_page():
    history = (ChatMessage.query.filter_by(user_id=current_user.id)
               .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(30).all())
    return render_template("member/aiko.html", history=list(reversed(history)))


@bp.route("/api/aiko", methods=["POST"])
@limiter.limit("20 per minute; 200 per day")
def aiko_api():
    msg = str((request.get_json(silent=True) or {}).get("message", "")).strip()
    if not msg:
        return jsonify(error="Leere Nachricht"), 400
    return jsonify(reply=aiko.answer_member(current_user, msg))


@bp.route("/aiko/verlauf-loeschen", methods=["POST"])
def aiko_clear():
    ChatMessage.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash("Chatverlauf gelöscht.", "info")
    return redirect(url_for("member.aiko_page"))


# --------------------------------------------------------------------------- Leistungen
@bp.route("/leistungen")
def services():
    items = Service.query.filter_by(active=True).order_by(Service.sort).all()
    return render_template("member/services.html", items=items)


@bp.route("/leistungen/<slug>", methods=["GET", "POST"])
@limiter.limit("10 per day", methods=["POST"])
def service_detail(slug):
    s = Service.query.filter_by(slug=slug, active=True).first_or_404()
    if request.method == "POST":
        inq = ServiceInquiry(service_id=s.id, user_id=current_user.id,
                             message=request.form.get("message", "").strip()[:2000])
        db.session.add(inq)
        db.session.commit()
        if s.provider_email:
            send_mail(s.provider_email, f"Anfrage über Main Power: {s.title}", "service_inquiry",
                      s=s, user=current_user, inq=inq)
        flash("Danke! Deine Anfrage ist angekommen — wir melden uns innerhalb von zwei Werktagen.", "success")
        return redirect(url_for("member.service_detail", slug=slug))
    return render_template("member/service_detail.html", s=s)


# --------------------------------------------------------------------------- Community-Chat (Telegram)
@bp.route("/community")
def community():
    """Community-Übersicht: neue Mitglieder, Termine mit Teilnehmenden, Themen, Kontakte — plus Telegram-Chat."""
    visible = (Profile.query.join(User).filter(User.status == "active", Profile.visible_in_directory.is_(True),
                                               Profile.user_id != current_user.id))
    newest = visible.order_by(User.created_at.desc()).limit(8).all()
    total = visible.count()
    industries = (db.session.query(Profile.industry, db.func.count(Profile.id))
                  .join(User).filter(User.status == "active", Profile.visible_in_directory.is_(True),
                                     Profile.industry != "")
                  .group_by(Profile.industry).order_by(db.func.count(Profile.id).desc()).limit(10).all())
    events = []
    for ev in upcoming(6):
        people = [r.user for r in ev.active_registrations
                  if r.user_id != current_user.id and r.user.profile and r.user.profile.visible_in_directory][:8]
        events.append((ev, people, len(ev.active_registrations)))
    accepted = IntroRequest.query.filter(
        IntroRequest.status == "accepted",
        (IntroRequest.from_user_id == current_user.id) | (IntroRequest.to_user_id == current_user.id)).all()
    contacts = [ir.to_user if ir.from_user_id == current_user.id else ir.from_user for ir in accepted]
    everyone = visible.order_by(Profile.updated_at.desc()).limit(40).all()
    my_matches = {m.other_id: m.score for m in matching.top_matches(current_user, k=8, explain=False)} \
        if current_user.profile.is_matchable and current_user.profile.embed_need else {}
    nodes = [{"l": "Du", "h": 1}]
    edges = []
    for p in everyone:
        idx = len(nodes)
        nodes.append({"l": f"{p.user.first_name} {p.user.last_name[:1]}.", "u": url_for("member.member_detail", user_id=p.user.id),
                      "h": 1 if p.user_id in my_matches else 0})
        edges.append([0, idx, round(max(my_matches.get(p.user_id, 0.15), 0.15), 2)])
    return render_template("member/community.html", tg=telegram.enabled(), group=telegram.group_enabled(),
                           group_title=Setting.get("telegram_group_title"), newest=newest, total=total,
                           industries=industries, events=events, contacts=contacts[:8],
                           graph={"nodes": nodes, "edges": edges})


@bp.route("/community/verbinden", methods=["POST"])
def community_link():
    if not telegram.enabled():
        abort(404)
    return redirect(telegram.deep_link(current_user))


@bp.route("/community/beitreten", methods=["POST"])
@limiter.limit("5 per hour")
def community_join():
    if not telegram.group_enabled() or not current_user.telegram_user_id:
        abort(400)
    try:
        link = telegram.create_invite(current_user)
    except Exception:
        flash("Der Einladungslink konnte nicht erstellt werden. Bitte versuch es später erneut.", "error")
        return redirect(url_for("member.community"))
    audit("telegram.invite", f"user:{current_user.id}")
    db.session.commit()
    return redirect(link)


@bp.route("/community/trennen", methods=["POST"])
def community_unlink():
    if current_user.telegram_user_id:
        telegram.kick(current_user.telegram_user_id)
    current_user.telegram_user_id = None
    current_user.telegram_username = None
    db.session.commit()
    flash("Telegram getrennt.", "info")
    return redirect(url_for("member.community"))


# --------------------------------------------------------------------------- Privatsphäre & Konto
@bp.route("/privatsphaere", methods=["GET", "POST"])
def privacy():
    p = current_user.profile
    if request.method == "POST":
        f = request.form
        changes = {"matching": bool(f.get("matching")), "directory": bool(f.get("directory")),
                   "newsletter": bool(f.get("newsletter"))}
        for kind, val in changes.items():
            if current_user.has_consent(kind) != val:
                record_consent(current_user, kind, val)
        p.allow_matching = changes["matching"]
        p.visible_in_directory = changes["directory"]
        p.event_invites = bool(f.get("event_invites"))
        if not p.allow_matching:
            Match.query.filter((Match.user_id == current_user.id) |
                               (Match.other_id == current_user.id)).delete(synchronize_session=False)
        elif not p.embed_need:
            matching.refresh_embeddings(p, commit=False)
        db.session.commit()
        flash("Einstellungen gespeichert.", "success")
        return redirect(url_for("member.privacy"))
    return render_template("member/privacy.html", p=p, newsletter=current_user.has_consent("newsletter"),
                           telegram_linked=bool(current_user.telegram_user_id))


@bp.route("/privatsphaere/export")
@limiter.limit("5 per hour")
def export():
    data = json.dumps(export_user(current_user), ensure_ascii=False, indent=2)
    return Response(data, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=main-power-meine-daten.json"})


@bp.route("/privatsphaere/passwort", methods=["POST"])
def change_password():
    if not current_user.check_password(request.form.get("current", "")):
        flash("Das aktuelle Passwort stimmt nicht.", "error")
    elif len(request.form.get("new", "")) < 10:
        flash("Das neue Passwort braucht mindestens 10 Zeichen.", "error")
    else:
        current_user.set_password(request.form["new"])
        db.session.commit()
        flash("Passwort geändert.", "success")
    return redirect(url_for("member.privacy"))


@bp.route("/privatsphaere/loeschen", methods=["POST"])
@limiter.limit("5 per hour")
def delete_account():
    if not current_user.check_password(request.form.get("password", "")):
        flash("Das Passwort stimmt nicht. Konto wurde nicht gelöscht.", "error")
        return redirect(url_for("member.privacy"))
    if current_user.is_superadmin and User.query.filter_by(role="superadmin").count() == 1:
        flash("Das letzte Superadmin-Konto kann nicht gelöscht werden.", "error")
        return redirect(url_for("member.privacy"))
    user = db.session.get(User, current_user.id)
    if user.telegram_user_id:
        telegram.kick(user.telegram_user_id)
    audit("user.self_delete", f"user:{user.id}", actor_id=None)
    logout_user()
    delete_user(user)
    flash("Dein Konto und alle zugehörigen Daten wurden gelöscht.", "info")
    return redirect(url_for("public.index"))
