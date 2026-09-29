"""Tarife, Kontingente und KI-Budget – damit keine Funktion mehr kostet, als der Tarif einbringt.

Zwei Ebenen:
- Mitglieder-Tarife (Basis kostenlos, Plus, Pro): Monatskontingente je Funktion, Zusatzpakete obendrauf.
- Klub-Tarife (Starter, Club, Verband): der Klub zahlt die Plattform; die Gratis-Nutzung seiner Mitglieder
  (Basis-Tarif, öffentliche Aiko, automatische Matching-Texte) ist durch ai_budget_cents gedeckelt. Ist das Budget
  erreicht, liefert llm.complete „nicht verfügbar“ und die Funktionen nutzen ihre Fallbacks ohne KI.
  Zahlende Mitglieder sind davon ausgenommen – ihr Tarif trägt ihre Kosten.

Kostenansätze je Nutzung (Cent, mit Puffer) sind Plattform-Einstellungen; die Tarifverwaltung zeigt damit die
Kosten im ungünstigsten Fall (alle Kontingente voll ausgeschöpft) und die Marge nach MwSt. und Stripe-Gebühr.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from flask import current_app, g, has_app_context, has_request_context

from ..extensions import db
from ..models import AddOn, Club, LLMUsage, Plan, PlatformSetting, QuotaBonus, UsageEvent, User, utcnow

log = logging.getLogger(__name__)

# Funktion -> (Bezeichnung, Erklärung, Kostenansatz in Cent je Einheit)
FEATURES = {
    "ki_aktionen": ("KI-Aktionen", "Chat mit der KI-Assistenz, Profil-Coach, Profil-Interview, Kontakt-Assistent", 0.7),
    "goal_checkin": ("Ziel-Check-ins", "Rückmeldung der KI zu deinem 90-Tage-Meilenstein", 0.7),
    "law_search": ("Gesetzes-Suche", "Fundstellen im Bundesrecht mit KI-Einordnung", 1.5),
    "site_check": ("Website-Checks", "SEO-, Compliance- und Sicherheits-Check", 1.3),
    "panel_runs": ("Markt-Panel-Läufe", "Synthetische Marktbefragung", 5.0),     # Auswertung je Lauf
    "panel_personas": ("Personas je Markt-Panel", "Größe eines Laufs", 0.55),    # je Persona
}
QUOTA_FEATURES = [f for f in FEATURES if f != "panel_personas"]
PERSONA_SIZES = (25, 50, 100, 200, 300, 500)

DEFAULT_PLANS = [
    dict(key="basis", kind="member", name="Basis", tagline="Kostenlos für alle Mitglieder", price_cents=0, sort=1,
         limits={"ki_aktionen": 20, "goal_checkin": 5, "law_search": 2, "site_check": 1, "panel_runs": 1,
                 "panel_personas": 25},
         features="Matching, Mitgliederverzeichnis und Termine\nProfil-Interview und Ziel mit KI-Rückmeldung\n"
                  "1 Website-Check und 1 Probe-Markt-Panel im Monat"),
    dict(key="plus", kind="member", name="Plus", tagline="Für alle, die das Netzwerk aktiv nutzen", price_cents=900,
         sort=2, limits={"ki_aktionen": 300, "goal_checkin": 30, "law_search": 20, "site_check": 10, "panel_runs": 2,
                         "panel_personas": 100},
         features="300 KI-Aktionen im Monat\n20 Gesetzes-Suchen, 10 Website-Checks\n2 Markt-Panels à 100 Personas"),
    dict(key="pro", kind="member", name="Pro", tagline="Für Unternehmer:innen mit viel Bedarf", price_cents=2900,
         sort=3, limits={"ki_aktionen": 800, "goal_checkin": 60, "law_search": 100, "site_check": 40, "panel_runs": 5,
                         "panel_personas": 200},
         features="800 KI-Aktionen im Monat\n100 Gesetzes-Suchen, 40 Website-Checks\n5 Markt-Panels bis 200 Personas"),
    dict(key="starter", kind="club", name="Starter", tagline="Für neue Klubs bis 50 Mitglieder", price_cents=4900,
         sort=11, members_included=50, extra_member_cents=40, ai_budget_cents=2450, max_club_questions=3,
         features="Bis 50 Mitglieder\nEigenes Branding, Formate, 3 eigene Fragen\nAuswertung für die Klubleitung"),
    dict(key="club", kind="club", name="Club", tagline="Für etablierte Klubs bis 250 Mitglieder", price_cents=14900,
         sort=12, members_included=250, extra_member_cents=40, ai_budget_cents=7450, max_club_questions=8,
         features="Bis 250 Mitglieder\nEigene Domain, 8 eigene Fragen\n30 % Provision auf Plus/Pro der Mitglieder"),
    dict(key="verband", kind="club", name="Verband", tagline="Für Verbände und Netzwerke bis 1.000 Mitglieder",
         price_cents=39900, sort=13, members_included=1000, extra_member_cents=40, ai_budget_cents=19950,
         max_club_questions=8, features="Bis 1.000 Mitglieder\nMehrere Admins, alle Funktionen\n"
                                         "30 % Provision auf Plus/Pro der Mitglieder"),
]
DEFAULT_ADDONS = [
    dict(key="panel-100", name="Markt-Panel mit 100 Personas", price_cents=990, feature="panel_runs", units=1,
         personas=100, sort=1, description="Ein zusätzlicher Lauf mit 100 fiktiven Personas"),
    dict(key="panel-300", name="Markt-Panel mit 300 Personas", price_cents=2490, feature="panel_runs", units=1,
         personas=300, sort=2, description="Ein zusätzlicher Lauf mit 300 fiktiven Personas"),
    dict(key="checks-10", name="10 Website-Checks", price_cents=990, feature="site_check", units=10, sort=3,
         description="SEO-, Compliance- oder Sicherheits-Check, beliebig kombinierbar"),
    dict(key="gesetze-50", name="50 Gesetzes-Suchen", price_cents=490, feature="law_search", units=50, sort=4,
         description="Fundstellen im Bundesrecht mit Einordnung"),
    dict(key="ki-500", name="500 KI-Aktionen", price_cents=490, feature="ki_aktionen", units=500, sort=5,
         description="Chat, Coach, Interview und Kontakt-Assistent"),
]


def ensure_defaults() -> int:
    """Standard-Tarife und Zusatzpakete anlegen, falls die Tabellen leer sind. Gibt die Zahl neuer Einträge zurück."""
    n = 0
    if Plan.query.count() == 0:
        for spec in DEFAULT_PLANS:
            db.session.add(Plan(**spec))
            n += 1
    if AddOn.query.count() == 0:
        for spec in DEFAULT_ADDONS:
            db.session.add(AddOn(**spec))
            n += 1
    db.session.commit()
    return n


# --------------------------------------------------------------------------- Plattform-Einstellungen
def setting_float(key: str, default: float) -> float:
    try:
        return float(PlatformSetting.get(key, str(default)).replace(",", "."))
    except ValueError:
        return default


def vat_rate() -> float:
    return setting_float("vat_rate", 19.0)


def commission_pct() -> float:
    return setting_float("commission_pct", 30.0)


def unit_cost(feature: str) -> float:
    return setting_float(f"cost.{feature}", FEATURES[feature][2])


# --------------------------------------------------------------------------- Kalkulation
def worst_case_cents(limits: dict | None) -> float:
    """Kosten, wenn alle Kontingente eines Monats voll ausgeschöpft werden (None = unbegrenzt -> nicht kalkulierbar)."""
    limits = limits or {}
    total = 0.0
    for f in QUOTA_FEATURES:
        n = limits.get(f)
        if n is None:
            return float("inf")
        if f == "panel_runs":
            total += n * (unit_cost("panel_runs") + (limits.get("panel_personas") or 0) * unit_cost("panel_personas"))
        else:
            total += n * unit_cost(f)
    return total


def stripe_fee_cents(gross_cents: int) -> float:
    return gross_cents * 0.015 + 25 if gross_cents else 0.0


def calculation(plan: Plan) -> dict:
    """Netto-Erlös, Kosten im ungünstigsten Fall, Marge – für die Tarifverwaltung."""
    vat = vat_rate() / 100
    if plan.kind == "member":
        gross = plan.price_cents
        net = gross / (1 + vat)
        cost = worst_case_cents(plan.limits)
    else:
        net = plan.price_cents
        gross = round(net * (1 + vat))
        basis = Plan.query.filter_by(key="basis").first()
        per_member = worst_case_cents(basis.limits if basis else {})
        cost = min(plan.ai_budget_cents or float("inf"), (plan.members_included or 0) * per_member)
    fee = stripe_fee_cents(gross)
    margin = net - cost - fee
    return {"gross": gross, "net": net, "cost": cost, "fee": fee, "margin": margin,
            "margin_pct": (100 * margin / net) if net and cost != float("inf") else None,
            "unbounded": cost == float("inf")}


def measured_costs(days: int = 30) -> dict[str, float]:
    """Gemessene Ø-Kosten je KI-Zweck (Cent) über alle Klubs – zum Abgleich mit den Kostenansätzen."""
    from datetime import timedelta
    from sqlalchemy import func
    since = utcnow() - timedelta(days=days)
    cfg = current_app.config
    rows = (db.session.query(LLMUsage.purpose, func.count(LLMUsage.id), func.sum(LLMUsage.input_tokens),
                             func.sum(LLMUsage.output_tokens))
            .execution_options(all_clubs=True).filter(LLMUsage.created_at >= since).group_by(LLMUsage.purpose).all())
    return {p: ((i or 0) * cfg["LLM_PRICE_IN"] + (o or 0) * cfg["LLM_PRICE_OUT"]) / 1_000_000 * 100 / max(n, 1)
            for p, n, i, o in rows}


# --------------------------------------------------------------------------- Mitglieder-Tarif
PAID_STATUS = ("active", "trialing", "past_due")


def user_plan(user: User) -> Plan:
    """Aktiver Mitglieder-Tarif; abgelaufene Vergaben und beendete Abos fallen auf Basis zurück."""
    key = user.plan_key or "basis"
    if key != "basis":
        if user.plan_source == "admin" and user.plan_until and user.plan_until < utcnow():
            key = "basis"
        elif user.plan_source == "stripe" and user.plan_status not in PAID_STATUS:
            key = "basis"
    plan = Plan.query.filter_by(key=key, kind="member").first() or Plan.query.filter_by(key="basis").first()
    if plan is None:  # Tabelle leer (z. B. frische Test-DB)
        ensure_defaults()
        plan = Plan.query.filter_by(key="basis").first()
    return plan


def is_paying(user: User | None) -> bool:
    """Nur ein bezahltes Stripe-Abo nimmt die Nutzung aus dem Klub-Budget. Vom Klub vergebene Tarife zählen gegen das
    Budget des Klubs – sonst könnte ein Klub allen Pro schenken und die Kosten auf die Plattform verlagern."""
    if not (user and user.is_authenticated):
        return False
    return user.plan_source == "stripe" and user.plan_status in PAID_STATUS and not user_plan(user).is_free


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass
class Quota:
    feature: str
    limit: int | None      # None = unbegrenzt
    used: int
    bonus_left: int

    @property
    def left(self) -> int | None:
        return None if self.limit is None else max(0, self.limit - self.used) + self.bonus_left

    def allows(self, units: int = 1) -> bool:
        return self.limit is None or self.left >= units

    @property
    def pct(self) -> int:
        if not self.limit:
            return 0 if self.limit is None else 100
        return min(100, round(100 * self.used / self.limit))


def quota(user: User, feature: str) -> Quota:
    limit = (user_plan(user).limits or {}).get(feature)
    from sqlalchemy import func
    used = (db.session.query(func.coalesce(func.sum(UsageEvent.units), 0))
            .filter(UsageEvent.user_id == user.id, UsageEvent.feature == feature, UsageEvent.from_bonus.is_(False),
                    UsageEvent.created_at >= month_start()).scalar())
    bonus = sum(b.left for b in QuotaBonus.query.filter_by(user_id=user.id, feature=feature))
    return Quota(feature, limit, int(used or 0), bonus)


class QuotaExceeded(Exception):
    def __init__(self, feature: str, q: Quota):
        self.feature, self.quota = feature, q
        name = FEATURES[feature][0]
        super().__init__(f"Dein Kontingent für {name} ist für diesen Monat aufgebraucht ({q.used} von {q.limit}). "
                         "Mit einem höheren Tarif oder einem Zusatzpaket geht es sofort weiter.")


def refund(user: User, feature: str) -> None:
    """Letzte Nutzung zurückbuchen, wenn die Funktion gescheitert ist (z. B. Website nicht erreichbar)."""
    ev = (UsageEvent.query.filter_by(user_id=user.id, feature=feature)
          .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).first())
    if ev is None:
        return
    if ev.from_bonus:
        b = (QuotaBonus.query.filter(QuotaBonus.user_id == user.id, QuotaBonus.feature == feature,
                                     QuotaBonus.used > 0).order_by(QuotaBonus.created_at.desc()).first())
        if b:
            b.used = max(0, b.used - ev.units)
    db.session.delete(ev)
    db.session.commit()


def consume_pack(user: User, feature: str, min_personas: int) -> None:
    """Markt-Panel größer als der Tarif erlaubt: nur aus einem Paket mit passender Persona-Zahl."""
    pack = next((b for b in QuotaBonus.query.filter_by(user_id=user.id, feature=feature).order_by(QuotaBonus.created_at)
                 if b.left and (b.personas or 0) >= min_personas), None)
    if pack is None:
        raise QuotaExceeded(feature, quota(user, feature))
    pack.used += 1
    db.session.add(UsageEvent(user_id=user.id, feature=feature, units=1, from_bonus=True))
    db.session.commit()


def consume(user: User, feature: str, units: int = 1) -> Quota:
    """Kontingent prüfen und verbrauchen (erst Monatskontingent, dann Zusatzpakete). Wirft QuotaExceeded."""
    q = quota(user, feature)
    if not q.allows(units):
        raise QuotaExceeded(feature, q)
    monthly_left = None if q.limit is None else max(0, q.limit - q.used)
    if monthly_left is None or monthly_left >= units:
        db.session.add(UsageEvent(user_id=user.id, feature=feature, units=units))
    else:
        rest = units - monthly_left
        if monthly_left:
            db.session.add(UsageEvent(user_id=user.id, feature=feature, units=monthly_left))
        for b in QuotaBonus.query.filter_by(user_id=user.id, feature=feature).order_by(QuotaBonus.created_at):
            take = min(b.left, rest)
            if take:
                b.used += take
                rest -= take
            if not rest:
                break
        db.session.add(UsageEvent(user_id=user.id, feature=feature, units=units - monthly_left, from_bonus=True))
    db.session.commit()
    return quota(user, feature)


def panel_persona_limit(user: User) -> int:
    """Größter erlaubter Markt-Panel-Lauf: Tarif oder – wenn größer – ein gekauftes Panel-Paket."""
    base = (user_plan(user).limits or {}).get("panel_personas") or 0
    packs = [b.personas or 0 for b in QuotaBonus.query.filter_by(user_id=user.id, feature="panel_runs") if b.left]
    return max([base] + packs)


def usage_overview(user: User) -> list[dict]:
    out = []
    for f in QUOTA_FEATURES:
        q = quota(user, f)
        out.append({"feature": f, "name": FEATURES[f][0], "hint": FEATURES[f][1], "q": q})
    return out


# --------------------------------------------------------------------------- Klub-Budget (Gratis-Nutzung)
def club_plan(club: Club | None) -> Plan | None:
    if not club or not club.plan:
        return None
    return Plan.query.filter_by(key=club.plan, kind="club").first()


def club_free_spend_cents(club_id: int) -> float:
    from sqlalchemy import func
    cfg = current_app.config
    i, o = (db.session.query(func.coalesce(func.sum(LLMUsage.input_tokens), 0),
                             func.coalesce(func.sum(LLMUsage.output_tokens), 0))
            .execution_options(all_clubs=True)
            .filter(LLMUsage.club_id == club_id, LLMUsage.paid.is_(False), LLMUsage.created_at >= month_start())
            .one())
    return ((i or 0) * cfg["LLM_PRICE_IN"] + (o or 0) * cfg["LLM_PRICE_OUT"]) / 1_000_000 * 100


DEFAULT_DAILY_CAP_CENTS = 3000  # 30 € Gratis-KI je Tag über alle Klubs


def daily_cap_cents() -> float:
    """Notbremse der Plattform: Gratis-KI (alle Klubs, auch ohne Klub-Tarif) je Kalendertag (UTC). 0 = aus."""
    try:
        return float(PlatformSetting.get("ai_daily_cap_cents", str(DEFAULT_DAILY_CAP_CENTS)))
    except ValueError:
        return DEFAULT_DAILY_CAP_CENTS


def free_spend_today_cents() -> float:
    from sqlalchemy import func
    cfg = current_app.config
    day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    i, o = (db.session.query(func.coalesce(func.sum(LLMUsage.input_tokens), 0),
                             func.coalesce(func.sum(LLMUsage.output_tokens), 0))
            .execution_options(all_clubs=True)
            .filter(LLMUsage.paid.is_(False), LLMUsage.created_at >= day)
            .one())
    return ((i or 0) * cfg["LLM_PRICE_IN"] + (o or 0) * cfg["LLM_PRICE_OUT"]) / 1_000_000 * 100


def club_budget(club: Club | None) -> dict:
    plan = club_plan(club)
    limit = plan.ai_budget_cents if plan else None
    spent = club_free_spend_cents(club.id) if club else 0.0
    return {"limit": limit, "spent": spent, "pct": min(100, round(100 * spent / limit)) if limit else 0,
            "exhausted": bool(limit) and spent >= limit}


def usage_context() -> tuple[int | None, bool]:
    """(user_id, zahlend) für den laufenden KI-Aufruf: aus dem Request oder aus g (Hintergrund-Threads)."""
    if not has_app_context():
        return None, False
    if "usage_user_id" in g:
        return g.usage_user_id, bool(g.get("usage_paid"))
    if has_request_context():
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            paid = is_paying(current_user)
            g.usage_user_id, g.usage_paid = current_user.id, paid
            return current_user.id, paid
    return None, False


def guard_llm() -> None:
    """Vor jedem KI-Aufruf: Gratis-Nutzung stoppen, wenn das Monatsbudget des Klub-Tarifs erreicht ist."""
    from ..tenancy import current_club
    from .llm import LLMUnavailable
    club = current_club()
    if club is None:
        return
    _uid, paid = usage_context()
    if paid:
        return
    cap = daily_cap_cents()
    if cap and free_spend_today_cents() >= cap:
        log.warning("Tageslimit der Gratis-KI (%.0f Cent) erreicht – KI bis Mitternacht (UTC) aus", cap)
        raise LLMUnavailable("Tageslimit der Plattform erreicht")
    b = club_budget(club)
    if b["limit"] and b["spent"] >= b["limit"]:
        raise LLMUnavailable("KI-Budget des Klubs für diesen Monat erreicht")
    if b["limit"] and b["spent"] >= 0.8 * b["limit"]:
        _warn_budget(club, b)


def _warn_budget(club: Club, b: dict) -> None:
    """Einmal je Monat bei 80 % die Klubleitung informieren."""
    from ..models import Setting
    key = f"billing.budget_warned.{month_start():%Y-%m}"
    if Setting.get(key):
        return
    Setting.set(key, "1")
    db.session.commit()
    try:
        from .mailer import send_mail
        admins = User.query.filter(User.role == "superadmin", User.status == "active").all()
        for u in admins:
            send_mail(u.email, "KI-Budget zu 80 % genutzt", "budget_warning", user=u, budget=b, plan=club_plan(club))
    except Exception:  # pragma: no cover
        log.exception("Budget-Warnung konnte nicht versendet werden")
