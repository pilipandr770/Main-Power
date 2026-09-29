"""Profil-Interview: Aiko fragt Schritt für Schritt, das Mitglied antwortet frei, die KI schlägt Profilwerte vor.

Nichts wird ohne Bestätigung gespeichert: `extract` liefert nur Vorschläge, `questionnaire.apply_values` übernimmt
erst nach Klick. Eine Antwort darf mehrere Felder füllen („Ich bin Gründer, suche eine Investorin …“).
Ohne API-Schlüssel landet die Antwort im gefragten Feld; Auswahlfelder werden über ihre Bezeichnungen erkannt.
"""
from __future__ import annotations

import json
import re

from .. import questionnaire as qn
from .insights import _json
from .llm import LLMUnavailable, complete

# Reihenfolge nach Nutzen fürs Matching: (Feld, Frage, Hinweis)
QUESTIONS = [
    ("q_focus", "Was machst du – in ein, zwei Sätzen?", "Branche, Zielgruppe, was du lieferst."),
    ("role", "Was trifft auf dich zu: Gründer/in, Inhaber/in, Geschäftsführung, selbständig, Investor/in …?", ""),
    ("goal_12m", "Was willst du in den nächsten 12 Monaten erreichen?", "Ein Ziel, an dem man Erfolg erkennt."),
    ("partner_types", "Wen suchst du gerade – Kund:innen, Partner, Investor:innen, eine Mentorin, Mitgründer …?", ""),
    ("q_looking_for", "Beschreib die ideale Person oder Firma, die dir jetzt weiterhelfen würde.", ""),
    ("q_can_help", "Womit kannst du anderen helfen?", "2–3 Themen, bei denen man dich anrufen darf."),
    ("milestone_90d", "Woran merkst du in 90 Tagen, dass du vorankommst?", "Ein messbarer Schritt."),
    ("q_challenge", "Was hält dich gerade am meisten auf?", ""),
    ("stage", "In welcher Phase ist dein Unternehmen – Idee, Gründung, Wachstum, etabliert oder Nachfolge?", ""),
    ("resources", "Was kannst du einbringen – Know-how, Kontakte, Kapital, Räume oder sogar Aufträge?", ""),
    ("languages", "In welchen Sprachen führst du Gespräche?", ""),
    ("q_tried", "Was hast du dafür schon versucht?", "So bekommst du keine Tipps, die du schon kennst."),
    ("asked_for", "Wofür fragen dich andere am häufigsten um Rat?", ""),
    ("help_mode", "Wie hilfst du anderen Mitgliedern – 30 Minuten kostenlos, als Mentoring oder als bezahlte Leistung?", ""),
    ("proud_of", "Auf welches Ergebnis bist du stolz?", "Ein Projekt oder eine Referenz."),
    ("not_wanted", "Gibt es Themen oder Angebote, mit denen du nicht kontaktiert werden möchtest?",
     "Bleibt privat und dient nur als Filter."),
    ("talk_topics", "Und zum Schluss: Worüber könntest du stundenlang reden?", ""),
]
FIELD_LABELS = {
    "q_focus": "Was du machst", "role": "Rolle", "goal_12m": "Ziel (12 Monate)", "partner_types": "Du suchst",
    "q_looking_for": "Ideale Person oder Firma", "q_can_help": "Kann helfen mit", "milestone_90d": "Meilenstein (90 Tage)",
    "q_challenge": "Herausforderung", "stage": "Phase", "resources": "Bringt ein", "languages": "Sprachen",
    "q_tried": "Schon versucht", "asked_for": "Wird gefragt zu", "help_mode": "So hilfst du", "proud_of": "Stolz auf",
    "not_wanted": "Nicht kontaktieren zu", "talk_topics": "Kann stundenlang reden über", "headline": "Rolle in einem Satz",
    "industry": "Branche", "company": "Unternehmen", "team_size": "Teamgröße", "goal_category": "Ziel-Kategorie",
    "customer_types": "Kund:innen", "markets": "Märkte", "first_outcome": "Gutes erstes Gespräch",
    "meeting_mode": "Treffen", "work_values": "Wichtig in der Zusammenarbeit", "expertise": "Expertise",
    "unknown_fact": "Steht in keinem Lebenslauf", "network_time": "Zeit fürs Netzwerk", "city": "Ort",
}
EXTRACTABLE = set(qn.ALL_TEXT_FIELDS) | set(qn.CHOICE_FIELDS)
# Wörter in Optionsbezeichnungen, die allein nichts über die Option aussagen
_FILLER = {"einer", "eines", "konkrete", "frage", "derselben", "phase", "unter", "mitglieder", "stunden", "lieber"}


def _questions() -> list[tuple[str, str, str]]:
    extra = [(f"custom:{q.key}", q.label, q.help or "") for q in qn.club_questions()]
    return QUESTIONS + extra


def _filled(p, field: str) -> bool:
    if field.startswith("custom:"):
        return bool((p.custom_answers or {}).get(field[7:]))
    return bool((getattr(p, field, "") or "").strip())


