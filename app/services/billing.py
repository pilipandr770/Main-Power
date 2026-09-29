"""Stripe-Abrechnung: Mitglieder-Abos (inkl. MwSt.), Klub-Abos (B2B, zzgl. MwSt.), Zusatzpakete, Kundenportal.

- Produkte/Preise werden bei Bedarf in Stripe angelegt und am Tarif gespeichert (Preisänderung = neuer Stripe-Preis;
  bestehende Abos behalten ihren alten Preis, bis sie gewechselt werden).
- MwSt. über Stripe-Steuersätze (inklusive für Verbraucher, exklusive für Klubs) – Rechnungen weisen sie aus.
- Ein Webhook für alle Klubs: Datensätze werden klubübergreifend gesucht, dann wird im Klub des Datensatzes gearbeitet.
- Idempotent: Zahlungen und Zusatzpakete tragen die Stripe-ID (unique), doppelte Webhooks ändern nichts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import current_app, url_for

from ..extensions import db
from ..models import AddOn, Club, Payment, Plan, PlatformSetting, QuotaBonus, Registration, User
from . import plans

log = logging.getLogger(__name__)


class BillingError(Exception):
    """Nutzerverständlicher Fehler (Deutsch)."""


def enabled() -> bool:
    return bool(current_app.config.get("STRIPE_ENABLED") and current_app.config.get("STRIPE_SECRET_KEY"))


def _stripe():
    import stripe
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    return stripe


# --------------------------------------------------------------------------- Stammdaten in Stripe
def _tax_rate(inclusive: bool) -> str | None:
    rate = plans.vat_rate()
    if rate <= 0:
        return None  # z. B. Kleinunternehmerregelung
    key = f"stripe.tax_rate.{'incl' if inclusive else 'excl'}.{rate:g}"
    tid = PlatformSetting.get(key)
    if not tid:
        tid = _stripe().TaxRate.create(display_name="MwSt.", percentage=rate, inclusive=inclusive, country="DE",
                                       jurisdiction="DE", description=f"Umsatzsteuer {rate:g} %").id
        PlatformSetting.set(key, tid)
        db.session.commit()
    return tid


def _price(obj, recurring: bool, label: str) -> str:
    """Stripe-Preis passend zum aktuellen Betrag des Tarifs/Pakets (legt Produkt und Preis bei Bedarf an)."""
    stripe = _stripe()
    if not obj.stripe_product_id:
        obj.stripe_product_id = stripe.Product.create(name=f"{label}: {obj.name}",
                                                      metadata={"key": obj.key}).id
    if not obj.stripe_price_id or obj.stripe_price_amount != obj.price_cents:
        kw = {"recurring": {"interval": "month"}} if recurring else {}
        obj.stripe_price_id = stripe.Price.create(product=obj.stripe_product_id, unit_amount=obj.price_cents,
                                                  currency="eur", metadata={"key": obj.key}, **kw).id
        obj.stripe_price_amount = obj.price_cents
    db.session.commit()
    return obj.stripe_price_id


def _line(price_id: str, inclusive: bool) -> dict:
    tax = _tax_rate(inclusive)
    return {"price": price_id, "quantity": 1, **({"tax_rates": [tax]} if tax else {})}


# --------------------------------------------------------------------------- Checkout
def checkout_member_plan(user: User, plan: Plan) -> str:
    """Neues Abo per Checkout oder – bei bestehendem Abo – Tarifwechsel mit anteiliger Verrechnung."""
    if not enabled():
        raise BillingError("Online-Zahlung ist noch nicht eingerichtet. Wende dich an die Klubleitung.")
    if plan.kind != "member" or plan.is_free or not plan.active:
        raise BillingError("Dieser Tarif kann nicht gebucht werden.")
    stripe = _stripe()
    price = _price(plan, True, "Mitglieder-Tarif")
    if user.stripe_subscription_id and user.plan_status in plans.PAID_STATUS:
        sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
        item = sub["items"]["data"][0]
        stripe.Subscription.modify(sub.id, items=[{"id": item.id, "price": price}], proration_behavior="create_prorations",
                                   cancel_at_period_end=False, metadata={**_meta_user(user), "plan_key": plan.key})
        user.plan_key, user.plan_cancel_at_end = plan.key, False
        db.session.commit()
        return url_for("member.plan_page", gewechselt=1)
    meta = {**_meta_user(user), "kind": "member_plan", "plan_key": plan.key}
    session = stripe.checkout.Session.create(
        mode="subscription", line_items=[_line(price, True)], locale="de",
        **({"customer": user.stripe_customer_id} if user.stripe_customer_id else {"customer_email": user.email}),
        metadata=meta, subscription_data={"metadata": meta},
        success_url=url_for("member.plan_page", ok=1, _external=True) + "&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("member.plan_page", _external=True))
    return session.url


def checkout_addon(user: User, addon: AddOn) -> str:
    if not enabled():
        raise BillingError("Online-Zahlung ist noch nicht eingerichtet. Wende dich an die Klubleitung.")
    if not addon.active:
        raise BillingError("Dieses Paket ist nicht verfügbar.")
    price = _price(addon, False, "Zusatzpaket")
    meta = {**_meta_user(user), "kind": "addon", "addon_key": addon.key}
    session = _stripe().checkout.Session.create(
        mode="payment", line_items=[_line(price, True)], locale="de",
        **({"customer": user.stripe_customer_id} if user.stripe_customer_id else {"customer_email": user.email}),
        metadata=meta, invoice_creation={"enabled": True},
        success_url=url_for("member.plan_page", paket=1, _external=True) + "&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("member.plan_page", _external=True))
    return session.url


def checkout_club_plan(club: Club, plan: Plan, admin: User) -> str:
    if not enabled():
        raise BillingError("Online-Zahlung ist noch nicht eingerichtet. Bitte an den Plattform-Betrieb wenden.")
    if plan.kind != "club" or not plan.active:
        raise BillingError("Dieser Tarif kann nicht gebucht werden.")
    stripe = _stripe()
    price = _price(plan, True, "Klub-Tarif")
    if club.stripe_subscription_id and club.plan_status in plans.PAID_STATUS:
        sub = stripe.Subscription.retrieve(club.stripe_subscription_id)
        item = sub["items"]["data"][0]
        stripe.Subscription.modify(sub.id, items=[{"id": item.id, "price": price}], proration_behavior="create_prorations",
                                   cancel_at_period_end=False,
                                   metadata={"kind": "club_plan", "club_id": str(club.id), "plan_key": plan.key})
        club.plan, club.plan_cancel_at_end = plan.key, False
        db.session.commit()
        return url_for("admin.billing", gewechselt=1)
    meta = {"kind": "club_plan", "club_id": str(club.id), "plan_key": plan.key, "admin_id": str(admin.id)}
    session = stripe.checkout.Session.create(
        mode="subscription", line_items=[_line(price, False)], locale="de",
        **({"customer": club.stripe_customer_id} if club.stripe_customer_id
           else {"customer_email": club.contact_email or admin.email}),
        billing_address_collection="required", tax_id_collection={"enabled": True},
        metadata=meta, subscription_data={"metadata": meta},
        success_url=url_for("admin.billing", ok=1, _external=True) + "&session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("admin.billing", _external=True))
    return session.url


def as_dict(obj):
    """StripeObject (stripe-python >= 15 kein dict mehr) rekursiv in normale dicts/Listen umwandeln."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj


