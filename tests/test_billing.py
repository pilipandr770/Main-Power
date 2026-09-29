"""Tarife, Kontingente, KI-Budget und Stripe-Abrechnung (Stripe per Attrappe, keine Netzaufrufe)."""
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import (AddOn, Club, LLMUsage, Payment, Plan, QuotaBonus, Registration, UsageEvent, User, utcnow)
from app.services import billing, plans
from app.tenancy import default_club, use_club
from tests.test_smoke import app, client, login, register  # noqa: F401  (Fixtures)

DEMO = "demo.main-power.local"


def by(first):
    return User.query.filter(User.email.like(f"{first}.%@{DEMO}")).one()


def test_default_plans_are_profitable_in_worst_case(app):
    with app.app_context():
        assert plans.ensure_defaults() == 0            # schon beim Seed angelegt
        for p in Plan.query.all():
            c = plans.calculation(p)
            assert not c["unbounded"], p.key
            if not p.is_free:
                assert c["margin_pct"] >= 40, (p.key, c)
        basis = Plan.query.filter_by(key="basis").one()
        assert plans.worst_case_cents(basis.limits) < 50      # Gratis kostet im schlimmsten Fall < 0,50 €


def test_quota_blocks_after_limit_and_refunds_failed_checks(client, app, monkeypatch):
    register(client)
    for _ in range(20):
        assert client.post("/app/api/profil-coach", json={"q_focus": "IT"}).status_code == 200
    r = client.post("/app/api/profil-coach", json={"q_focus": "IT"})
    assert r.status_code == 402 and "aufgebraucht" in r.get_json()["error"] and r.get_json()["upgrade_url"]

    from app.services import seo_check
    monkeypatch.setattr(seo_check, "analyze", lambda url, kw: (_ for _ in ()).throw(seo_check.SeoCheckError("weg")))
    client.post("/app/seo-check", data={"url": "example.org", "authorized": "1"})
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        assert plans.quota(u, "site_check").used == 0             # gescheiterter Check zurückgebucht
        # Zusatzpaket: nach dem Monatskontingent geht es aus dem Paket weiter
        db.session.add(QuotaBonus(user_id=u.id, feature="ki_aktionen", units=2, source="test"))
        db.session.commit()
    assert client.post("/app/api/profil-coach", json={"q_focus": "IT"}).status_code == 200
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        assert QuotaBonus.query.filter_by(source="test").one().left == 1
    page = client.get("/app/tarif").get_data(as_text=True)
    assert "Mein Tarif" in page and "20 / 20" in page and "+1 Paket" in page


def test_panel_larger_than_plan_needs_matching_pack(client, app):
    register(client)
    data = {"product_name": "Testprodukt", "description": "Ein Werkzeug für Handwerksbetriebe, das Termine plant und "
            "Kund:innen informiert.", "audience": "business", "regional": "de", "unit": "monat", "n": "100",
            "synthetic": "1"}
    html = client.get("/app/markt-panel").get_data(as_text=True)
    assert 'value="25"' in html and 'value="100"' not in html       # Basis: nur 25 Personas wählbar
    r = client.post("/app/markt-panel", data=data)                    # 100 nicht erlaubt -> Lauf mit 25
    assert r.status_code == 302
    with app.app_context(), use_club(default_club()):
        from app.models import PanelRun
        assert PanelRun.query.one().n_requested == 25
    with app.app_context(), use_club(default_club()):
        u = User.query.filter_by(email="neu@example.com").one()
        db.session.add(QuotaBonus(user_id=u.id, feature="panel_runs", units=1, personas=100, source="pack"))
        db.session.commit()
        assert plans.panel_persona_limit(u) == 100
        plans.consume_pack(u, "panel_runs", 100)
        assert QuotaBonus.query.filter_by(source="pack").one().left == 0
        with pytest.raises(plans.QuotaExceeded):
            plans.consume_pack(u, "panel_runs", 100)


