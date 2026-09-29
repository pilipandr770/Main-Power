from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from flask import (Blueprint, Response, session, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from flask_login import current_user, login_required, login_user, logout_user

from .. import questionnaire
from ..extensions import db, limiter
from ..models import (FORMATS, SOCIALS, AddOn, ChatMessage, Event, GoalCheckin, IntroRequest, LawQuery, Match,
                      Notification, PairInsight, PanelRun, Plan, Profile, QuotaBonus, Registration, Service,
                      ServiceInquiry, SeoReport, Setting, User, utcnow)
from ..services import (aiko, billing, compliance_check, goals, insights, interview, laws, matching, media, panel,
                        payments, plans, security_check, seo_check, telegram)
from ..services import club as club_settings
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
    return redirect(url_for("member.dashboard"))


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
                           events=upcoming(3), announcement=Setting.get("announcement"), invites=invites,
                           goal_days=goals.days_left(p))


# --------------------------------------------------------------------------- Profil
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
    "goal_12m": {
        "why": "Ein klares Ziel macht aus Kontakten Fortschritt: Aiko sucht Menschen und Termine, die genau darauf "
               "einzahlen.",
        "tips": "Ein Ziel, an dem man Erfolg erkennt. Zahl, Zielgruppe oder Ergebnis nennen.",
        "examples": ["In 12 Monaten 20 Stammkund:innen im Mittelstand und ein zweites Produkt am Markt.",
                     "Eine Seed-Finanzierung über 500.000 € abschließen und zwei Entwickler einstellen."]},
    "milestone_90d": {
        "why": "Große Ziele werden in 90-Tage-Schritten greifbar. Aiko fragt nach dem Fortschritt und schlägt "
               "passende Kontakte vor.",
        "tips": "Ein messbarer Schritt, den du selbst beeinflussen kannst.",
        "examples": ["Drei Pilotkund:innen haben den Vertrag unterschrieben.",
                     "Pitch-Deck steht und ich habe mit fünf Investor:innen gesprochen."]},
    "q_tried": {
        "why": "So bekommst du keine Tipps, die du schon kennst — und Menschen, die über den nächsten Schritt "
               "sprechen können.",
        "tips": "Stichworte reichen: was du probiert hast und woran es gehakt hat.",
        "examples": ["Kaltakquise per LinkedIn und zwei Messen — viele Gespräche, kaum Abschlüsse.",
                     "Jobportale und eine Agentur, die Kandidat:innen passten nicht zur Kultur."]},
    "not_wanted": {
        "why": "Schützt dich vor unpassenden Anfragen. Wer genau das anbietet, wird dir nicht empfohlen — und du "
               "ihnen nicht.",
        "tips": "Themen oder Angebote nennen, nicht Personen. Diese Angabe sieht niemand außer dir.",
        "examples": ["Versicherungen, Finanzprodukte und Network-Marketing.",
                     "Verkaufsgespräche zu Software, die ich nicht angefragt habe."]},
    "asked_for": {
        "why": "Was andere bei dir suchen, ist oft präziser als die eigene Beschreibung — und findet die richtigen "
               "Menschen.",
        "tips": "Denk an die letzten Anrufe oder Nachrichten, in denen dich jemand um Rat gefragt hat.",
        "examples": ["Wie man einen Mietvertrag für Gewerbeflächen verhandelt.",
                     "Welche Förderprogramme es für Digitalisierung im Mittelstand gibt."]},
}


def _profile_guide() -> dict:
    """Anleitungstexte mit dem Namen der KI-Assistenz dieses Klubs."""
    name = club_settings.settings()["assistant_name"]
    return {k: {**v, "why": v["why"].replace("Aiko", name)} for k, v in PROFILE_GUIDE.items()}


