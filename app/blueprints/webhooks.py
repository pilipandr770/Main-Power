import hmac
import logging

from flask import Blueprint, abort, current_app, request

from ..extensions import db, limiter
from ..services import payments, telegram

bp = Blueprint("webhooks", __name__)
log = logging.getLogger(__name__)


@bp.route("/stripe", methods=["POST"])
@limiter.exempt
def stripe_webhook():
    if not payments.stripe_enabled():
        abort(404)
    try:
        kind = payments.handle_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
    except Exception:
        log.exception("Stripe-Webhook abgelehnt")
        abort(400)
    return {"received": kind}


@bp.route("/telegram", methods=["POST"])
@limiter.exempt
def telegram_webhook():
    secret = current_app.config.get("TELEGRAM_WEBHOOK_SECRET", "")
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not telegram.enabled() or not secret or not hmac.compare_digest(secret, got):
        abort(403)
    try:
        telegram.handle_update(request.get_json(force=True, silent=True) or {})
    except Exception:
        log.exception("Telegram-Update fehlgeschlagen")
        db.session.rollback()
    return {"ok": True}