def test_club_budget_guard_and_club_assigned_plans(app):
    from app.services.llm import LLMUnavailable
    with app.app_context():
        club = default_club()
        club.plan = "starter"                                      # KI-Budget 24,50 €
        db.session.commit()
        with use_club(club), app.test_request_context():
            db.session.add(LLMUsage(purpose="aiko_public", input_tokens=10_000_000, output_tokens=3_000_000))
            db.session.commit()                                    # 10 $ + 15 $ = 25 $ > Budget
            b = plans.club_budget(club)
            assert b["exhausted"]
            with pytest.raises(LLMUnavailable):
                plans.guard_llm()
            # vom Klub vergebener Pro-Tarif: zählt weiter gegen das Budget
            u = by("julia")
            u.plan_key, u.plan_source = "pro", "admin"
            db.session.commit()
            assert plans.user_plan(u).key == "pro" and not plans.is_paying(u)
            # eigenes Stripe-Abo: ausgenommen
            u.plan_source, u.plan_status = "stripe", "active"
            db.session.commit()
            assert plans.is_paying(u)
            from flask import g
            g.usage_user_id, g.usage_paid = u.id, True
            plans.guard_llm()                                      # kein Fehler für Zahlende
            # abgelaufene Vergabe fällt auf Basis zurück
            u.plan_source, u.plan_until = "admin", utcnow() - timedelta(days=1)
            assert plans.user_plan(u).key == "basis"


def _event(kind, obj):
    return {"type": kind, "data": {"object": obj}}


def test_stripe_webhooks_member_club_addon_and_registration(app):
    with app.app_context():
        club = default_club()
        with use_club(club):
            u = by("tobias")
            uid, cid = u.id, club.id
        meta = {"kind": "member_plan", "user_id": str(uid), "club_id": str(cid), "plan_key": "plus"}
        assert billing.process_event(_event("checkout.session.completed", {
            "id": "cs_1", "mode": "subscription", "customer": "cus_1", "subscription": "sub_1", "metadata": meta})) \
            == "member_plan"
        billing.process_event(_event("customer.subscription.updated", {
            "id": "sub_1", "status": "active", "customer": "cus_1", "cancel_at_period_end": True, "metadata": meta,
            "items": {"data": [{"current_period_end": 1893456000}]}}))
        for _ in range(2):  # doppelter Webhook: nur eine Zahlung
            billing.process_event(_event("invoice.paid", {"id": "in_1", "amount_paid": 900,
                                                          "parent": {"subscription_details": {"subscription": "sub_1"}}}))
        with use_club(club):
            u = db.session.get(User, uid)
            assert (u.plan_key, u.plan_source, u.plan_status, u.plan_cancel_at_end) == ("plus", "stripe", "active", True)
            assert u.plan_period_end.year == 2030 and plans.is_paying(u)
            assert Payment.query.filter_by(reference="invoice:in_1").count() == 1
        billing.process_event(_event("customer.subscription.deleted", {"id": "sub_1", "status": "canceled",
                                                                         "metadata": meta}))
        with use_club(club):
            assert db.session.get(User, uid).plan_key == "basis"

        # Zusatzpaket (idempotent)
        addon_meta = {"kind": "addon", "user_id": str(uid), "club_id": str(cid), "addon_key": "checks-10"}
        for _ in range(2):
            billing.process_event(_event("checkout.session.completed", {
                "id": "cs_2", "mode": "payment", "payment_status": "paid", "amount_total": 990, "metadata": addon_meta}))
        with use_club(club):
            assert QuotaBonus.query.filter_by(user_id=uid).one().units == 10

        # Klub-Abo
        cmeta = {"kind": "club_plan", "club_id": str(cid), "plan_key": "club"}
        billing.process_event(_event("checkout.session.completed", {
            "id": "cs_3", "mode": "subscription", "customer": "cus_c", "subscription": "sub_c", "metadata": cmeta}))
        billing.process_event(_event("invoice.paid", {"id": "in_c", "amount_paid": 17731, "subscription": "sub_c"}))
        club = db.session.get(Club, cid)
        assert (club.plan, club.plan_status) == ("club", "active")
        assert Payment.query.execution_options(all_clubs=True).filter_by(kind="club_plan").one().amount_cents == 17731


def test_registration_payment_in_other_club_is_saved(app):
    """Regressionstest: Objekte wurden vor dem Klubwechsel geladen und gingen verloren."""
    from app.seed import create_club
    with app.app_context():
        other = create_club("nord", "Klub Nord", "chef@nord.test", "nord-passwort-123", demo=True)
        with use_club(other):
            from app.models import Event
            ev = Event.query.first()
            buyer = User(email="kaeufer@nord.test", first_name="K", last_name="N")
            buyer.set_password("x-passwort-123")
            db.session.add(buyer)
            db.session.flush()
            reg = Registration(event_id=ev.id, user_id=buyer.id, status="pending_payment")
            db.session.add(reg)
            db.session.commit()
            rid, oid = reg.id, other.id
    with app.app_context(), use_club(default_club()):
        billing.process_event(_event("checkout.session.completed", {
            "id": "cs_r", "payment_status": "paid", "amount_total": 2500, "metadata": {"registration_id": str(rid)}}))
    with app.app_context(), use_club(db.session.get(Club, oid)):
        assert db.session.get(Registration, rid).status == "paid"


