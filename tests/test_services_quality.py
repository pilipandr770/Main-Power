"""Regressionstests zu Befunden aus dem Live-Test der Leistungen (andrii-it.de, nis2.store, whatsappbothelfer.de)."""
import json

from app.services import compliance_check, laws, panel, security_check, seo_check
from tests.test_smoke import app  # noqa: F401  (Fixture)

SPA = ('<!doctype html><html lang="de"><head><title>Andrii-IT</title>'
       '<script type="module" crossorigin src="/assets/index-abc.js"></script></head>'
       '<body><div id="root"></div></body></html>')


def test_seo_heading_with_br_and_keyword_variants():
    pg = seo_check._Page()
    pg.feed('<h1>Kundenanfragen werden beantwortet<br><span>automatisch — 24/7</span></h1>')
    assert " ".join(pg.h["h1"][0].split()) == "Kundenanfragen werden beantwortet automatisch — 24/7"
    assert seo_check.kw_pattern("Kundenservice Automatisierung").search("Wir wollen Kundenservice automatisieren")
    assert seo_check.kw_pattern("WhatsApp Bot").search("unsere WhatsApp-Bots")
    assert not seo_check.kw_pattern("NIS-2 Audit").search("NIS2 Compliance")


def test_spa_shell_detection():
    assert seo_check.spa_shell(SPA, 7) == ["/assets/index-abc.js"]
    assert seo_check.spa_shell(SPA, 500) == []                       # genug Text im HTML: normale Seite
    assert seo_check.spa_shell("<html><body><p>Text</p></body></html>", 3) == []


def test_compliance_finds_legal_links_inside_js_bundle(app, monkeypatch):
    class Resp:
        status_code = 200
        encoding = "utf-8"
        headers = {"Content-Type": "text/html"}
        cookies = []

    def fake_get(url, max_bytes=0):
        if url.endswith(".js"):
            return Resp(), b'const r=[{path:"/impressum"},{path:"/datenschutz"}];', 0.1, url
        return Resp(), SPA.encode(), 0.1, url
    monkeypatch.setattr(compliance_check, "_get", fake_get)
    with app.test_request_context():
        res = compliance_check.analyze("https://example.org")
    f = {x["key"]: x for x in res["findings"]}
    assert f["imp"]["status"] == "warn" and "/impressum" in f["imp"]["detail"]
    assert f["ds"]["status"] == "warn" and "JavaScript-App" in f["ds"]["title"]
    assert res["facts"]["js_app"] is True


def test_security_report_demotes_hints_when_there_are_no_weaknesses(monkeypatch):
    monkeypatch.setattr(seo_check, "ai_report", lambda system, payload, result: {
        "zusammenfassung": "Gut.", "keyword_hinweise": "", "naechste_schritte": "", "ai": True,
        "massnahmen": [{"prio": 1, "titel": "CAA-Einträge setzen", "warum": "", "so_gehts": "", "aufwand": "gering",
                        "wirkung": "hoch"}]})
    result = {"score": 100, "facts": {"host": "nis2.store"},
              "findings": [{"key": "caa", "cat": "DNS", "status": "info", "title": "CAA", "detail": "", "fix": ""}]}
    rep = security_check.build_report(result)
    assert rep["massnahmen"][0]["wirkung"] == "niedrig" and rep["massnahmen"][0]["titel"].startswith("Optional:")


def test_laws_dedupe_prefers_full_text_and_excerpt_keeps_context(app, monkeypatch):
    full = {"slug": "ao", "enbez": "§ 147", "law_abbreviation": "AO", "text": "(1) Die folgenden Unterlagen … voll",
            "score": 0.80, "part": None}
    part = dict(full, text="(1) Die folgenden Unterlagen … Teil", score=0.95, part="1/3")
    middle = {"slug": "estg", "enbez": "§ 4", "law_abbreviation": "EStG", "text": "ich tätig … 6b. Arbeitszimmer",
              "score": 0.9, "part": "3/5"}

    class R:
        def raise_for_status(self): pass
        def json(self): return [part, full, middle]
    monkeypatch.setattr(laws.requests, "get", lambda *a, **k: R())
    app.config["LAWS_API_URL"] = "http://laws.test"
    with app.app_context():
        hits = laws.search("Aufbewahrung Rechnungen", expand=False)
    assert [h["law_abbreviation"] for h in hits] == ["EStG", "AO"] and hits[1]["text"].endswith("voll")

    long = ("(1) Absatz eins mit Nummer 4 Buchungsbelege. " * 30 + "(5) Die folgenden Betriebsausgaben dürfen den Gewinn "
            "nicht mindern: " + "x " * 700 + "6b. Aufwendungen für ein häusliches Arbeitszimmer. " + "y " * 800)
    ex = laws.excerpt(long, "Kann ich ein häusliches Arbeitszimmer absetzen?")
    assert ex.startswith("(1) Absatz eins") and "dürfen den Gewinn nicht mindern" in ex and "Arbeitszimmer" in ex


def test_laws_middle_parts_are_quoted_but_not_explained(app, monkeypatch):
    seen = []

    def fake_complete(system, messages, **kw):
        seen.append(json.loads(messages[-1]["content"]))
        return json.dumps({"erlaeuterungen": [{"id": 2, "text": "Erklärung"}], "nicht_passend": [], "hinweis": "ok"})
    monkeypatch.setattr(laws, "complete", fake_complete)
    hits = [{"id": 1, "slug": "estg", "law_abbreviation": "EStG", "enbez": "§ 4", "titel": "", "category": "",
             "text": "6b. Arbeitszimmer", "part": "3/5"},
            {"id": 2, "slug": "estg", "law_abbreviation": "EStG", "enbez": "§ 9", "titel": "", "category": "",
             "text": "(1) Werbungskosten …", "part": ""}]
    with app.app_context():
        ans = laws.explain("Arbeitszimmer absetzen?", hits)
    assert [f["id"] for f in seen[0]["fundstellen"]] == [2]           # Mittelteil geht nicht an das Modell
    assert ans["nur_wortlaut"] == [1] and ans["erlaeuterungen"] == [{"id": 2, "text": "Erklärung"}]


def test_panel_business_personas_have_no_health_segment_and_ab_flag():
    seg = dict(panel.SEGMENTS)["Gesundheit"]
    assert seg({"kind": "b", "health": "gesund"}) is None
    assert seg({"kind": "c", "health": "gesund"}) == "gesund"
    assert panel._cut("Ein recht langer Satz über den Preis von neunundvierzig Euro", 30).endswith(" …")