def next_question(p, skip: list[str] | None = None) -> dict | None:
    skip = set(skip or [])
    todo = [q for q in _questions() if q[0] not in skip]
    open_ = [q for q in todo if not _filled(p, q[0])]
    total = len(_questions())
    if not open_:
        return None
    field, question, hint = open_[0]
    return {"field": field, "question": question, "hint": hint,
            "done": total - len([q for q in _questions() if not _filled(p, q[0])]), "total": total}


def _display(field: str, value) -> str:
    if field in qn.CHOICE_FIELDS:
        options = qn.CHOICE_FIELDS[field][0]
        return ", ".join(options.get(v, v) for v in qn.split(value))
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


def _match_options(field: str, text: str) -> str:
    """Auswahl ohne KI: Optionen, deren Bezeichnung (oder Wortstamm) in der Antwort vorkommt."""
    options, _multi, _limit = qn.CHOICE_FIELDS[field]
    low = text.lower()
    hits = []
    for key, lbl in options.items():
        words = [w for w in re.split(r"[^\wäöüß]+", lbl.lower()) if len(w) >= 5 and w not in _FILLER]
        if key.lower() in low or any(w[:max(4, len(w) - 2)] in low for w in words):
            hits.append(key)
    return qn.clean_choice(field, hits)


def _to_key(field: str, item) -> str:
    """Modelle liefern gelegentlich die Bezeichnung statt des Schlüssels („Gründer/in“ statt „gruender“)."""
    options = qn.CHOICE_FIELDS[field][0]
    item = str(item).strip()
    if item in options:
        return item
    by_label = {lbl.casefold(): key for key, lbl in options.items()}
    return by_label.get(item.casefold()) or (_match_options(field, item).split(",")[0] if item else "")


def _fallback(field: str, answer: str) -> dict:
    if field in qn.CHOICE_FIELDS:
        value = _match_options(field, answer)
        return {field: value} if value else {}
    return {field: answer.strip()}


def _schema() -> dict:
    out = {f: "Freitext" for f in qn.ALL_TEXT_FIELDS}
    for f, (options, multi, limit) in qn.CHOICE_FIELDS.items():
        out[f] = {"werte": list(options), "bedeutung": options, "mehrfach": multi, "max": limit}
    return out


def extract(p, field: str, answer: str) -> tuple[list[dict], bool]:
    """Vorschläge [{field, label, value, display}] aus einer freien Antwort; zweiter Wert: KI benutzt?"""
    answer = (answer or "").strip()[:2000]
    if not answer:
        return [], False
    values, ai = {}, False
    if field.startswith("custom:"):
        q = next((q for q in qn.club_questions() if f"custom:{q.key}" == field), None)
        if q is None:
            return [], False
        if q.kind == "text":
            values = {field: answer}
        else:
            hits = [o for o in q.option_list if o.lower() in answer.lower()]
            values = {field: hits if q.kind == "multi" else (hits[0] if hits else "")}
    else:
        system = ("Du hilfst einem Mitglied von {CLUB}, sein Profil auszufüllen. Aus der freien Antwort leitest du "
                  "Profilwerte ab. Regeln: nur, was die Person wirklich sagt – nichts erfinden, nichts ergänzen; "
                  "Texte knapp in der Ich-/Du-neutralen Profilsprache (1–2 Sätze), Deutsch; Auswahlfelder NUR mit den "
                  "erlaubten Schlüsseln aus 'werte' (Mehrfachauswahl als Liste). Das gefragte Feld hat Vorrang; weitere "
                  "Felder nur, wenn sie eindeutig in der Antwort stecken. Keine besonderen Kategorien personenbezogener "
                  "Daten (Gesundheit, Religion, Politik …). Antworte NUR als JSON-Objekt {feld: wert}.")
        payload = {"gefragtes_feld": field, "antwort": answer, "felder": _schema()}
        try:
            data = _json(complete(system, [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                                  max_tokens=600, purpose="profile_interview"))
            values = {k: v for k, v in data.items() if k in EXTRACTABLE and v not in (None, "", [])}
            ai = True
        except (LLMUnavailable, ValueError, KeyError, TypeError):
            values = {}
        if field not in values:
            values.update(_fallback(field, answer))
    proposals = []
    for f, v in values.items():
        if f in qn.CHOICE_FIELDS:
            v = qn.clean_choice(f, [_to_key(f, i) for i in (v if isinstance(v, list) else qn.split(str(v)))])
            if not v:
                continue
        elif f in qn.ALL_TEXT_FIELDS:
            v = str(v).strip()[:qn.ALL_TEXT_FIELDS[f]]
        elif not f.startswith("custom:") or v in ("", []):
            continue
        label = FIELD_LABELS.get(f) or next((q[1] for q in _questions() if q[0] == f), f)
        proposals.append({"field": f, "label": label, "value": v, "display": _display(f, v),
                          "private": f in qn.SHAREABLE_PRIVATE or f in qn.ALWAYS_PRIVATE})
    proposals.sort(key=lambda d: d["field"] != field)  # gefragtes Feld zuerst
    return proposals, ai
