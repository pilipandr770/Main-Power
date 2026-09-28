"""Auswertung für die Klubleitung: Wer sucht was, wer bietet was, wo sind Lücken – und welche Termine helfen.

Nur Summen, keine Namen. Private Angaben (Ziel- und Meilenstein-Texte, Herausforderung, „Schon versucht“,
Ausschlüsse) fließen NICHT ein; ausgewertet werden Auswahlfelder und die im Profil sichtbaren Texte
(„Sucht“, „Kann helfen“, sichtbare Klubfragen). Die KI-Themenanalyse bekommt die Texte anonymisiert und gemischt.
"""
from __future__ import annotations

import json
import logging
import random
import re
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

from ..extensions import db
from ..models import FORMATS, GoalCheckin, Profile, Setting, User, utcnow
from .. import questionnaire as qn
from .insights import _json
from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)
REPORT_KEY = "insights.club_report"

# Terminideen, wenn ein Partnertyp deutlich häufiger gesucht als angeboten wird
GAP_IDEAS = {
    "investor": ("Investor:innen-Abend", "Business Angels und Förderbanken einladen, Gründer:innen pitchen kurz."),
    "mentor": ("Sparring-Runde", "Erfahrene Unternehmer:innen beraten in kleinen Tischen jüngere Mitglieder."),
    "kunden": ("Einkäufer:innen-Frühstück", "Mitglieder mit Aufträgen stellen vor, was sie suchen."),
    "kooperation": ("Partner-Speeddating", "Kurze Runden zu Vertriebs- und Kooperationspartnerschaften."),
    "dienstleister": ("Expert:innen-Markt", "Dienstleister:innen aus dem Klub stellen sich in 3 Minuten vor."),
    "mitgruender": ("Co-Founder-Matching", "Gründer:innen und Interessierte mit Umsetzungserfahrung zusammenbringen."),
    "team": ("Freelancer-Abend", "Selbständige und Unternehmen mit Projektbedarf treffen sich."),
    "experte": ("Themen-Frühstück mit Expert:in", "Eine konkrete Frage, eine Expert:in, offene Runde."),
    "startups": ("Start-up-Pitch-Abend", "Junge Unternehmen stellen sich Investor:innen und Mentor:innen vor."),
    "peers": ("Runde nach Phase", "Tische nach Unternehmensphase – Austausch unter Gleichen."),
}
STOP = set("""und oder der die das ein eine einen einem einer für mit von zu zum zur im in am an auf aus bei nach
ich wir mich mir uns unser unsere meine mein sich sie er es ist sind bin hat haben suche suchen gerade
auch als wie was wer die den dem des nicht kein keine sowie bzw etc z b gern gerne neue neuen neuer""".split())


def _members() -> list[Profile]:
    return Profile.query.join(User).filter(User.status == "active").all()


def _count(profiles, field: str) -> list[dict]:
    options = qn.CHOICE_FIELDS[field][0]
    c = Counter(k for p in profiles for k in qn.split(getattr(p, field)) if k in options)
    return [{"key": k, "label": options[k], "n": c.get(k, 0)} for k in options if c.get(k)]


def _supply(profiles, ptype: str) -> int:
    neutral = SimpleNamespace(stage="", partner_types="", resources="", role="", help_mode="")
    return sum(1 for b in profiles if qn.PARTNER_FIT[ptype](neutral, b) >= 1.0)


def overview() -> dict:
    ps = _members()
    n = len(ps)
    filled = sum(1 for p in ps if p.role or p.partner_types or p.goal_category)
    demand = Counter(k for p in ps for k in qn.split(p.partner_types) if k in qn.PARTNER_TYPES)
    market = []
    for key, lbl in qn.PARTNER_TYPES.items():
        d = demand.get(key, 0)
        if key == "peers":
            s = d  # Gleichgesinnte suchen sich gegenseitig
        else:
            s = _supply(ps, key)
        if d or s:
            market.append({"key": key, "label": lbl, "demand": d, "supply": s, "gap": d - s})
    market.sort(key=lambda r: (-r["gap"], -r["demand"]))
    top = max([max(r["demand"], r["supply"]) for r in market] + [1])
    for r in market:
        r["demand_pct"], r["supply_pct"] = round(100 * r["demand"] / top), round(100 * r["supply"] / top)
    since = utcnow() - timedelta(days=180)
    checkins = Counter(c.status for c in GoalCheckin.query.filter(GoalCheckin.created_at >= since))
    custom = []
    for q in qn.club_questions():
        if q.kind == "text":
            continue
        def values(p, key=q.key):
            v = (p.custom_answers or {}).get(key)
            return v if isinstance(v, list) else ([v] if v else [])
        c = Counter(v for p in ps for v in values(p))
        custom.append({"label": q.label, "rows": [{"label": o, "n": c.get(o, 0)} for o in q.option_list if c.get(o)]})
    return {
        "members": n, "filled": filled, "filled_pct": round(100 * filled / n) if n else 0,
        "with_goal": sum(1 for p in ps if p.goal_12m or p.goal_category),
        "with_orders": sum(1 for p in ps if "auftraege" in qn.split(p.resources)),
        "free_help": sum(1 for p in ps if p.help_mode in ("kostenlos", "mentoring")),
        "market": market,
        "gaps": [r for r in market if r["gap"] > 0][:4],
        "goals": _count(ps, "goal_category"), "stages": _count(ps, "stage"), "roles": _count(ps, "role"),
        "languages": _count(ps, "languages"), "resources": _count(ps, "resources"),
        "meeting": _count(ps, "meeting_mode"), "time": _count(ps, "network_time"),
        "checkins": [{"label": GoalCheckin.STATUS[k], "n": checkins.get(k, 0)} for k in GoalCheckin.STATUS
                     if checkins.get(k)],
        "checkins_total": sum(checkins.values()),
        "custom": custom,
    }