def sync_checkout(session_id: str, user: User | None = None, club: Club | None = None) -> str:
    """Nach der Rückkehr vom Checkout sofort übernehmen, statt auf den Webhook zu warten (idempotent).
    Nur Sessions, die zu diesem Mitglied bzw. Klub gehören."""
    if not enabled() or not session_id.startswith("cs_"):
        return "skip"
    stripe = _stripe()
    session = as_dict(stripe.checkout.Session.retrieve(session_id, expand=["subscription"]))
    meta = session.get("metadata") or {}
    if user is not None and meta.get("user_id") != str(user.id):
        return "fremd"
    if club is not None and meta.get("club_id") != str(club.id):
        return "fremd"
    sub = session.get("subscription")
    if isinstance(sub, dict):
        session["subscription"] = sub.get("id")
    result = process_event({"type": "checkout.session.completed", "data": {"object": session}})
    if isinstance(sub, dict):
        process_event({"type": "customer.subscription.updated", "data": {"object": sub}})
    return result


def sync_subscription(sub_id: str) -> str:
    """Abo-Status direkt bei Stripe abfragen (z. B. nach der Rückkehr aus dem Kundenportal)."""
    if not enabled() or not sub_id:
        return "skip"
    sub = as_dict(_stripe().Subscription.retrieve(sub_id))
    kind = "customer.subscription.deleted" if sub.get("status") == "canceled" else "customer.subscription.updated"
    return process_event({"type": kind, "data": {"object": sub}})


