from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, abort, jsonify, render_template, request

from ..extensions import csrf, limiter
from ..models import FORMATS, Event, KnowledgeItem, utcnow
from ..services import aiko

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


@bp.route("/ki-hinweis")
def ai_notice():
    return render_template("public/ai_notice.html")


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