# --------------------------------------------------------------------------- Themen (KI) mit Cache
def _texts(ps) -> tuple[list[str], list[str]]:
    need, offer = [], []
    public_q = [q for q in qn.club_questions() if q.public and q.kind == "text" and q.use in ("need", "offer")]
    for p in ps:
        if p.q_looking_for:
            need.append(p.q_looking_for)
        for t in (p.q_can_help, p.asked_for):
            if t:
                offer.append(t)
        for q in public_q:
            v = (p.custom_answers or {}).get(q.key)
            if v:
                (need if q.use == "need" else offer).append(str(v))
    rnd = random.Random(len(need) * 31 + len(offer))  # gemischt, damit keine Zuordnung zur Mitgliederliste möglich
    rnd.shuffle(need)
    rnd.shuffle(offer)
    return [t[:300] for t in need[:150]], [t[:300] for t in offer[:150]]


def _keywords(texts: list[str], k: int = 8) -> list[dict]:
    c = Counter(w for t in texts for w in set(re.findall(r"[a-zäöüß][a-zäöüß-]{3,}", t.lower())) if w not in STOP)
    return [{"thema": w.capitalize(), "anzahl": n} for w, n in c.most_common(k) if n >= 2]


def _fallback_report(ov: dict, need: list[str], offer: list[str]) -> dict:
    ideas = []
    for g in ov["gaps"][:3]:
        title, why = GAP_IDEAS.get(g["key"], (g["label"], ""))
        ideas.append({"titel": title, "format": "", "warum": f"{g['demand']} Mitglieder suchen „{g['label']}“, "
                                                               f"nur {g['supply']} bieten es an. {why}"})
    return {"nachfrage": _keywords(need), "angebot": _keywords(offer), "luecken": [], "ideen": ideas, "ai": False}


def build_report() -> dict:
    ps = _members()
    ov = overview()
    need, offer = _texts(ps)
    report = _fallback_report(ov, need, offer)
    if need or offer:
        system = ("Du analysierst für die Leitung von {CLUB} anonymisierte Profiltexte der Mitglieder. Ziel: Programm "
                  "und Einladungen besser planen. Fasse ähnliche Anliegen zu Themen zusammen und zähle grob, wie viele "
                  "Texte dazu passen. Nenne keine Personen, zitiere keine Texte wörtlich, erfinde nichts. "
                  "Lücken = Themen mit viel Nachfrage und wenig Angebot. Ideen = 3 konkrete Termine im Klub, jeweils "
                  "mit einem der Formate. Antworte NUR als JSON: {\"nachfrage\": [{\"thema\": \"…\", \"anzahl\": n}], "
                  "\"angebot\": [...], \"luecken\": [{\"thema\": \"…\", \"hinweis\": \"1 Satz\"}], "
                  "\"ideen\": [{\"titel\": \"…\", \"format\": \"<key>\", \"warum\": \"1–2 Sätze\"}]}. "
                  "Höchstens 8 Themen je Liste, Deutsch.")
        payload = {"mitglieder": ov["members"], "nachfrage_texte": need, "angebot_texte": offer,
                   "partnersuche": [{"typ": r["label"], "gesucht": r["demand"], "angeboten": r["supply"]}
                                    for r in ov["market"]],
                   "formate": {k: f["name"] for k, f in FORMATS.items()}}
        try:
            data = _json(complete(system, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                                  max_tokens=1400, purpose="club_insights"))
            report = {"nachfrage": data.get("nachfrage", [])[:8], "angebot": data.get("angebot", [])[:8],
                      "luecken": data.get("luecken", [])[:6], "ideen": data.get("ideen", [])[:4], "ai": True}
        except (LLMUnavailable, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.info("Klub-Auswertung ohne KI (%s)", exc)
    report["created_at"] = utcnow().isoformat(timespec="minutes")
    report["basis"] = {"mitglieder": ov["members"], "texte": len(need) + len(offer)}
    Setting.set(REPORT_KEY, json.dumps(report, ensure_ascii=False))
    db.session.commit()
    return report


def cached_report() -> dict | None:
    raw = Setting.get(REPORT_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None
