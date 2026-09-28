"""Mitglieder-Fragebogen: Auswahllisten, Sichtbarkeit und strukturierte Passung fürs Matching.

Alle Fragen sind optional. Jede hat einen Zweck (Art. 5 DSGVO, Datenminimierung): Umsatz, Alter, Religion, Gesundheit
und politische Haltung werden bewusst nicht erfragt. Auswahlfelder werden als kommagetrennte Keys gespeichert.

Structured Fit (0–1) ergänzt die semantische Ähnlichkeit der Texte: Er erkennt Paare, die Text-Embeddings nicht
unterscheiden (Gründerin sucht Investor ↔ Investor, Auftraggeber ↔ Dienstleister, gleiche Phase, gemeinsame Sprache).
"""
from __future__ import annotations

ROLES = {
    "gruender": "Gründer/in",
    "inhaber": "Inhaber/in",
    "geschaeftsfuehrung": "Geschäftsführung",
    "selbstaendig": "Selbständig / Freelance",
    "fuehrungskraft": "Führungskraft im Unternehmen",
    "investor": "Investor/in",
    "berater": "Berater/in",
}
STAGES = {
    "idee": "Idee",
    "gruendung": "Gründung (unter 1 Jahr)",
    "wachstum": "Wachstum",
    "etabliert": "Etabliert",
    "nachfolge": "Nachfolge / Verkauf",
}
TEAM_SIZES = {"solo": "Solo", "2-9": "2–9", "10-49": "10–49", "50-249": "50–249", "250+": "250+"}
CUSTOMER_TYPES = {"b2b": "Unternehmen (B2B)", "b2c": "Privatkund:innen (B2C)", "b2g": "Öffentliche Hand"}
LANGUAGES = {
    "de": "Deutsch", "en": "Englisch", "tr": "Türkisch", "ru": "Russisch", "uk": "Ukrainisch", "ar": "Arabisch",
    "fr": "Französisch", "es": "Spanisch", "it": "Italienisch", "pl": "Polnisch",
}
GOAL_CATEGORIES = {
    "kunden": "Mehr Kund:innen / Umsatz",
    "finanzierung": "Finanzierung",
    "team": "Team aufbauen",
    "markt": "Neuer Markt / Export",
    "produkt": "Produkt entwickeln",
    "digital": "Digitalisierung / Effizienz",
    "nachfolge": "Nachfolge / Verkauf",
    "persoenlich": "Persönliche Entwicklung",
}
PARTNER_TYPES = {
    "kunden": "Kund:innen",
    "kooperation": "Vertriebs- oder Kooperationspartner",
    "dienstleister": "Dienstleister / Lieferanten",
    "mitgruender": "Mitgründer/in",
    "team": "Mitarbeitende / Freelancer",
    "investor": "Investor/in, Finanzierung",
    "mentor": "Mentor/in, Sparring",
    "experte": "Expert:in für eine konkrete Frage",
    "peers": "Gleichgesinnte in derselben Phase",
    "startups": "Start-ups zum Investieren oder Begleiten",
}
FIRST_OUTCOMES = {
    "rat": "30 Minuten Rat",
    "kennenlernen": "Erst mal kennenlernen",
    "angebot": "Ein konkretes Angebot",
    "projekt": "Ein gemeinsames Projekt",
}
RESOURCES = {
    "knowhow": "Know-how",
    "kontakte": "Kontakte (Branche, Land)",
    "kundenzugang": "Zugang zu Kund:innen",
    "kapital": "Kapital",
    "raeume": "Räume, Ausstattung",
    "auftraege": "Aufträge zu vergeben",
}
HELP_MODES = {
    "kostenlos": "30 Minuten kostenlos für Mitglieder",
    "mentoring": "Mentoring, regelmäßig",
    "bezahlt": "Als bezahlte Leistung",
}
NETWORK_TIME = {"1-2": "1–2 Std. im Monat", "3-5": "3–5 Std. im Monat", "mehr": "Mehr als 5 Std. im Monat"}
MEETING_MODES = {"vor_ort": "Lieber vor Ort", "online": "Lieber online", "beides": "Beides"}
WORK_VALUES = {
    "verlaesslichkeit": "Verlässlichkeit", "tempo": "Tempo", "qualitaet": "Qualität",
    "langfristig": "Langfristigkeit", "offenheit": "Offenheit", "fairness": "Fairness",
}

