"""Gesetzes-Suche: zitiert Fundstellen aus dem deutschen Bundesrecht und ordnet sie sprachlich ein.

Nutzt intern ein separates Projekt (laws_pipeline: Qdrant + der komplette Korpus von gesetze-im-internet.de,
semantisch durchsuchbar über eine kleine FastAPI unter LAWS_API_URL — nur im internen Docker-Netzwerk erreichbar,
kein öffentlicher Endpunkt). Main Power ruft daraus nur GET /search auf.

Kein Ersatz für Rechtsberatung: Der zitierte Gesetzestext kommt unverändert aus der Datenbank (kein KI-Text); die
KI-Einordnung erklärt AUSSCHLIESSLICH, was im Text steht — nie, was jemand tun sollte oder ob er im Recht ist.
Die vorrangigen KI-VO-Regeln (Art. 50: KI stellt sich vor, keine Täuschung) stehen in llm.AI_ACT_RULES und werden
jedem Aufruf hier automatisch vorangestellt.
"""
from __future__ import annotations

import json
import logging
import re

import requests
from flask import current_app

from .llm import LLMUnavailable, complete

log = logging.getLogger(__name__)

TIMEOUT = 12
MAX_HITS = 8

# Rechtsgebiete aus dem kuratierten Katalog des Korpus (laws_pipeline/law_catalog.py). Nicht abschließend: alles
# andere aus dem Bundesrecht ist als "Unclassified" mit erfasst und wird trotzdem gefunden, nur ohne Filter-Label.
CATEGORIES = [
    "Verfassungsrecht", "Zivilrecht", "Handelsrecht", "Gesellschaftsrecht", "Insolvenzrecht", "Verfahrensrecht",
    "Anwaltsrecht", "Wettbewerbsrecht", "Notarrecht", "Strafrecht", "Strafverfahrensrecht", "Wirtschaftsstrafrecht",
    "IT-/Digitalrecht", "Datenschutzrecht", "IT-Sicherheitsrecht", "Urheberrecht", "Wirtschaftsrecht",
    "Finanzaufsichtsrecht", "Steuerrecht", "Steuerverfahrensrecht",
]


class LawsUnavailable(Exception):
    """Nutzerverständliche Fehlermeldung (Deutsch): Suche derzeit nicht erreichbar."""


def enabled() -> bool:
    return bool(current_app.config.get("LAWS_API_URL"))


def expand_query(query: str) -> list[str]:
    """Bis zu zwei zusätzliche Suchformulierungen in Fachsprache (nur für die Suche; zitiert wird weiter wörtlich).

    Alltagsfragen („Wie lange muss ich Rechnungen aufheben?“) treffen den Gesetzeswortlaut („Aufbewahrungsfristen,
    Buchungsbelege, Abgabenordnung“) sonst schlecht. Ohne KI: keine Erweiterung.
    """
    from .insights import json_call
    system = ("Formuliere eine Frage zum deutschen Bundesrecht in bis zu zwei kurze Suchanfragen in der Fachsprache der "
              "Gesetze um (Fachbegriffe, wahrscheinlich einschlägige Gesetze als Name oder Abkürzung). Keine Antwort auf "
              "die Frage, keine Bewertung. Antworte NUR als JSON: {\"suchanfragen\": [\"…\", \"…\"]}.")
    try:
        data = json_call(complete, system, [{"role": "user", "content": query[:800]}], max_tokens=200,
                         purpose="law_search")
    except (LLMUnavailable, ValueError, KeyError, TypeError):
        return []
    items = data.get("suchanfragen", []) if isinstance(data, dict) else []
    return [str(q).strip()[:300] for q in items if str(q).strip() and str(q).strip() != query][:2]


