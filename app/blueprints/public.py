from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, Response, abort, jsonify, render_template, request, send_from_directory

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


@bp.route("/")
def index():
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
    data = request.get_json(silent=True) or {}
    history = data.get("history") or []
    if not isinstance(history, list) or not history:
        return jsonify(error="Leere Nachricht"), 400
    history = [{"role": str(h.get("role")), "content": str(h.get("content", ""))[:1500]}
               for h in history[-10:] if isinstance(h, dict)]
    return jsonify(reply=aiko.answer_public(history))
