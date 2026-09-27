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


def search(query: str, category: str = "", k: int = 6) -> list[dict]:
    """Fragt die interne Suche ab; jeder Treffer trägt den Original-Gesetzestext (verbatim, keine KI)."""
    url = current_app.config.get("LAWS_API_URL")
    if not url:
        raise LawsUnavailable("Die Gesetzes-Suche ist auf dieser Installation nicht eingerichtet.")
    query = query.strip()[:800]
    if not query:
        raise LawsUnavailable("Bitte gib eine Frage oder ein Stichwort ein.")
    params = {"q": query, "k": max(1, min(MAX_HITS, k))}
    if category:
        params["category"] = category
    try:
        resp = requests.get(f"{url}/search", params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning("laws-api nicht erreichbar: %s", exc)
        raise LawsUnavailable("Die Gesetzes-Suche ist gerade nicht erreichbar. Bitte versuch es in ein paar Minuten "
                              "erneut.") from exc
    except ValueError as exc:
        raise LawsUnavailable("Die Gesetzes-Suche hat eine unerwartete Antwort geliefert.") from exc
    if not isinstance(data, list):
        raise LawsUnavailable("Die Gesetzes-Suche hat eine unerwartete Antwort geliefert.")
    hits = []
    for i, h in enumerate(data[:MAX_HITS]):
        if not isinstance(h, dict) or not h.get("text"):
            continue
        hits.append({"id": i + 1, "slug": str(h.get("slug", ""))[:80],
                     "law_abbreviation": str(h.get("law_abbreviation", ""))[:120],
                     "law_title": str(h.get("law_title", ""))[:300], "category": str(h.get("category", ""))[:80],
                     "enbez": str(h.get("enbez", ""))[:60], "titel": str(h.get("titel", ""))[:300],
                     "text": str(h.get("text", ""))[:6000], "stand": h.get("stand") or [],
                     "source_url": str(h.get("source_url", ""))[:300], "score": round(float(h.get("score", 0)), 3)})
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
    "antworten.\n"
    "Antworte NUR als JSON: {\"erlaeuterungen\": [{\"id\": <Nummer>, \"text\": \"…\"}], \"nicht_passend\": [<Nummern>], "
    "\"hinweis\": \"1–2 Sätze, z. B. wenn nichts passt, die Frage mehrere Rechtsgebiete berührt oder eine Fachperson "
    "gefragt werden sollte\"}. In Textwerten keine doppelten Anführungszeichen (nutze ‚einfache‘), keine Zeilenumbrüche."
)


def _fallback(hits: list[dict]) -> dict:
    return {"erlaeuterungen": [], "nicht_passend": [h["id"] for h in hits],
            "hinweis": "Automatische Fundstellen ohne KI-Erläuterung (keine KI-Anbindung konfiguriert). Prüfe die "
                       "zitierten Texte direkt.", "ai": False}


def explain(query: str, hits: list[dict]) -> dict:
    """{erlaeuterungen: [{id, text}], nicht_passend: [id, ...], hinweis, ai}. Erklärt nur — empfiehlt nichts."""
    if not hits:
        return {"erlaeuterungen": [], "nicht_passend": [],
                "hinweis": "Zu dieser Frage wurden keine passenden Fundstellen gefunden.", "ai": False}
    payload = {"frage": query, "fundstellen": [
        {"id": h["id"], "gesetz": h["law_abbreviation"] or h["slug"], "paragraph": h["enbez"], "titel": h["titel"],
         "kategorie": h["category"], "text": h["text"][:1200]} for h in hits]}
    data = None
    for attempt in (1, 2):
        try:
            raw = complete(SYSTEM + (" Deine letzte Antwort war kein gültiges JSON. Antworte kompakter und achte "
                                     "streng auf die Syntax." if attempt == 2 else ""),
                           [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], max_tokens=1400,
                           purpose="law_search")
            data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1], strict=False)
            break
        except LLMUnavailable:
            return _fallback(hits)
        except ValueError as exc:
            log.warning("Gesetzes-Suche: ungültiges JSON (Versuch %s): %s", attempt, exc)
    if data is None:
        return _fallback(hits)
    valid_ids = {h["id"] for h in hits}
    erl = [{"id": int(e["id"]), "text": str(e["text"])[:600]} for e in data.get("erlaeuterungen", [])
           if str(e.get("id", "")).isdigit() and int(e["id"]) in valid_ids and e.get("text")]
    nicht = sorted({int(i) for i in data.get("nicht_passend", []) if str(i).isdigit() and int(i) in valid_ids}
                  - {e["id"] for e in erl})
    return {"erlaeuterungen": erl, "nicht_passend": nicht, "hinweis": str(data.get("hinweis", ""))[:400], "ai": True}
