"""Stripe Checkout für kostenpflichtige Termine (z. B. Main Power Hub, 25 €).

Feature-Flag STRIPE_ENABLED. Ist Stripe aus, wird eine Anmeldung als 'reserved' (Zahlung vor Ort) gespeichert.
"""
from __future__ import annotations

import logging

from flask import current_app, url_for

from ..extensions import db
from ..models import Registration

log = logging.getLogger(__name__)


def _club_name() -> str:
    from . import club as club_settings
    return club_settings.settings().get("full_name") or "Community"


def stripe_enabled() -> bool:
    return bool(current_app.config.get("STRIPE_ENABLED") and current_app.config.get("STRIPE_SECRET_KEY"))


def _stripe():
    import stripe
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


def create_checkout(reg: Registration) -> str:
    stripe = _stripe()
    ev = reg.event
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=reg.user.email,
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "eur",
                "unit_amount": ev.price_cents,
                "product_data": {"name": ev.title, "description": "Teilnahme · " + _club_name()},
            },
        }],
        metadata={"registration_id": str(reg.id), "club_id": str(reg.club_id), "event_id": str(ev.id), "user_id": str(reg.user_id)},
        success_url=url_for("member.event_detail", event_id=ev.id, paid=1, _external=True),
        cancel_url=url_for("member.event_detail", event_id=ev.id, _external=True),
        locale="de",
    )
    reg.stripe_session_id = session.id
    reg.status = "pending_payment"
    reg.amount_cents = ev.price_cents
    db.session.commit()
    return session.url


def handle_webhook(payload: bytes, sig_header: str) -> str:
    stripe = _stripe()
    event = stripe.Webhook.construct_event(payload, sig_header, current_app.config["STRIPE_WEBHOOK_SECRET"])
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        obj = event["data"]["object"]
        if obj.get("payment_status") == "paid":
            reg_id = int(obj.get("metadata", {}).get("registration_id", 0))
            # Ein Webhook-Endpunkt für alle Klubs: Anmeldung klubübergreifend suchen, dann in ihrem Klub arbeiten
            reg = Registration.query.execution_options(all_clubs=True).filter_by(id=reg_id).first()
            if reg:
                from ..tenancy import set_club
                from ..models import Club
                set_club(db.session.get(Club, reg.club_id))
                reg.status = "paid"
                reg.amount_cents = obj.get("amount_total") or reg.amount_cents
                db.session.commit()
                log.info("Registrierung %s bezahlt", reg_id)
    return event["type"]