def portal_url(customer_id: str, return_url: str) -> str:
    if not enabled() or not customer_id:
        raise BillingError("Kein Stripe-Konto vorhanden.")
    return _stripe().billing_portal.Session.create(customer=customer_id, return_url=return_url, locale="de").url


def _meta_user(user: User) -> dict:
    return {"user_id": str(user.id), "club_id": str(user.club_id)}


# --------------------------------------------------------------------------- Webhooks
def _enter(club_id) -> Club | None:
    from ..tenancy import set_club
    club = db.session.get(Club, int(club_id)) if str(club_id or "").isdigit() else None
    set_club(club)
    return club


def _ts(value) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None) if value else None


def _period_end(sub) -> datetime | None:
    """Neuere API-Versionen: Periodenende je Abo-Position; ältere: am Abo."""
    end = sub.get("current_period_end")
    if not end:
        items = (sub.get("items") or {}).get("data") or []
        end = max((i.get("current_period_end") or 0 for i in items), default=0)
    return _ts(end)


def _owner_by_subscription(sub_id: str):
    """Besitzer eines Abos (Mitglied oder Klub) – erst den Klub betreten, dann laden: set_club() entfernt Objekte
    eines anderen Klubs aus der Session, Änderungen an vorher geladenen Objekten gingen sonst verloren."""
    if not sub_id:
        return None
    ref = (User.query.execution_options(all_clubs=True).with_entities(User.id, User.club_id)
           .filter_by(stripe_subscription_id=sub_id).first())
    if ref:
        _enter(ref.club_id)
        return db.session.get(User, ref.id)
    club = Club.query.filter_by(stripe_subscription_id=sub_id).first()
    if club:
        return _enter(club.id)
    return None


def _invoice_subscription(inv) -> str | None:
    sub = inv.get("subscription")
    if not sub:
        parent = inv.get("parent") or {}
        sub = (parent.get("subscription_details") or {}).get("subscription")
    return sub if isinstance(sub, str) else (sub or {}).get("id")


def _record_payment(club_id, **kw) -> bool:
    if Payment.query.execution_options(all_clubs=True).filter_by(reference=kw["reference"]).first():
        return False
    from ..tenancy import current_club_id
    if current_club_id() != club_id:
        _enter(club_id)
    db.session.add(Payment(**kw))
    db.session.commit()
    return True