@bp.route("/profil", methods=["GET", "POST"])
def profile():
    p = current_user.profile
    if request.method == "POST":
        f = request.form
        current_user.first_name = f.get("first_name", current_user.first_name).strip()[:80] or current_user.first_name
        current_user.last_name = f.get("last_name", "").strip()[:80]
        current_user.phone = f.get("phone", "").strip()[:40]
        values = {field: f.get(field, "") for field in questionnaire.ALL_TEXT_FIELDS}
        values.update({field: f.getlist(field) for field in questionnaire.CHOICE_FIELDS})
        values.update({f"custom:{q.key}": (f.getlist(f"custom_{q.key}") if q.kind == "multi" else
                                           f.get(f"custom_{q.key}", "")) for q in questionnaire.club_questions()})
        questionnaire.apply_values(p, values)
        if not p.milestone_90d:
            p.milestone_set_at = None
        p.public_fields = ",".join(k for k in f.getlist("public_fields") if k in questionnaire.SHAREABLE_PRIVATE)
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
                           guide=_profile_guide())


@bp.route("/api/profil-coach", methods=["POST"])
@limiter.limit("20 per hour")
def profile_coach():
    """KI-Feedback zu den (noch ungespeicherten) Profilantworten."""
    data = request.get_json(silent=True) or {}
    blocked = _quota_json("ki_aktionen")
    if blocked:
        return blocked
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
            telegram.send(target.telegram_user_id, f"{current_user.first_name} möchte dich über {club_settings.settings()['name']} "
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
    rolle = request.args.get("rolle", "") if request.args.get("rolle") in questionnaire.ROLES else ""
    bringt = request.args.get("bringt", "") if request.args.get("bringt") in questionnaire.RESOURCES else ""
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
        results = query.order_by(Profile.updated_at.desc()).limit(200).all()
    if rolle:
        results = [p for p in results if p.role == rolle]
    if bringt:
        results = [p for p in results if bringt in questionnaire.split(p.resources)]
    return render_template("member/directory.html", results=results[:60], q=q, branche=branche, rolle=rolle,
                           bringt=bringt, me_visible=current_user.profile.visible_in_directory)


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
    if not (me.q_can_help or me.q_looking_for or me.q_challenge or me.q_focus or me.goal_12m or me.partner_types):
        return jsonify(error=f"Fülle zuerst dein Profil aus, dann kann {club_settings.settings()['assistant_name']} "
                             "dir sagen, was dieser Kontakt dir bringt."), 400
    force = bool((request.get_json(silent=True) or {}).get("refresh"))
    cached = PairInsight.query.filter_by(user_id=current_user.id, other_id=u.id).first()
    if force or cached is None:
        blocked = _quota_json("ki_aktionen")
        if blocked:
            return blocked
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


# --------------------------------------------------------------------------- Ziel & 90-Tage-Check-in
@bp.route("/ziel", methods=["GET", "POST"])
@limiter.limit("10 per day", methods=["POST"])
def goal_page():
    p = current_user.profile
    if request.method == "POST":
        if not p.milestone_90d and not request.form.get("next_milestone", "").strip():
            flash("Trag zuerst einen Meilenstein ein – dann kannst du den Fortschritt melden.", "error")
            return redirect(url_for("member.goal_page"))
        try:
            plans.consume(current_user, "goal_checkin")
        except plans.QuotaExceeded as exc:
            flash(str(exc), "error")
            return redirect(url_for("member.plan_page"))
        c = goals.record_checkin(current_user, request.form.get("status", "auf_kurs"), request.form.get("note", ""),
                                 request.form.get("next_milestone", ""), request.form.get("goal"))
        flash(f"Check-in gespeichert: {c.status_label}. Der nächste 90-Tage-Zeitraum läuft ab heute.", "success")
        return redirect(url_for("member.goal_page") + "#verlauf")
    return render_template("member/goal.html", p=p, items=goals.history(current_user),
                           days_left=goals.days_left(p), due=goals.is_due(p), statuses=GoalCheckin.STATUS)


# --------------------------------------------------------------------------- Profil-Interview mit Aiko
@bp.route("/profil/interview")
def interview_page():
    return render_template("member/interview.html", p=current_user.profile)


def _interview_skip() -> list[str]:
    data = request.get_json(silent=True) or {}
    return [str(s) for s in data.get("skip", []) if isinstance(s, str)][:50]


@bp.route("/api/interview/weiter", methods=["POST"])
def interview_next():
    return jsonify(question=interview.next_question(current_user.profile, _interview_skip()),
                   completeness=current_user.profile.completeness)


@bp.route("/api/interview/antwort", methods=["POST"])
@limiter.limit("60 per hour")
def interview_answer():
    data = request.get_json(silent=True) or {}
    field = str(data.get("field", ""))[:60]
    blocked = _quota_json("ki_aktionen")
    if blocked:
        return blocked
    proposals, ai = interview.extract(current_user.profile, field, str(data.get("answer", "")))
    return jsonify(proposals=proposals, ai=ai)


@bp.route("/api/interview/uebernehmen", methods=["POST"])
def interview_apply():
    data = request.get_json(silent=True) or {}
    values = data.get("values") if isinstance(data.get("values"), dict) else {}
    p = current_user.profile
    changed = questionnaire.apply_values(p, values)
    if changed:
        p.updated_at = utcnow()
        matching.refresh_embeddings(p, commit=False)
        db.session.commit()
    return jsonify(saved=changed, question=interview.next_question(p, _interview_skip()), completeness=p.completeness)


# --------------------------------------------------------------------------- Tarif & Kontingente
def _quota_json(feature: str):
    """Kontingent verbrauchen; bei Überschreitung JSON-Antwort mit Hinweis auf den Tarif (sonst None)."""
    try:
        plans.consume(current_user, feature)
    except plans.QuotaExceeded as exc:
        return jsonify(error=str(exc), upgrade_url=url_for("member.plan_page")), 402
    return None


def _quota_form(feature: str, field: str = "url") -> dict:
    """Wie _quota_json, aber als Formularfehler."""
    try:
        plans.consume(current_user, feature)
    except plans.QuotaExceeded as exc:
        return {field: str(exc) + " Siehe „Mein Tarif“."}
    return {}


@bp.route("/tarif")
def plan_page():
    if request.args.get("portal") and current_user.stripe_subscription_id:
        try:
            billing.sync_subscription(current_user.stripe_subscription_id)
        except Exception:
            current_app.logger.exception("Abo-Abgleich fehlgeschlagen")
            db.session.rollback()
    if request.args.get("session_id"):
        try:
            billing.sync_checkout(request.args["session_id"], user=current_user)
        except Exception:  # Webhook übernimmt, falls Stripe gerade nicht antwortet
            current_app.logger.exception("Checkout-Abgleich fehlgeschlagen")
            db.session.rollback()
    plan = plans.user_plan(current_user)
    return render_template("member/plan.html", plan=plan, usage=plans.usage_overview(current_user),
                           plans=Plan.query.filter_by(kind="member", active=True).order_by(Plan.sort).all(),
                           addons=AddOn.query.filter_by(active=True).order_by(AddOn.sort).all(),
                           bonuses=[b for b in QuotaBonus.query.filter_by(user_id=current_user.id) if b.left],
                           features=plans.FEATURES, stripe_on=billing.enabled(), vat=plans.vat_rate())


@bp.route("/tarif/<key>", methods=["POST"])
@limiter.limit("10 per hour")
def plan_choose(key):
    plan = Plan.query.filter_by(key=key, kind="member", active=True).first_or_404()
    if plan.is_free:
        flash("Den Basis-Tarif erreichst du, indem du dein Abo unter „Abo verwalten“ kündigst.", "info")
        return redirect(url_for("member.plan_page"))
    new_sub = not (current_user.stripe_subscription_id and current_user.plan_status in plans.PAID_STATUS)
    if new_sub and not request.form.get("widerruf"):
        flash("Bitte bestätige den Hinweis zum Widerrufsrecht, damit der Tarif sofort starten kann.", "error")
        return redirect(url_for("member.plan_page"))
    try:
        target = billing.checkout_member_plan(current_user, plan)
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("member.plan_page"))
    audit("plan.checkout", f"user:{current_user.id}", plan.key)
    db.session.commit()
    if not new_sub:
        flash(f"Tarif gewechselt: {plan.name}. Die Differenz wird anteilig verrechnet.", "success")
    return redirect(target)