# Auswahlfelder: Profilspalte -> (Optionen, Mehrfachauswahl, max. Anzahl)
CHOICE_FIELDS = {
    "role": (ROLES, False, 1),
    "stage": (STAGES, False, 1),
    "team_size": (TEAM_SIZES, False, 1),
    "customer_types": (CUSTOMER_TYPES, True, 3),
    "languages": (LANGUAGES, True, 10),
    "goal_category": (GOAL_CATEGORIES, False, 1),
    "partner_types": (PARTNER_TYPES, True, 4),
    "first_outcome": (FIRST_OUTCOMES, False, 1),
    "resources": (RESOURCES, True, 6),
    "help_mode": (HELP_MODES, False, 1),
    "network_time": (NETWORK_TIME, False, 1),
    "meeting_mode": (MEETING_MODES, False, 1),
    "work_values": (WORK_VALUES, True, 3),
}

# Freitextfelder zusätzlich zu den vier Hub-Fragen: Spalte -> max. Länge
TEXT_FIELDS = {
    "markets": 300, "goal_12m": 800, "milestone_90d": 400, "q_tried": 800, "not_wanted": 400,
    "asked_for": 600, "proud_of": 600, "talk_topics": 400, "unknown_fact": 400,
}

# Felder, die privat bleiben können (nur für Matching und die eigene Aiko). Standard: privat, Freigabe per Opt-in.
SHAREABLE_PRIVATE = {
    "goal_12m": "Ziel für 12 Monate",
    "milestone_90d": "Meilenstein in 90 Tagen",
    "q_challenge": "Größte Herausforderung",
    "q_tried": "Was du schon versucht hast",
}
# Nie für andere sichtbar, auch nicht über KI-Texte
ALWAYS_PRIVATE = {"not_wanted"}

STAGE_ORDER = list(STAGES)


