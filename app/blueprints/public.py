from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, send_from_directory

from ..extensions import csrf, limiter
from ..models import FORMATS, Event, KnowledgeItem, utcnow
from ..services import aiko, media
from ..services import club as club_settings

bp = Blueprint("public", __name__)


def upcoming(limit: int | None = None, fmt: str | None = None):
    q = Event.query.filter(Event.status == "published", Event.starts_at >= utcnow() - timedelta(hours=6))
    if fmt:
        q = q.filter(Event.format == fmt)
    q = q.order_by(Event.starts_at)
    return q.limit(limit).all() if limit else q.all()


SIZES = {"50": "bis 50 Mitglieder", "250": "bis 250 Mitglieder", "1000": "bis 1.000 Mitglieder", "mehr": "mehr als 1.000"}


def _platform_home(errors=None, form=None, status=200):
    from ..models import Plan
    plans = Plan.query.filter_by(kind="club", active=True).order_by(Plan.sort).all()
    from flask import current_app
    from ..tenancy import is_platform_host
    demo = current_app.config.get("PLATFORM_DEMO_URL") or ("" if is_platform_host(request.host) else "/")
    return render_template("public/platform_home.html", plans=plans, sizes=SIZES, form=form or {},
                           errors=errors or {}, demo_url=demo), status


@bp.route("/fuer-klubs")
def platform_home():
    return _platform_home()


@bp.route("/fuer-klubs/anfrage", methods=["POST"])
@limiter.limit("5 per hour; 20 per day")
def club_request():
    """Anfrage eines neuen Klubs: wird gespeichert, die Betreiber-Konsole zeigt sie; Mail an Betreiber und Person."""
    import re
    from flask import current_app, url_for
    from ..extensions import db
    from ..models import ClubRequest
    from ..services.mailer import send_mail
    f = request.form
    if f.get("website"):  # Honeypot: Menschen sehen das Feld nicht
        return redirect(url_for("public.platform_home") + "#anfrage")
    errors = {}
    name, contact, email = f.get("club_name", "").strip(), f.get("contact_name", "").strip(), f.get("email", "").strip().lower()
    if not name:
        errors["club_name"] = "Wie heißt dein Klub?"
    if not contact:
        errors["contact_name"] = "Wie dürfen wir dich ansprechen?"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors["email"] = "Bitte gib eine gültige E-Mail-Adresse an."
    if not f.get("consent_privacy"):
        errors["consent_privacy"] = "Bitte stimme der Verarbeitung deiner Angaben zur Bearbeitung der Anfrage zu."
    if errors:
        return _platform_home(errors, f, 400)
    row = ClubRequest(club_name=name[:120], contact_name=contact[:120], email=email[:255],
                      city=f.get("city", "").strip()[:120], size=f.get("size") if f.get("size") in SIZES else "",
                      message=f.get("message", "").strip()[:2000])
    db.session.add(row)
    db.session.commit()
    operator = current_app.config.get("PLATFORM_ADMIN_EMAIL") or club_settings.settings()["contact_email"]
    send_mail(operator, f"Neue Klub-Anfrage: {row.club_name}", "club_request_operator", req=row, sizes=SIZES,
              link=url_for("platform.requests", _external=True))
    send_mail(row.email, f"Deine Anfrage bei {current_app.config['PLATFORM_NAME']}", "club_request_received", req=row)
    return redirect(url_for("public.platform_home", danke=1) + "#anfrage")


@bp.route("/")
def index():
    from ..tenancy import is_platform_host
    if is_platform_host(request.host):
        return _platform_home()
    faq = KnowledgeItem.query.filter_by(active=True, public=True).order_by(KnowledgeItem.sort).limit(8).all()
    return render_template("public/index.html", events=upcoming(4), faq=faq)


@bp.route("/formate/<key>")
def format_page(key):
    if key not in FORMATS or key == "community":
        abort(404)
    return render_template("public/format.html", key=key, f=FORMATS[key], events=upcoming(fmt=key))


@bp.route("/termine")
def events():
    fmt = request.args.get("format")
    return render_template("public/events.html", events=upcoming(fmt=fmt if fmt in FORMATS else None), active=fmt)


@bp.route("/impressum")
def impressum():
    from ..services import club as club_settings
    c = club_settings.settings()
    if c["impressum_url"]:
        return redirect(c["impressum_url"])
    return render_template("public/impressum.html")


@bp.route("/datenschutz")
def privacy():
    return render_template("public/privacy.html")


@bp.route("/cookies")
def cookies():
    return render_template("public/cookies.html")


@bp.route("/nutzungsbedingungen")
def terms():
    return render_template("public/terms.html")


@bp.route("/ki-hinweis")
def ai_notice():
    return render_template("public/ai_notice.html")


@bp.route("/club.css")
@limiter.exempt
def club_css():
    """Farben des Klubs als CSS-Variablen (eigene Datei, weil die CSP keine Inline-Styles erlaubt)."""
    resp = Response(club_settings.css(), mimetype="text/css")
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@bp.route("/klub/medien/<name>")
@limiter.exempt
def club_media(name):
    """Öffentliche Bilder des Klubs (Logo, Startseite, Formate) — nur aus dem Ordner des aktuellen Klubs."""
    if "/" in name or "\\" in name or ".." in name or not name.endswith((".png", ".jpg")):
        abort(404)
    resp = send_from_directory(media.club_media_dir(), name, max_age=86400)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@bp.route("/healthz")
@limiter.exempt
def health():
    return {"ok": True}


@bp.route("/api/aiko", methods=["POST"])
@csrf.exempt  # öffentlich, zustandslos, keine Sitzungsdaten -> kein CSRF-Risiko
@limiter.limit("12 per minute; 80 per hour")
def aiko_public():
    if club_settings.settings()["public_assistant"] != "1":
        abort(404)  # Klub hat den öffentlichen Chat ausgeschaltet
    data = request.get_json(silent=True) or {}
    history = data.get("history") or []
    if not isinstance(history, list) or not history:
        return jsonify(error="Leere Nachricht"), 400
    history = [{"role": str(h.get("role")), "content": str(h.get("content", ""))[:1500]}
               for h in history[-10:] if isinstance(h, dict)]
    return jsonify(reply=aiko.answer_public(history))