@bp.route("/tarif/paket/<key>", methods=["POST"])
@limiter.limit("10 per hour")
def addon_buy(key):
    addon = AddOn.query.filter_by(key=key, active=True).first_or_404()
    if not request.form.get("widerruf"):
        flash("Bitte bestätige den Hinweis zum Widerrufsrecht, damit das Paket sofort nutzbar ist.", "error")
        return redirect(url_for("member.plan_page"))
    try:
        return redirect(billing.checkout_addon(current_user, addon))
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("member.plan_page"))


@bp.route("/tarif/verwalten", methods=["POST"])
def plan_portal():
    try:
        return redirect(billing.portal_url(current_user.stripe_customer_id,
                                           url_for("member.plan_page", portal=1, _external=True)))
    except billing.BillingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("member.plan_page"))


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
        "BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//{club_settings.settings()['name']}//Plattform//DE", "BEGIN:VEVENT",
        f"UID:ev-{ev.id}@{request.host.split(':')[0]}", f"DTSTAMP:{utcnow():{fmt}}", f"DTSTART:{ev.starts_at:{fmt}}",
        f"DTEND:{end:{fmt}}", f"SUMMARY:{ev.title}", f"LOCATION:{ev.location or club_settings.settings().get('city', '')}",
        "DESCRIPTION:" + (ev.description or "").replace("\n", "\\n")[:900], "END:VEVENT", "END:VCALENDAR", ""])
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename={club_settings.slug(club_settings.settings()['name'])}-{ev.id}.ics"})


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
                  else "Dein Treffen ist veröffentlicht." + (f" {club_settings.settings()['assistant_name']} hat {invited} passende Mitglieder eingeladen."
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
    blocked = _quota_json("ki_aktionen")
    if blocked:
        return blocked
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


# Leistungen mit eigenem Werkzeug statt Anfrageformular
TOOL_SERVICES = {"seo-check": "member.seo_check_page", "compliance-check": "member.compliance_check_page",
                 "sicherheits-check": "member.security_check_page", "markt-panel": "member.panel_page",
                 "gesetzes-suche": "member.laws_page"}


@bp.route("/leistungen/<slug>", methods=["GET", "POST"])
@limiter.limit("10 per day", methods=["POST"])
def service_detail(slug):
    s = Service.query.filter_by(slug=slug, active=True).first_or_404()
    if slug in TOOL_SERVICES and request.method == "GET":
        return redirect(url_for(TOOL_SERVICES[slug]))
    if request.method == "POST":
        inq = ServiceInquiry(service_id=s.id, user_id=current_user.id,
                             message=request.form.get("message", "").strip()[:2000])
        db.session.add(inq)
        db.session.commit()
        if s.provider_email:
            send_mail(s.provider_email, f"Anfrage über {club_settings.settings()['name']}: {s.title}", "service_inquiry",
                      s=s, user=current_user, inq=inq)
        flash("Danke! Deine Anfrage ist angekommen — wir melden uns innerhalb von zwei Werktagen.", "success")
        return redirect(url_for("member.service_detail", slug=slug))
    return render_template("member/service_detail.html", s=s)


# --------------------------------------------------------------------------- SEO-Check
@bp.route("/seo-check", methods=["GET", "POST"])
@limiter.limit("6 per hour", methods=["POST"])
def seo_check_page():
    errors: dict[str, str] = {}
    form = request.form
    if request.method == "POST":
        if not form.get("authorized"):
            errors["authorized"] = "Bitte bestätige, dass du diese Website prüfen darfst."
        keywords = [k.strip()[:60] for k in re.split(r"[,\n;]", form.get("keywords", "")) if k.strip()][:5]
        if not errors:
            errors.update(_quota_form("site_check"))
        if not errors:
            try:
                result = seo_check.analyze(form.get("url", ""), keywords)
            except seo_check.SeoCheckError as exc:
                errors["url"] = str(exc)
                plans.refund(current_user, "site_check")
            else:
                report = seo_check.build_report(result, keywords)
                row = SeoReport(user_id=current_user.id, url=result["url"][:500], keywords=", ".join(keywords),
                                score=result["score"], data={"result": result, "report": report}, ai=report["ai"])
                db.session.add(row)
                audit("seo.check", f"user:{current_user.id}", row.url[:200])
                db.session.commit()
                return redirect(url_for("member.seo_report", rid=row.id))
    reports = (SeoReport.query.filter_by(user_id=current_user.id, kind="seo").order_by(SeoReport.created_at.desc())
               .limit(10).all())
    return render_template("member/seo_check.html", errors=errors, form=form, reports=reports)


@bp.route("/compliance-check", methods=["GET", "POST"])
@limiter.limit("6 per hour", methods=["POST"])
def compliance_check_page():
    errors: dict[str, str] = {}
    form = request.form
    if request.method == "POST":
        if not form.get("authorized"):
            errors["authorized"] = "Bitte bestätige, dass du diese Website prüfen darfst."
        if not errors:
            errors.update(_quota_form("site_check"))
        if not errors:
            try:
                result = compliance_check.analyze(form.get("url", ""))
            except seo_check.SeoCheckError as exc:
                errors["url"] = str(exc)
                plans.refund(current_user, "site_check")
            else:
                report = compliance_check.build_report(result)
                row = SeoReport(user_id=current_user.id, kind="compliance", url=result["url"][:500], score=result["score"],
                                data={"result": result, "report": report}, ai=report["ai"])
                db.session.add(row)
                audit("compliance.check", f"user:{current_user.id}", row.url[:200])
                db.session.commit()
                return redirect(url_for("member.seo_report", rid=row.id))
    reports = (SeoReport.query.filter_by(user_id=current_user.id, kind="compliance")
               .order_by(SeoReport.created_at.desc()).limit(10).all())
    return render_template("member/compliance_check.html", errors=errors, form=form, reports=reports)


# --------------------------------------------------------------------------- Markt-Panel (synthetische Personas)
@bp.route("/markt-panel", methods=["GET", "POST"])
@limiter.limit("8 per hour", methods=["POST"])
def panel_page():
    max_n = max(plans.PERSONA_SIZES)  # größte Laufgröße überhaupt
    unlimited = current_user.is_admin
    q = plans.quota(current_user, "panel_runs")
    limit, used = (q.limit or 0) + q.bonus_left, q.used
    allowed = max_n if unlimited else min(max_n, plans.panel_persona_limit(current_user))
    sizes = [n for n in plans.PERSONA_SIZES if n <= allowed] or [allowed or 25]
    errors: dict[str, str] = {}
    form = request.form
    if request.method == "POST":
        name = form.get("product_name", "").strip()[:160]
        desc = form.get("description", "").strip()[:1500]
        audience = form.get("audience") if form.get("audience") in ("privat", "business", "beide") else "beide"
        regional = form.get("regional") if form.get("regional") in ("de", "rhein-main") else "de"
        unit = form.get("unit") if form.get("unit") in panel.UNITS else "einmalig"
        pa, pb = panel._num(form.get("price_a", "")) if form.get("price_a", "").strip() else None, \
            panel._num(form.get("price_b", "")) if form.get("price_b", "").strip() else None
        n = int(form.get("n")) if (form.get("n") or "").isdigit() and int(form.get("n")) in sizes else sizes[-1]
        if len(name) < 3:
            errors["product_name"] = "Bitte gib dem Produkt einen Namen."
        if len(desc) < 40:
            errors["description"] = "Bitte beschreibe das Produkt in mindestens 40 Zeichen: Was ist es, für wen, was kann es?"
        if (pa is not None and not 0 < pa < 1_000_000) or (form.get("price_a", "").strip() and pa is None):
            errors["price_a"] = "Bitte einen gültigen Preis in Euro angeben (z. B. 29,90) oder das Feld leer lassen."
        if pb is not None and pa is None:
            errors["price_b"] = "Für den Preisvergleich braucht es zuerst Preis A."
        elif (pb is not None and not 0 < pb < 1_000_000) or (form.get("price_b", "").strip() and pb is None):
            errors["price_b"] = "Bitte einen gültigen Preis in Euro angeben."
        if not form.get("synthetic"):
            errors["synthetic"] = "Bitte bestätige, dass dir bewusst ist: Die Ergebnisse sind synthetisch (KI-generiert)."
        if not errors and not unlimited:
            if n > ((plans.user_plan(current_user).limits or {}).get("panel_personas") or 0):
                try:
                    plans.consume_pack(current_user, "panel_runs", n)
                except plans.QuotaExceeded:
                    errors["quota"] = (f"Für {n} Personas brauchst du ein passendes Markt-Panel-Paket oder einen "
                                       "höheren Tarif. Siehe „Mein Tarif“.")
            else:
                errors.update(_quota_form("panel_runs", field="quota"))
        if not errors:
            run = panel.start_run(current_user, {"product_name": name, "description": desc, "audience": audience,
                                                 "regional": regional, "unit": unit, "price_a": pa, "price_b": pb, "n": n})
            audit("panel.start", f"run:{run.id}", f"{name[:80]} n={n}")
            db.session.commit()
            return redirect(url_for("member.panel_run", rid=run.id))
    runs = PanelRun.query.filter_by(user_id=current_user.id).order_by(PanelRun.created_at.desc()).limit(10).all()
    for r in runs:
        panel.refresh_status(r)
    return render_template("member/markt_panel.html", errors=errors, form=form, runs=runs, sizes=sizes, limit=limit,
                           used=used, unlimited=unlimited, units=panel.UNITS)


def _own_run(rid: int) -> PanelRun:
    run = PanelRun.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    return panel.refresh_status(run)


@bp.route("/markt-panel/<int:rid>")
def panel_run(rid):
    run = _own_run(rid)
    rows = [{"r": x, "p": x.persona, "a": x.answers} for x in run.responses] if run.status == "done" else []
    return render_template("member/markt_panel_run.html", run=run, res=run.result or {}, rep=run.report or {}, rows=rows,
                           units=panel.UNITS, intent_label=panel.INTENT_LABEL,
                           cost=panel.cost_usd(run) if current_user.is_admin else None)


@bp.route("/api/markt-panel/<int:rid>/status")
def panel_status(rid):
    run = _own_run(rid)
    return jsonify(status=run.status, done=run.n_done, requested=run.n_requested, failed=run.n_failed, error=run.error)


@bp.route("/markt-panel/<int:rid>/loeschen", methods=["POST"])
def panel_delete(rid):
    run = _own_run(rid)
    if run.status == "running":
        flash("Der Lauf ist noch aktiv.", "info")
        return redirect(url_for("member.panel_run", rid=rid))
    db.session.delete(run)
    db.session.commit()
    flash("Panel-Auswertung gelöscht.", "info")
    return redirect(url_for("member.panel_page"))


@bp.route("/sicherheits-check", methods=["GET", "POST"])
@limiter.limit("6 per hour", methods=["POST"])
def security_check_page():
    errors: dict[str, str] = {}
    form = request.form
    if request.method == "POST":
        if not form.get("authorized"):
            errors["authorized"] = "Bitte bestätige, dass du diese Domain prüfen darfst."
        if not errors:
            errors.update(_quota_form("site_check"))
        if not errors:
            try:
                result = security_check.analyze(form.get("url", ""))
            except seo_check.SeoCheckError as exc:
                errors["url"] = str(exc)
                plans.refund(current_user, "site_check")
            else:
                report = security_check.build_report(result)
                row = SeoReport(user_id=current_user.id, kind="security", url=result["url"][:500], score=result["score"],
                                data={"result": result, "report": report}, ai=report["ai"])
                db.session.add(row)
                audit("security.check", f"user:{current_user.id}", row.url[:200])
                db.session.commit()
                return redirect(url_for("member.seo_report", rid=row.id))
    reports = (SeoReport.query.filter_by(user_id=current_user.id, kind="security")
               .order_by(SeoReport.created_at.desc()).limit(10).all())
    return render_template("member/security_check.html", errors=errors, form=form, reports=reports)


@bp.route("/seo-check/<int:rid>")
def seo_report(rid):
    row = SeoReport.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    return render_template("member/seo_report.html", r=row, result=row.data["result"], report=row.data["report"])


@bp.route("/seo-check/<int:rid>/loeschen", methods=["POST"])
def seo_report_delete(rid):
    row = SeoReport.query.filter_by(id=rid, user_id=current_user.id).first_or_404()
    kind = row.kind
    db.session.delete(row)
    db.session.commit()
    flash("Bericht gelöscht.", "info")
    return redirect(url_for({"compliance": "member.compliance_check_page", "security": "member.security_check_page"}
                            .get(kind, "member.seo_check_page")))


# --------------------------------------------------------------------------- Gesetzes-Suche
@bp.route("/gesetze", methods=["GET", "POST"])
@limiter.limit("15 per hour", methods=["POST"])
def laws_page():
    errors: dict[str, str] = {}
    form = request.form
    if request.method == "POST":
        query = form.get("query", "").strip()
        category = form.get("category", "").strip()
        if category not in laws.CATEGORIES:
            category = ""
        if len(query) < 8:
            errors["query"] = "Bitte formuliere deine Frage etwas ausführlicher (mindestens 8 Zeichen)."
        if not form.get("kein_ersatz"):
            errors["kein_ersatz"] = "Bitte bestätige, dass dir bewusst ist: Das ist keine Rechtsberatung."
        if not errors:
            errors.update(_quota_form("law_search", field="query"))
        if not errors:
            try:
                hits = laws.search(query, category)
            except laws.LawsUnavailable as exc:
                errors["query"] = str(exc)
                plans.refund(current_user, "law_search")
            else:
                answer = laws.explain(query, hits)
                row = LawQuery(user_id=current_user.id, question=query[:1000], category=category, hits=hits,
                              answer=answer, ai=answer["ai"])
                db.session.add(row)
                audit("laws.search", f"user:{current_user.id}", query[:200])
                db.session.commit()
                return redirect(url_for("member.law_query", qid=row.id))
    history = LawQuery.query.filter_by(user_id=current_user.id).order_by(LawQuery.created_at.desc()).limit(10).all()
    return render_template("member/gesetze.html", errors=errors, form=form, history=history, categories=laws.CATEGORIES,
                           available=laws.enabled())


@bp.route("/gesetze/<int:qid>")
def law_query(qid):
    row = LawQuery.query.filter_by(id=qid, user_id=current_user.id).first_or_404()
    erl_by_id = {e["id"]: e["text"] for e in (row.answer or {}).get("erlaeuterungen", [])}
    nicht_passend = set((row.answer or {}).get("nicht_passend", []))
    return render_template("member/gesetz_treffer.html", row=row, erl_by_id=erl_by_id, nicht_passend=nicht_passend)


@bp.route("/gesetze/<int:qid>/loeschen", methods=["POST"])
def law_query_delete(qid):
    row = LawQuery.query.filter_by(id=qid, user_id=current_user.id).first_or_404()
    db.session.delete(row)
    db.session.commit()
    flash("Anfrage gelöscht.", "info")
    return redirect(url_for("member.laws_page"))


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
        p.goal_reminders = bool(f.get("goal_reminders"))
        if not p.allow_matching:
            Match.query.filter((Match.user_id == current_user.id) |
                               (Match.other_id == current_user.id)).delete(synchronize_session="fetch")
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
                    headers={"Content-Disposition": f"attachment; filename={club_settings.slug(club_settings.settings()['name'])}-meine-daten.json"})


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