class FakeStripe:
    def __init__(self):
        self.calls = []
        fake = self

        class _Obj:
            @staticmethod
            def create(**kw):
                fake.calls.append(kw)
                return SimpleNamespace(id=f"obj_{len(fake.calls)}", url="https://checkout.stripe.test/session")
        self.Product = self.Price = self.TaxRate = _Obj
        self.checkout = SimpleNamespace(Session=_Obj)
        self.billing_portal = SimpleNamespace(Session=_Obj)


def test_member_checkout_uses_stripe_with_tax_and_requires_consent(client, app, monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(billing, "_stripe", lambda: fake)
    app.config.update(STRIPE_ENABLED=True, STRIPE_SECRET_KEY="sk_test_dummy")
    register(client)
    r = client.post("/app/tarif/plus", follow_redirects=True)
    assert "Widerrufsrecht" in r.get_data(as_text=True) and not fake.calls
    r = client.post("/app/tarif/plus", data={"widerruf": "1"})
    assert r.status_code == 302 and r.headers["Location"] == "https://checkout.stripe.test/session"
    session_kw = fake.calls[-1]
    assert session_kw["mode"] == "subscription" and session_kw["metadata"]["plan_key"] == "plus"
    assert session_kw["line_items"][0]["tax_rates"]                    # MwSt. ausgewiesen
    tax_kw = next(c for c in fake.calls if "percentage" in c)
    assert tax_kw["inclusive"] is True and tax_kw["percentage"] == 19.0
    with app.app_context():
        assert Plan.query.filter_by(key="plus").one().stripe_price_id


def test_platform_tariff_editor_and_admin_assignment(client, app):
    app.config.update(PLATFORM_ADMIN_EMAIL="op@test.local", PLATFORM_ADMIN_PASSWORD="op-passwort-123456")
    with client.session_transaction() as s:
        import time
        s["platform_admin"] = {"email": "op@test.local", "at": time.time()}
    page = client.get("/plattform/tarife").get_data(as_text=True)
    assert "Tarife und Kontingente" in page and "Plus" in page and "Starter" in page
    with app.app_context():
        pid = Plan.query.filter_by(key="plus").one().id
    limits = {f"limit.{f}": "" for f in plans.FEATURES}
    r = client.post(f"/plattform/tarife/{pid}", data={"name": "Plus", "price": "9", "active": "1", **limits},
                    follow_redirects=True)
    assert "unbegrenzt" in r.get_data(as_text=True)                   # Warnung: Kosten nach oben offen
    client.post("/plattform/tarife/einstellungen", data={"vat_rate": "0", "commission_pct": "25"})
    with app.app_context():
        assert plans.vat_rate() == 0 and plans.commission_pct() == 25

    login(client, os.environ.get("ADMIN_EMAIL", "admin@main-power.local"),
          os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern"))
    with app.app_context(), use_club(default_club()):
        j = by("julia")
        jid, email = j.id, j.email
    client.post(f"/admin/mitglieder/{jid}/bearbeiten", data={"first_name": "Julia", "last_name": "Wagner",
                                                             "email": email, "plan_key": "pro",
                                                             "plan_until": "2031-01-31"})
    with app.app_context(), use_club(default_club()):
        j = db.session.get(User, jid)
        assert (j.plan_key, j.plan_source, j.plan_until.year) == ("pro", "admin", 2031)
    assert "Julia Wagner" in client.get("/admin/tarif").get_data(as_text=True)


def test_stripe_objects_and_portal_cancel_at(app):
    """Live-Befunde: stripe-python 15 liefert keine dicts mehr; Portal-Kündigung setzt cancel_at statt cancel_at_period_end."""
    import stripe
    ev = stripe.Event.construct_from({"type": "invoice.paid", "data": {"object": {"id": "in_x", "metadata": {}}}}, "k")
    assert billing.as_dict(ev)["data"]["object"].get("id") == "in_x"
    with app.app_context():
        with use_club(default_club()):
            u = by("markus")
            u.stripe_subscription_id, u.plan_key, u.plan_source, u.plan_status = "sub_x", "plus", "stripe", "active"
            db.session.commit()
            uid = u.id
        billing.process_event(_event("customer.subscription.updated", {
            "id": "sub_x", "status": "active", "cancel_at_period_end": False, "cancel_at": 1893456000,
            "items": {"data": [{"current_period_end": 1890000000}]}, "metadata": {}}))
        with use_club(default_club()):
            u = db.session.get(User, uid)
            assert u.plan_cancel_at_end and u.plan_period_end.year == 2030 and plans.is_paying(u)