def search(query: str, category: str = "", k: int = 6, expand: bool = True) -> list[dict]:
    """Fragt die interne Suche ab; jeder Treffer trägt den Original-Gesetzestext (verbatim, keine KI)."""
    url = current_app.config.get("LAWS_API_URL")
    if not url:
        raise LawsUnavailable("Die Gesetzes-Suche ist auf dieser Installation nicht eingerichtet.")
    query = query.strip()[:800]
    if not query:
        raise LawsUnavailable("Bitte gib eine Frage oder ein Stichwort ein.")
    k = max(1, min(MAX_HITS, k))
    queries = [query] + (expand_query(query) if expand else [])
    data: list = []
    try:
        for q in queries:
            params = {"q": q, "k": min(16, k * 2)}
            if category:
                params["category"] = category
            resp = requests.get(f"{url}/search", params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            part = resp.json()
            if not isinstance(part, list):
                raise ValueError("keine Liste")
            data += part
    except requests.RequestException as exc:
        log.warning("laws-api nicht erreichbar: %s", exc)
        raise LawsUnavailable("Die Gesetzes-Suche ist gerade nicht erreichbar. Bitte versuch es in ein paar Minuten "
                              "erneut.") from exc
    except ValueError as exc:
        raise LawsUnavailable("Die Gesetzes-Suche hat eine unerwartete Antwort geliefert.") from exc
    # Gleiche Vorschrift nur einmal (auch aus mehreren Suchformulierungen). Der Korpus enthält lange Vorschriften
    # vollständig UND in Teilen („2/5“): die vollständige Fassung hat Vorrang, sonst der beste Teil.
    best: dict[tuple, dict] = {}
    for h in data:
        if not isinstance(h, dict) or not h.get("text"):
            continue
        key = (h.get("slug"), h.get("enbez"))
        cur = best.get(key)
        full, cur_full = not h.get("part"), bool(cur) and not cur.get("part")
        if cur is None or (full and not cur_full) or (full == cur_full and float(h.get("score", 0)) >
                                                           float(cur.get("score", 0))):
            best[key] = h
    ranked = sorted(best.values(), key=lambda h: -float(h.get("score", 0)))[:k]
    hits = []
    for i, h in enumerate(ranked):
        hits.append({"id": i + 1, "slug": str(h.get("slug", ""))[:80],
                     "law_abbreviation": str(h.get("law_abbreviation", ""))[:120],
                     "law_title": str(h.get("law_title", ""))[:300], "category": str(h.get("category", ""))[:80],
                     "enbez": str(h.get("enbez", ""))[:60], "titel": str(h.get("titel", ""))[:300],
                     "text": str(h.get("text", ""))[:6000], "stand": h.get("stand") or [],
                     "source_url": str(h.get("source_url", ""))[:300], "score": round(float(h.get("score", 0)), 3),
                     "part": str(h.get("part") or "")[:10]})
    return hits


SYSTEM = (
    "Du hilfst bei der ERSTEN ORIENTIERUNG im deutschen Bundesrecht, indem du ausschließlich vorhandene Gesetzestexte "
    "einordnest. Das ist KEINE Rechtsberatung und du gibst KEINE Handlungsempfehlung — auch nicht indirekt oder "
    "vorsichtig formuliert (kein 'du solltest', 'ich empfehle', 'am besten machst du', 'das bedeutet für dich'). Du "
    "bewertest nie, ob jemand im Recht ist, und wendest den Text nie auf eine persönliche Situation an — du erklärst "
    "ausschließlich, was im zitierten Gesetzestext wörtlich geregelt ist.\n"
    "Unten stehen nummerierte Fundstellen aus einer semantischen Suche über den kompletten deutschen Bundesgesetzkorpus. "
    "Der Wortlaut wird der Person bereits unverändert angezeigt, du musst ihn nicht wiederholen.\n"
    "Für jede Fundstelle, die WIRKLICH zur Frage passt: schreibe 1–2 sachliche Sätze, was der Text bedeutet — rein "
    "sprachliche Erläuterung, keine Einzelfall-Einschätzung. Fundstellen, die nicht wirklich passen (nur oberflächlich "
    "ähnliche Wörter, anderes Thema), listest du in 'nicht_passend' und lässt sie sonst weg — erfinde keine Verbindung. "
    "Wenn KEINE Fundstelle wirklich passt, sag das ehrlich in 'hinweis', statt zu raten oder aus allgemeinem Wissen zu "
    "antworten. Nenne auch im Hinweis KEINE Gesetze oder Paragrafen, die nicht unter den Fundstellen stehen; sag dann nur, "
    "dass die einschlägige Vorschrift nicht dabei ist und eine andere Formulierung helfen kann. Lange Vorschriften "
    "werden als Ausschnitt geliefert (Anfang … Einleitungssatz des Absatzes … Fundstelle). Gib Verneinungen, "
    "Ausnahmen und Rückausnahmen exakt wieder (z. B. ‚dürfen den Gewinn nicht mindern – dies gilt nicht, wenn …‘). "
    "Löse Verweise wie ‚Absatz 1 Nummer 4‘ nur auf, wenn der Ausschnitt sie zeigt; sonst nenne den Verweis, statt zu "
    "raten. Nenne Gesetze mit ihrem Kürzel wie in den Fundstellen (z. B. ‚DDG‘), ohne alte Namen zuzuordnen.\n"
    "Antworte NUR als JSON: {\"erlaeuterungen\": [{\"id\": <Nummer>, \"text\": \"…\"}], \"nicht_passend\": [<Nummern>], "
    "\"hinweis\": \"1–2 Sätze, z. B. wenn nichts passt, die Frage mehrere Rechtsgebiete berührt oder eine Fachperson "
    "gefragt werden sollte\"}. In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine Zeilenumbrüche."
)


_STOP = set("der die das und oder ein eine einen einem einer ich du wir mich mir muss kann darf wie wann was wer "
             "welche welcher welches bis zu zum zur von vom mit für bei auf aus als ist sind wird werden nach".split())


def excerpt(text: str, query: str, size: int = 2400) -> str:
    """Ausschnitt einer langen Vorschrift für die KI-Erläuterung, ohne Sinnverlust:

    Anfang der Vorschrift (Absatz 1 mit Definitionen und Aufzählungen, auf die später verwiesen wird) + Einleitungssatz
    des Absatzes, in dem die Fundstelle liegt („Die folgenden Betriebsausgaben dürfen den Gewinn nicht mindern: …“) +
    die zur Frage passendste Stelle. Sonst drehen sich Verneinungen um oder Verweise („Absatz 1 Nummer 4“) bleiben offen.
    """
    if len(text) <= size:
        return text
    head_len, lead_len = 700, 300
    win = size - head_len - lead_len
    stems = {w[:6] for w in re.findall(r"[a-zäöüß]{4,}", query.lower()) if w not in _STOP}
    low = text.lower()
    best, best_hits = head_len, 0
    for start in range(head_len, max(head_len + 1, len(text) - win + 1), 150):
        n = sum(low[start:start + win].count(s) for s in stems)
        if n > best_hits:
            best, best_hits = start, n
    if not best_hits:
        return text[:size] + " …"
    parts = [text[:head_len]]
    absatz = list(re.finditer(r"\(\d+[a-z]?\)\s", text[head_len:best]))
    if absatz:
        a = head_len + absatz[-1].start()
        parts.append(text[a:min(a + lead_len, best)])
    parts.append(text[best:best + win])
    return " … ".join(p.strip() for p in parts) + (" …" if best + win < len(text) else "")


def _middle_part(h: dict) -> bool:
    """Teil 2/5 usw.: beginnt mitten in der Vorschrift, Einleitungssätze fehlen."""
    part = h.get("part") or ""
    return bool(part) and not part.startswith("1/")


def _fallback(hits: list[dict]) -> dict:
    return {"erlaeuterungen": [], "nicht_passend": [h["id"] for h in hits],
            "hinweis": "Automatische Fundstellen ohne KI-Erläuterung (keine KI-Anbindung konfiguriert). Prüfe die "
                       "zitierten Texte direkt.", "ai": False}


def explain(query: str, hits: list[dict]) -> dict:
    """{erlaeuterungen: [{id, text}], nicht_passend: [id, ...], hinweis, ai}. Erklärt nur — empfiehlt nichts."""
    if not hits:
        return {"erlaeuterungen": [], "nicht_passend": [],
                "hinweis": "Zu dieser Frage wurden keine passenden Fundstellen gefunden.", "ai": False}
    # Mittelteile („3/5“) beginnen ohne den Einleitungssatz, der festlegt, was für die aufgezählten Punkte gilt.
    # Im Live-Test hat das Modell daraus trotz Warnhinweis das Gegenteil gemacht (§ 4 Abs. 5 Nr. 6b EStG) – solche
    # Fundstellen werden deshalb nur wörtlich gezeigt und nicht erläutert.
    middle = [h["id"] for h in hits if _middle_part(h)]
    explainable = [h for h in hits if h["id"] not in middle]
    if not explainable:
        return {"erlaeuterungen": [], "nicht_passend": [], "nur_wortlaut": middle, "ai": False,
                "hinweis": "Die Fundstellen sind Auszüge aus der Mitte längerer Vorschriften. Ohne deren Anfang ordnen "
                           "wir sie nicht ein – lies bitte den vollständigen amtlichen Text über den Link."}
    payload = {"frage": query, "fundstellen": [
        {"id": h["id"], "gesetz": h["law_abbreviation"] or h["slug"], "paragraph": h["enbez"], "titel": h["titel"],
         "kategorie": h["category"], "text": excerpt(h["text"], query)} for h in explainable]}
    data = None
    for attempt in (1, 2):
        try:
            raw = complete(SYSTEM + (" Deine letzte Antwort war kein gültiges JSON. Antworte kompakter und achte "
                                     "streng auf die Syntax." if attempt == 2 else ""),
                           [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], max_tokens=1400,
                           purpose="law_search")
            from .insights import _json
            data = _json(raw)
            break
        except LLMUnavailable:
            return _fallback(hits)
        except ValueError as exc:
            log.warning("Gesetzes-Suche: ungültiges JSON (Versuch %s): %s", attempt, exc)
    if data is None:
        return _fallback(hits)
    valid_ids = {h["id"] for h in explainable}
    erl = [{"id": int(e["id"]), "text": str(e["text"])[:600]} for e in data.get("erlaeuterungen", [])
           if str(e.get("id", "")).isdigit() and int(e["id"]) in valid_ids and e.get("text")]
    nicht = sorted({int(i) for i in data.get("nicht_passend", []) if str(i).isdigit() and int(i) in valid_ids}
                  - {e["id"] for e in erl})
    return {"erlaeuterungen": erl, "nicht_passend": nicht, "nur_wortlaut": middle,
            "hinweis": _sentences(str(data.get("hinweis", "")), 700), "ai": True}


def _sentences(text: str, n: int) -> str:
    """Auf höchstens n Zeichen kürzen, aber an einem Satzende."""
    if len(text) <= n:
        return text
    cut = text[:n]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 0 else cut.rsplit(" ", 1)[0] + " …"