def process_event(event) -> str:
    """Stripe-Ereignis verarbeiten (bereits signaturgeprüft). Gibt eine kurze Beschreibung zurück."""
    kind, obj = event["type"], event["data"]["object"]
    meta = obj.get("metadata") or {}

    if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        what = meta.get("kind") or ("registration" if meta.get("registration_id") else "")
        if what == "registration":
            return _paid_registration(obj)
        if what == "member_plan" and obj.get("mode") == "subscription":
            _enter(meta.get("club_id"))
            user = db.session.get(User, int(meta.get("user_id", 0)))
            if user:
                user.plan_key, user.plan_source, user.plan_status = meta.get("plan_key", "basis"), "stripe", "active"
                user.stripe_customer_id = obj.get("customer") or user.stripe_customer_id
                user.stripe_subscription_id = obj.get("subscription") or user.stripe_subscription_id
                user.plan_until, user.plan_cancel_at_end = None, False
                db.session.commit()
            return "member_plan"
        if what == "club_plan" and obj.get("mode") == "subscription":
            club = _enter(meta.get("club_id"))
            if club:
                club.plan, club.plan_status = meta.get("plan_key", club.plan), "active"
                club.stripe_customer_id = obj.get("customer") or club.stripe_customer_id
                club.stripe_subscription_id = obj.get("subscription") or club.stripe_subscription_id
                club.plan_cancel_at_end = False
                db.session.commit()
            return "club_plan"
        if what == "addon" and obj.get("payment_status") == "paid":
            _enter(meta.get("club_id"))
            addon = AddOn.query.filter_by(key=meta.get("addon_key")).first()
            user = db.session.get(User, int(meta.get("user_id", 0)))
            ref = f"checkout:{obj.get('id')}"
            if addon and user and not QuotaBonus.query.filter_by(source=ref).first():
                db.session.add(QuotaBonus(user_id=user.id, feature=addon.feature, units=addon.units,
                                          personas=addon.personas, source=ref))
                if obj.get("customer") and not user.stripe_customer_id:
                    user.stripe_customer_id = obj["customer"]
                db.session.commit()
                _record_payment(user.club_id, user_id=user.id, kind="addon", plan_key=addon.key, reference=ref,
                                amount_cents=obj.get("amount_total") or addon.price_cents, description=addon.name)
            return "addon"
        return "checkout_ignored"

    if kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        owner = _owner_by_subscription(obj.get("id"))
        if owner is None and meta.get("kind") == "member_plan":  # Update kam vor checkout.session.completed
            _enter(meta.get("club_id"))
            owner = db.session.get(User, int(meta.get("user_id", 0)))
            if owner:
                owner.stripe_subscription_id, owner.plan_source = obj.get("id"), "stripe"
        if owner is None and meta.get("kind") == "club_plan":
            owner = _enter(meta.get("club_id"))
            if owner:
                owner.stripe_subscription_id = obj.get("id")
        if owner is None:
            return "subscription_unknown"
        status = "canceled" if kind.endswith("deleted") else obj.get("status", "")
        owner.plan_status = status
        owner.plan_period_end = _period_end(obj)
        # Neuere API-Versionen kündigen über das Portal mit cancel_at (Zeitpunkt) statt cancel_at_period_end
        owner.plan_cancel_at_end = bool(obj.get("cancel_at_period_end") or obj.get("cancel_at"))
        if obj.get("cancel_at"):
            owner.plan_period_end = _ts(obj["cancel_at"])
        owner.stripe_customer_id = obj.get("customer") or owner.stripe_customer_id
        if meta.get("plan_key"):
            if isinstance(owner, User):
                owner.plan_key = meta["plan_key"]
            else:
                owner.plan = meta["plan_key"]
        if isinstance(owner, User) and status == "canceled":
            owner.plan_key, owner.plan_source = "basis", ""
        db.session.commit()
        return f"subscription_{status}"

    if kind in ("invoice.paid", "invoice.payment_failed"):
        sub_id = _invoice_subscription(obj)
        owner = _owner_by_subscription(sub_id) if sub_id else None
        if owner is None:
            return "invoice_unknown"
        club_id = owner.club_id if isinstance(owner, User) else owner.id
        if kind == "invoice.payment_failed":
            owner.plan_status = "past_due"
            db.session.commit()
            return "invoice_failed"
        is_user = isinstance(owner, User)
        _record_payment(club_id, user_id=owner.id if is_user else None,
                        kind="member_plan" if is_user else "club_plan",
                        plan_key=owner.plan_key if is_user else owner.plan, reference=f"invoice:{obj.get('id')}",
                        amount_cents=obj.get("amount_paid") or 0,
                        description=f"Abo {owner.plan_key if is_user else owner.plan}")
        if owner.plan_status in ("past_due", "incomplete", ""):
            owner.plan_status = "active"
            db.session.commit()
        return "invoice_paid"
    return "ignored"


def _paid_registration(obj) -> str:
    if obj.get("payment_status") != "paid":
        return "registration_unpaid"
    reg_id = int((obj.get("metadata") or {}).get("registration_id", 0))
    ref = (Registration.query.execution_options(all_clubs=True).with_entities(Registration.club_id)
           .filter_by(id=reg_id).first())
    reg = None
    if ref:
        _enter(ref.club_id)
        reg = db.session.get(Registration, reg_id)
    if reg:
        reg.status = "paid"
        reg.amount_cents = obj.get("amount_total") or reg.amount_cents
        db.session.commit()
        _record_payment(reg.club_id, user_id=reg.user_id, kind="event", plan_key="", amount_cents=reg.amount_cents,
                        reference=f"checkout:{obj.get('id')}", description=f"Termin {reg.event_id}")
        log.info("Registrierung %s bezahlt", reg_id)
    return "registration"