def split(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def labels(options: dict, value: str | None) -> list[str]:
    return [options[k] for k in split(value) if k in options]


def label(options: dict, value: str | None) -> str:
    return options.get((value or "").strip(), "")


def clean_choice(field: str, values: list[str]) -> str:
    options, multi, limit = CHOICE_FIELDS[field]
    keep = [v for v in dict.fromkeys(values) if v in options]
    return ",".join(keep[:limit] if multi else keep[:1])


# --------------------------------------------------------------------------- Structured Fit
def _stage_rank(p) -> int:
    return STAGE_ORDER.index(p.stage) if p.stage in STAGE_ORDER else -1


def _mentor(a, b) -> float:
    if b.help_mode == "mentoring":
        return 1.0
    ra, rb = _stage_rank(a), _stage_rank(b)
    return 0.8 if ra >= 0 and rb > ra else 0.3


PARTNER_FIT = {  # Person a sucht diesen Typ – wie gut erfüllt Person b das? (0–1)
    "kunden": lambda a, b: 1.0 if "auftraege" in split(b.resources) else
    0.6 if b.role in ("inhaber", "geschaeftsfuehrung", "fuehrungskraft") else 0.3,
    "kooperation": lambda a, b: 1.0 if "kooperation" in split(b.partner_types) else
    0.6 if {"kontakte", "kundenzugang"} & set(split(b.resources)) else 0.2,
    "dienstleister": lambda a, b: 1.0 if b.role in ("selbstaendig", "berater") or b.help_mode == "bezahlt" else 0.3,
    "mitgruender": lambda a, b: 1.0 if "mitgruender" in split(b.partner_types) else 0.5 if b.role == "gruender" else 0.1,
    "team": lambda a, b: 1.0 if b.role == "selbstaendig" else 0.2,
    "investor": lambda a, b: 1.0 if b.role == "investor" or "kapital" in split(b.resources) else 0.1,
    "mentor": _mentor,
    "experte": lambda a, b: 1.0 if b.help_mode in ("kostenlos", "mentoring") else 0.5,
    "peers": lambda a, b: 1.0 if a.stage and a.stage == b.stage else 0.2,
    "startups": lambda a, b: 1.0 if b.role == "gruender" else 0.6 if b.stage in ("idee", "gruendung", "wachstum") else 0.1,
}

GOAL_FIT = {  # Ziel von a -> wie sehr kann b dabei helfen? (0–1)
    "kunden": lambda a, b: max(PARTNER_FIT["kunden"](a, b), PARTNER_FIT["kooperation"](a, b)),
    "finanzierung": PARTNER_FIT["investor"],
    "team": lambda a, b: 1.0 if b.role == "selbstaendig" or "kontakte" in split(b.resources) else 0.3,
    "markt": lambda a, b: 0.8 if "kontakte" in split(b.resources) else 0.3,
    "produkt": lambda a, b: 0.8 if b.role in ("selbstaendig", "berater") or "knowhow" in split(b.resources) else 0.3,
    "digital": lambda a, b: 0.8 if b.role in ("selbstaendig", "berater") or "knowhow" in split(b.resources) else 0.3,
    "nachfolge": lambda a, b: 1.0 if b.role == "investor" or "kapital" in split(b.resources) else
    0.6 if b.stage == "nachfolge" else 0.2,
    "persoenlich": _mentor,
}


def _partner_fit(seeker, other) -> float | None:
    types = [t for t in split(seeker.partner_types) if t in PARTNER_FIT]
    return max(PARTNER_FIT[t](seeker, other) for t in types) if types else None


def structured_fit(a, b) -> float | None:
    """Passung aus den Auswahlfeldern (0–1) aus Sicht von a; None, wenn zu wenig strukturierte Angaben vorliegen."""
    parts: list[tuple[float, float]] = []
    fwd = _partner_fit(a, b)
    if fwd is not None:
        parts.append((0.35, fwd))
    back = _partner_fit(b, a)  # Gegenseitigkeit: sucht b auch jemanden wie a?
    if back is not None:
        parts.append((0.20, back))
    if a.goal_category in GOAL_FIT:
        g = GOAL_FIT[a.goal_category](a, b)
        if b.goal_category == a.goal_category:
            g = max(g, 0.6)  # gleiche Aufgabe -> Erfahrungsaustausch
        parts.append((0.15, g))
    la, lb = set(split(a.languages)), set(split(b.languages))
    if la and lb:
        parts.append((0.15, 1.0 if la & lb else 0.0))
    if a.meeting_mode and b.meeting_mode:
        modes = {a.meeting_mode, b.meeting_mode}
        v = 0.2 if modes == {"vor_ort", "online"} else 1.0
        if modes == {"vor_ort"} and (a.city or "").strip().lower() != (b.city or "").strip().lower():
            v = 0.5
        parts.append((0.10, v))
    va, vb = set(split(a.work_values)), set(split(b.work_values))
    if va and vb:
        parts.append((0.05, min(1.0, len(va & vb) / 2)))
    weight = sum(w for w, _ in parts)
    if weight < 0.3:
        return None
    return sum(w * v for w, v in parts) / weight


def shared_facts(a, b) -> list[str]:
    """Kurze, sachliche Gründe aus den Auswahlfeldern (für Begründungen ohne KI und als KI-Kontext)."""
    out = []
    for t in split(a.partner_types):
        if t in PARTNER_FIT and PARTNER_FIT[t](a, b) >= 1.0:
            out.append(f"passt zu „{PARTNER_TYPES[t]}“, was du suchst")
            break
    if "auftraege" in split(b.resources) and "kunden" in split(a.partner_types):
        out.append("hat Aufträge zu vergeben")
    if a.stage and a.stage == b.stage:
        out.append(f"steht wie du in der Phase „{STAGES[a.stage]}“")
    common = [LANGUAGES[k] for k in split(a.languages) if k in split(b.languages) and k != "de"]
    if common:
        out.append("spricht auch " + ", ".join(common))
    if b.help_mode == "kostenlos":
        out.append("bietet Mitgliedern 30 Minuten kostenlos an")
    return out
