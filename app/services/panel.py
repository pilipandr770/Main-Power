"""Markt-Panel: synthetische Marktforschung mit fiktiven KI-Personas.

Ablauf: Personas ziehen (panel_personas) -> jede Persona beantwortet einen Fragebogen (ein Modellaufruf je Persona,
parallel, im Hintergrund) -> Kennzahlen, Segmente, Preiskurve, A/B-Vergleich -> Bericht von Aiko (Themen mit exakt gezählten
Nennungen). ALLE Ergebnisse sind synthetisch: Hypothesen und Formulierungshilfen, kein Ersatz für Befragungen echter Menschen.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

from flask import current_app

from ..extensions import db
from ..models import PanelResponse, PanelRun, Setting, utcnow
from . import panel_personas as pp
from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

UNITS = {"einmalig": "einmalig", "monat": "pro Monat", "jahr": "pro Jahr"}
INTENTS = ("sofort_kaufen", "nach_test", "vielleicht", "nein")
INTENT_LABEL = {"sofort_kaufen": "kauft sofort", "nach_test": "kauft nach Test", "vielleicht": "vielleicht", "nein": "nein"}
STALE_AFTER = timedelta(minutes=12)

SYSTEM = (
    "Du nimmst an einer SYNTHETISCHEN Marktforschung teil und spielst eine FIKTIVE Person mit dem gegebenen Steckbrief. "
    "Bleib strikt in dieser Rolle und antworte so, wie diese Person im Alltag reagieren würde: mit ihrem Budget, ihrer Zeit, "
    "ihrer Lebenslage und in ihren eigenen Worten.\n"
    "Realismus-Regeln (verhindern Zustimmungs-Verzerrung):\n"
    "- Die meisten Menschen kaufen die meisten neuen Produkte NICHT. Ein Nein oder Vielleicht ist normal. „sofort_kaufen“ "
    "nur bei klarem, akutem Bedarf, passendem Budget und Vertrauen.\n"
    "- Berücksichtige Einkommen, Fixkosten, Zeit, Gesundheit, Technikaffinität und Entscheidungsstil der Person.\n"
    "- Sei nicht höflich-positiv. Nenne echte Zweifel (Vertrauen, Preis, Aufwand, Alternativen, Datenschutz).\n"
    "- Nenne Preisgrenzen realistisch nach deinem Budget und unabhängig vom genannten Angebotspreis (kein Anker).\n"
    "- Erfinde keine Fakten über das Produkt; beurteile nur, was beschrieben ist.\n"
    "Antworte NUR als JSON gemäß Schema, ohne weiteren Text."
)


# --------------------------------------------------------------------------- Einstellungen und Kontingent
def limits() -> tuple[int, int]:
    def num(key, default):
        try:
            return max(0, int(Setting.get(key) or default))
        except ValueError:
            return default
    return num("panel_monthly_limit", 3), num("panel_max_personas", 100)


def used_this_month(user_id: int) -> int:
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return PanelRun.query.filter(PanelRun.user_id == user_id, PanelRun.created_at >= start, PanelRun.status != "failed").count()


# --------------------------------------------------------------------------- Persona-Befragung
def _num(v) -> float | None:
    """Zahl aus Modellantwort lesen: 1.299,50 / 12,5 € / 42 / 42.5."""
    t = str(v).replace("€", "").replace("EUR", "").replace(" ", "").strip()
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def parse_answers(raw: str) -> dict | None:
    try:
        d = json.loads(raw[raw.find("{"):raw.rfind("}") + 1], strict=False)
    except ValueError:
        return None
    intent = str(d.get("kaufabsicht", "")).strip().lower().replace(" ", "_")
    prob, rel = _num(d.get("kaufwahrscheinlichkeit")), _num(d.get("relevanz"))
    if intent not in INTENTS or prob is None:
        return None
    wtp, cheap = _num(d.get("max_preis")), _num(d.get("zu_billig_preis"))
    return {"kaufabsicht": intent, "kaufwahrscheinlichkeit": max(0.0, min(100.0, prob)),
            "relevanz": max(1.0, min(10.0, rel)) if rel is not None else None,
            "max_preis": max(0.0, wtp) if wtp is not None and wtp < 1_000_000 else None,
            "zu_billig_preis": max(0.0, cheap) if cheap is not None and cheap < 1_000_000 else None,
            "hauptgrund": str(d.get("hauptgrund", ""))[:300], "hauptbedenken": str(d.get("hauptbedenken", ""))[:300],
            "hauptnutzen": str(d.get("hauptnutzen", ""))[:300], "aenderungswunsch": str(d.get("aenderungswunsch", ""))[:300]}


def ask_persona(persona: pp.Persona, run: dict, price: float | None, usage: dict, model: str | None) -> dict | None:
    unit = UNITS.get(run["unit"], "einmalig")
    price_line = (f"Angebotspreis: {price:.2f} € ({unit})".replace(".", ",") if price else
                  "Preis: noch offen — nenne deine eigene Preisvorstellung.")
    user = (f"# Dein Steckbrief (fiktive Person)\n{persona.sheet()}\n\n# Produkt\nName: {run['product_name']}\n"
            f"Beschreibung: {run['description'][:1500]}\n{price_line}\n\n# Fragebogen\nAntworte als JSON mit genau diesen Schlüsseln:\n"
            '{"kaufabsicht": "sofort_kaufen|nach_test|vielleicht|nein", "kaufwahrscheinlichkeit": <Zahl 0-100, ehrliche '
            'Wahrscheinlichkeit, dass du es innerhalb von 3 Monaten kaufst>, "relevanz": <Zahl 1-10>, "max_preis": <höchster '
            f'Preis in Euro ({unit}), den du zahlen würdest; 0 wenn nie>, "zu_billig_preis": <Preis in Euro, unter dem du an der '
            'Qualität zweifeln würdest; 0 wenn nicht relevant>, "hauptgrund": "1–2 Sätze in deinen Worten, warum du kaufst oder '
            'nicht kaufst", "hauptbedenken": "1 Satz", "hauptnutzen": "1 Satz, leer wenn kein Nutzen", "aenderungswunsch": '
            '"1 Satz: was müsste sich ändern, damit du kaufst"}\nIn Textwerten keine doppelten Anführungszeichen.')
    for _ in (1, 2):
        try:
            raw = complete(SYSTEM, [{"role": "user", "content": user}], max_tokens=500, purpose="market_panel",
                           usage=usage, model=model)
        except LLMUnavailable:
            return None
        ans = parse_answers(raw)
        if ans:
            return ans
    return None


# --------------------------------------------------------------------------- Auswertung
def _share(rows, pred) -> float:
    return round(100 * sum(1 for r in rows if pred(r)) / len(rows), 1) if rows else 0.0


def _stats(rows: list[dict]) -> dict:
    a = [r["a"] for r in rows]
    wtp = [x["max_preis"] for x in a if x.get("max_preis")]
    return {"n": len(rows), "intent": _share(a, lambda x: x["kaufabsicht"] in ("sofort_kaufen", "nach_test")),
            "prob": round(statistics.mean(x["kaufwahrscheinlichkeit"] for x in a), 1) if a else 0.0,
            "relevanz": round(statistics.mean([x["relevanz"] for x in a if x.get("relevanz")] or [0]), 1),
            "wtp": round(statistics.median(wtp), 2) if wtp else None}


def _nice(p: float) -> float:
    if p < 10:
        return round(p * 2) / 2
    if p < 100:
        return float(round(p))
    if p < 1000:
        return float(round(p / 5) * 5)
    return float(round(p / 50) * 50)


def price_curve(rows: list[dict], extra: list[float]) -> dict:
    """Nachfragekurve: erwarteter Anteil zahlender Kund:innen je Preis = Mittel der Kaufwahrscheinlichkeiten aller Personas,
    deren Preisgrenze den Preis erreicht. Umsatzindex = Preis mal Anteil."""
    wtp = sorted(r["a"]["max_preis"] for r in rows if r["a"].get("max_preis"))
    if len(wtp) < 8:
        return {"points": [], "best": None}
    q = lambda f: wtp[min(len(wtp) - 1, int(f * len(wtp)))]  # noqa: E731
    grid = sorted({_nice(q(f / 10)) for f in range(1, 10)} | {_nice(x) for x in extra if x})
    pts = []
    for p in [g for g in grid if g > 0]:
        share = round(100 * sum(r["a"]["kaufwahrscheinlichkeit"] / 100 for r in rows if (r["a"].get("max_preis") or 0) >= p) / len(rows), 1)
        pts.append({"price": p, "share": share, "index": round(p * share, 1)})
    best = max(pts, key=lambda x: x["index"]) if pts and max(x["index"] for x in pts) > 0 else None
    return {"points": pts, "best": best}


def _ztest(a: list[dict], b: list[dict]) -> float | None:
    """Zwei-Stichproben-Test für Anteile (Kaufabsicht ja/nein); liefert p-Wert (zweiseitig) oder None."""
    ya = sum(1 for r in a if r["a"]["kaufabsicht"] in ("sofort_kaufen", "nach_test"))
    yb = sum(1 for r in b if r["a"]["kaufabsicht"] in ("sofort_kaufen", "nach_test"))
    na, nb = len(a), len(b)
    if na < 10 or nb < 10:
        return None
    p = (ya + yb) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    if se == 0:
        return 1.0
    z = abs(ya / na - yb / nb) / se
    return round(math.erfc(z / math.sqrt(2)), 3)


SEGMENTS = [
    ("Zielgruppe", lambda p: "Privatpersonen" if p["kind"] == "c" else "Geschäftsentscheider"),
    ("Alter", lambda p: p["age_group"]),
    ("Einkommen (Privat)", lambda p: p["income_class"] if p["kind"] == "c" else None),
    ("Technikaffinität", lambda p: "hoch (4–5)" if p["tech"] >= 4 else "mittel (3)" if p["tech"] == 3 else "niedrig (1–2)"),
    ("Preisbewusstsein", lambda p: "hoch (4–5)" if p["price_sens"] >= 4 else "mittel (3)" if p["price_sens"] == 3 else "niedrig (1–2)"),
    ("Wohnort", lambda p: p["ortstyp"]),
    ("Gesundheit", lambda p: "gesund" if p["health"] == "gesund" else "mit Erkrankung/Einschränkung"),
]


def aggregate(rows: list[dict], run: dict, n_requested: int) -> dict:
    valid = [r for r in rows if r["a"]]
    res: dict = {"n_valid": len(valid), "n_requested": n_requested}
    if not valid:
        return res
    res["overall"] = _stats(valid)
    core = [r for r in valid if (r["a"].get("relevanz") or 0) >= 6]
    if len(core) >= 5:  # Kernzielgruppe: Personas, die sich vom Produkt angesprochen fühlen
        res["core"] = _stats(core)
    res["intents"] = {INTENT_LABEL[k]: sum(1 for r in valid if r["a"]["kaufabsicht"] == k) for k in INTENTS}
    res["prob_hist"] = {f"{lo}–{lo + 19}": sum(1 for r in valid if lo <= r["a"]["kaufwahrscheinlichkeit"] < lo + 20 + (1 if lo == 80 else 0))
                        for lo in range(0, 100, 20)}
    res["curve"] = price_curve(valid, [run.get("price_a") or 0, run.get("price_b") or 0])
    segs = []
    for name, fn in SEGMENTS:
        groups: dict[str, list] = {}
        for r in valid:
            k = fn(r["p"])
            if k:
                groups.setdefault(k, []).append(r)
        rowsd = [{"label": k, **_stats(v)} for k, v in groups.items() if len(v) >= 5]
        if len(rowsd) >= 2:
            segs.append({"dimension": name, "rows": sorted(rowsd, key=lambda x: -x["intent"])})
    res["segments"] = segs
    ranked = sorted((s | {"dimension": d["dimension"]} for d in segs for s in d["rows"] if s["n"] >= 8), key=lambda x: -x["intent"])
    res["best_segments"], res["worst_segments"] = ranked[:3], ranked[::-1][:3]
    if run.get("price_b"):
        A = [r for r in valid if r["v"] == "A"]
        B = [r for r in valid if r["v"] == "B"]
        res["ab"] = {"price_a": run["price_a"], "price_b": run["price_b"], "a": _stats(A), "b": _stats(B), "p_value": _ztest(A, B)}
    by_prob = sorted(valid, key=lambda r: -r["a"]["kaufwahrscheinlichkeit"])

    def ex(r):
        return {"name": r["p"]["name"], "label": r["p"]["label"], "age": r["p"]["age"], "prob": r["a"]["kaufwahrscheinlichkeit"],
                "text": r["a"]["hauptgrund"]}
    res["examples_yes"] = [ex(r) for r in by_prob[:4]]
    res["examples_no"] = [ex(r) for r in by_prob[::-1][:4]]
    return res


# --------------------------------------------------------------------------- Bericht
def _theme_block(rows: list[dict], key: str) -> tuple[list[str], list[int]]:
    items = [(i, r["a"].get(key, "").strip()) for i, r in enumerate(rows)]
    items = [(i, t) for i, t in items if t]
    return [t[:140] for _, t in items], [i for i, _ in items]


def _theme_fallback(texts: list[str]) -> list[dict]:
    return [{"thema": t[:80], "anzahl": 1, "beispiel": t} for t in texts[:5]]


def synthesize(run: dict, res: dict, rows: list[dict], usage: dict, model: str | None) -> dict:
    valid = [r for r in rows if r["a"]]
    con_t, _ = _theme_block(valid, "hauptbedenken")
    pro_t, _ = _theme_block(valid, "hauptnutzen")
    chg_t, _ = _theme_block(valid, "aenderungswunsch")
    numbered = lambda ts: [f"{i + 1}: {t}" for i, t in enumerate(ts)]  # noqa: E731
    ov = res.get("overall", {})
    fallback = {
        "zusammenfassung": (f"{res['n_valid']} von {res['n_requested']} fiktiven Personas haben geantwortet. "
                            f"{ov.get('intent', 0)} % zeigen Kaufabsicht (sofort oder nach Test), im Mittel liegt die "
                            f"Kaufwahrscheinlichkeit bei {ov.get('prob', 0)} %."),
        "bedenken": _theme_fallback(con_t), "nutzen": _theme_fallback(pro_t), "aenderungen": _theme_fallback(chg_t),
        "empfehlungen": [], "zielgruppen_fazit": "", "preis_fazit": "", "verzerrung_hinweis": "", "ai": False}
    system = ("Du bist {ASSISTANT}, Analystin von {CLUB}, und wertest ein SYNTHETISCHES Markt-Panel aus (fiktive KI-Personas, "
              "keine echten Menschen). Schreibe für Unternehmer:innen: ruhig, klar, ehrlich, Deutsch, du-Form. Nutze NUR die gelieferten "
              "Zahlen und Aussagen. Behaupte nie, dass das Ergebnis die Meinung realer Gruppen abbildet oder Umsätze vorhersagt; sprich "
              "von Hinweisen und Hypothesen, die mit echten Kund:innen zu prüfen sind. Fasse die Aussagen zu 3–6 Themen je Liste zusammen "
              "und gib zu jedem Thema die Nummern der Aussagen an, die dazu gehören (jede Nummer höchstens einem Thema). "
              "Antworte NUR als JSON: {\"zusammenfassung\": \"4–6 Sätze\", \"bedenken\": [{\"thema\": \"…\", \"ids\": [1,2]}], "
              "\"nutzen\": [{\"thema\": \"…\", \"ids\": []}], \"aenderungen\": [{\"thema\": \"…\", \"ids\": []}], \"empfehlungen\": "
              "[{\"titel\": \"…\", \"warum\": \"1–2 Sätze\", \"so_gehts\": \"1–2 Sätze\"}] (4–5), \"zielgruppen_fazit\": \"2–3 Sätze zu "
              "den stärksten und schwächsten Segmenten\", \"preis_fazit\": \"2 Sätze, bei fehlendem Preis oder Kurve Hinweis, dass die "
              "Datenbasis dünn ist\", \"verzerrung_hinweis\": \"2 Sätze: welche Verzerrungen bei diesem Lauf besonders zu beachten "
              "sind\"}. WICHTIG: In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine Zeilenumbrüche. "
              "Fasse dich kurz: jeder Textwert höchstens zwei Sätze.")
    payload = {"produkt": {"name": run["product_name"], "beschreibung": run["description"][:900], "preis_a": run.get("price_a"),
                           "preis_b": run.get("price_b"), "einheit": UNITS.get(run["unit"], "")},
               "kennzahlen": {k: res.get(k) for k in ("overall", "intents", "curve", "best_segments", "worst_segments", "ab")},
               "aussagen_bedenken": numbered(con_t), "aussagen_nutzen": numbered(pro_t), "aussagen_aenderungswuensche": numbered(chg_t)}
    data = None
    for attempt in (1, 2):
        try:
            raw = complete(system + (" Deine letzte Antwort war kein gültiges JSON. Antworte kompakter." if attempt == 2 else ""),
                           [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], max_tokens=3200,
                           purpose="market_panel", usage=usage, model=model)
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1], strict=False)
            break
        except LLMUnavailable:
            return fallback
        except ValueError as exc:
            log.warning("Panel-Bericht: ungültiges JSON (Versuch %s): %s", attempt, exc)
    if data is None:
        return fallback

    def themes(key, texts):
        out = []
        for t in data.get(key, [])[:8]:
            ids = [int(i) for i in t.get("ids", []) if str(i).isdigit() and 1 <= int(i) <= len(texts)]
            if t.get("thema") and ids:
                out.append({"thema": str(t["thema"])[:120], "anzahl": len(set(ids)), "beispiel": texts[ids[0] - 1]})
        return sorted(out, key=lambda x: -x["anzahl"]) or _theme_fallback(texts)

    return {"zusammenfassung": str(data.get("zusammenfassung", ""))[:1500] or fallback["zusammenfassung"],
            "bedenken": themes("bedenken", con_t), "nutzen": themes("nutzen", pro_t), "aenderungen": themes("aenderungen", chg_t),
            "empfehlungen": [{"titel": str(e.get("titel", ""))[:140], "warum": str(e.get("warum", ""))[:400],
                              "so_gehts": str(e.get("so_gehts", ""))[:400]} for e in data.get("empfehlungen", [])[:6] if e.get("titel")],
            "zielgruppen_fazit": str(data.get("zielgruppen_fazit", ""))[:600], "preis_fazit": str(data.get("preis_fazit", ""))[:500],
            "verzerrung_hinweis": str(data.get("verzerrung_hinweis", ""))[:500], "ai": True}


# --------------------------------------------------------------------------- Ausführung
def refresh_status(run: PanelRun) -> PanelRun:
    """Hängende Läufe (Neustart des Servers) nach einer Frist als fehlgeschlagen markieren."""
    if run.status in ("queued", "running") and utcnow() - (run.updated_at or run.created_at) > STALE_AFTER:
        run.status, run.error = "failed", "Der Lauf wurde unterbrochen (Neustart oder Zeitüberschreitung). Bitte erneut starten."
        db.session.commit()
    return run


def start_run(user, form: dict, background: bool = True) -> PanelRun:
    """Legt den Lauf an und startet die Befragung im Hintergrund (in Tests synchron)."""
    run = PanelRun(user_id=user.id, product_name=form["product_name"], description=form["description"], audience=form["audience"],
                   regional=form["regional"], unit=form["unit"], price_a=form.get("price_a"), price_b=form.get("price_b"),
                   n_requested=form["n"], seed=int(utcnow().timestamp()) % 1_000_000, status="queued",
                   model=current_app.config["ANTHROPIC_MODEL"])
    db.session.add(run)
    db.session.commit()
    app = current_app._get_current_object()
    if background and not app.config.get("TESTING"):
        threading.Thread(target=run_worker, args=(app, run.id, run.club_id), daemon=True, name=f"panel-{run.id}").start()
    else:
        run_worker(app, run.id, run.club_id)
    return run


def _enter_club(club_id: int) -> None:
    """Hintergrund-Threads haben keinen Request: Klub des Laufs explizit setzen (Mandantenfilter, Token-Zuordnung)."""
    from ..models import Club
    from ..tenancy import set_club
    set_club(db.session.get(Club, club_id))


def run_worker(app, run_id: int, club_id: int) -> None:
    with app.app_context():
        _enter_club(club_id)
        run = db.session.get(PanelRun, run_id)
        try:
            run.status = "running"
            db.session.commit()
            ctx = {"product_name": run.product_name, "description": run.description, "unit": run.unit,
                   "price_a": run.price_a, "price_b": run.price_b}
            personas = pp.sample(run.n_requested, run.audience, run.regional, run.seed)
            usage: dict = {"in": 0, "out": 0}
            model = run.model or None
            rows: list[dict] = []

            price_a, price_b = run.price_a, run.price_b  # Werte vorab lesen: ORM-Objekte nie in Threads anfassen

            def task(p: pp.Persona):
                variant = "B" if (price_b and p.idx % 2 == 1) else "A"
                price = price_b if variant == "B" else price_a
                with app.app_context():
                    _enter_club(club_id)
                    return p, variant, ask_persona(p, ctx, price, usage, model)

            # Tests (SQLite im Speicher, eine gemeinsame Verbindung): nacheinander, sonst parallel
            workers = 1 if app.config.get("TESTING") else 8
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(task, p) for p in personas] if workers > 1 else []
                if workers == 1:
                    from concurrent.futures import Future
                    for p in personas:
                        fut = Future()
                        fut.set_result(task(p))
                        futures.append(fut)
                for fut in (as_completed(futures) if workers > 1 else futures):
                    try:
                        p, variant, ans = fut.result()
                    except Exception:  # pragma: no cover
                        log.exception("Panel-Persona fehlgeschlagen")
                        run.n_failed += 1
                        db.session.commit()
                        continue
                    db.session.add(PanelResponse(run_id=run.id, idx=p.idx, variant=variant, persona=p.public(), answers=ans, ok=bool(ans)))
                    run.n_done += 1
                    if not ans:
                        run.n_failed += 1
                    db.session.commit()
                    rows.append({"p": p.public(), "v": variant, "a": ans})
            rows.sort(key=lambda r: r["p"]["idx"])
            spec = {"price_a": price_a, "price_b": price_b}
            res = aggregate(rows, spec, run.n_requested)
            if not res.get("n_valid"):
                raise RuntimeError("Keine Persona hat verwertbar geantwortet (KI nicht erreichbar?).")
            run.result = res
            run.report = synthesize({"product_name": run.product_name, "description": run.description, "unit": run.unit, **spec},
                                    res, rows, usage, model)
            run.ai = run.report.get("ai", False)
            run.tokens_in, run.tokens_out = usage["in"], usage["out"]
            run.status, run.finished_at = "done", utcnow()
            db.session.commit()
        except Exception as exc:
            log.exception("Markt-Panel %s fehlgeschlagen", run_id)
            db.session.rollback()
            _enter_club(club_id)
            run = db.session.get(PanelRun, run_id)
            run.status, run.error = "failed", str(exc)[:290]
            db.session.commit()


def cost_usd(run: PanelRun) -> float:
    c = current_app.config
    return ((run.tokens_in or 0) * c["LLM_PRICE_IN"] + (run.tokens_out or 0) * c["LLM_PRICE_OUT"]) / 1_000_000
