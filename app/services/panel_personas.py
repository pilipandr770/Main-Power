"""Synthetische Personas für das Markt-Panel.

100 Rollen-Archetypen (62 Privatpersonen, 38 Geschäftsentscheider) + Attribut-Verteilungen (Alter, Region, Haushalt,
Einkommen, Gesundheit, Gewicht, Technikaffinität, Preisbewusstsein, Werte, Persönlichkeit). Aus Archetyp und Zufalls-
stichprobe (mit Seed, also reproduzierbar) entsteht ein Steckbrief, den das Modell beantwortet.

ALLE Personen sind FIKTIV. Gesundheit und Gewicht sind erfundene Merkmale zur realistischen Streuung, keine Daten realer
Menschen. Es werden bewusst KEINE Merkmale wie Religion, Herkunft oder politische Haltung erzeugt (Stereotypen-Risiko).
Erweitern: weitere Zeilen in B2C/B2B ergänzen; die Stichprobe zieht mit Zurücklegen, wenn mehr Personen als Rollen gewünscht sind.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

# --------------------------------------------------------------------------- Archetypen
# Privatpersonen: (Beruf/Lebenslage, Alter von, bis, Einkommensklasse, Bildung, Situation)
B2C = [
    ("Schüler:in Oberstufe", 16, 19, "mittel", "Schule", "Taschengeld und Nebenjob, viel online, kaum eigenes Budget"),
    ("Auszubildende:r im Handwerk", 17, 24, "niedrig", "Realschule", "kleine Ausbildungsvergütung, lebt oft noch bei den Eltern"),
    ("Auszubildende:r kaufmännisch", 17, 24, "niedrig", "Realschule", "wenig Geld, viel Zeit am Handy, Berufsschule"),
    ("Student:in (Bachelor)", 19, 26, "niedrig", "Abitur", "BAföG oder Nebenjob, WG, rechnet jeden Euro"),
    ("Masterstudent:in oder Promovierende:r", 24, 32, "niedrig", "Studium", "knappes Einkommen, hohe Bildung, Zeitdruck"),
    ("Berufseinsteiger:in im Büro", 22, 28, "mittel", "Studium", "erster Vollzeitjob, baut sich Alltag und Konsum auf"),
    ("Pflegefachkraft", 25, 60, "mittel", "Ausbildung", "Schichtdienst, körperlich fordernd, wenig Zeit"),
    ("Erzieher:in", 22, 60, "mittel", "Ausbildung", "moderates Gehalt, viel Verantwortung, Personalmangel"),
    ("Grundschullehrer:in", 28, 62, "mittel", "Studium", "Beamtenstatus möglich, Zeit knapp, Familie"),
    ("Gymnasiallehrer:in", 30, 62, "hoch", "Studium", "sicheres Einkommen, bildungsnah"),
    ("Softwareentwickler:in", 24, 55, "hoch", "Studium", "sehr technikaffin, gut bezahlt, kritisch bei Marketing"),
    ("IT-Administrator:in", 25, 58, "mittel", "Ausbildung", "technikaffin, pragmatisch, sicherheitsbewusst"),
    ("Bankkaufmann/-frau", 25, 60, "mittel", "Ausbildung", "regelkonform, sicherheitsorientiert"),
    ("Versicherungsvertreter:in", 26, 62, "mittel", "Ausbildung", "verkaufsorientiert, netzwerkt viel"),
    ("Verkäufer:in im Einzelhandel", 18, 62, "niedrig", "Ausbildung", "geringes Einkommen, lange Arbeitszeiten am Wochenende"),
    ("Lagerhelfer:in oder Kassierer:in", 20, 62, "niedrig", "Hauptschule", "Niedriglohn, wenig Puffer"),
    ("LKW-Fahrer:in", 25, 62, "mittel", "Ausbildung", "viel unterwegs, wenig feste Termine"),
    ("Paketzusteller:in", 20, 58, "niedrig", "Hauptschule", "körperliche Arbeit, Zeitdruck"),
    ("Elektriker:in (Geselle)", 22, 58, "mittel", "Ausbildung", "praktisch, handwerklich, skeptisch bei Apps"),
    ("Handwerksmeister:in (selbstständig)", 30, 62, "hoch", "Ausbildung", "eigener Betrieb, Auftragslage schwankt"),
    ("Friseur:in", 20, 58, "niedrig", "Ausbildung", "Trinkgeld-abhängig, viel soziale Medien"),
    ("Koch oder Köchin", 20, 58, "niedrig", "Ausbildung", "lange Schichten, wenig Freizeit"),
    ("Gastronom:in (Inhaber:in)", 30, 62, "mittel", "Ausbildung", "Kosten- und Personaldruck"),
    ("Ärzt:in im Krankenhaus", 28, 62, "hoch", "Studium", "Überstunden, gutes Einkommen, wenig Zeit"),
    ("Hausärzt:in mit eigener Praxis", 38, 66, "hoch", "Studium", "Unternehmer:in, Bürokratie-Frust"),
    ("Medizinische Fachangestellte", 20, 58, "niedrig", "Ausbildung", "Praxisalltag, begrenztes Gehalt"),
    ("Apotheker:in", 30, 62, "hoch", "Studium", "gut informiert, regulatorisch geprägt"),
    ("Physiotherapeut:in", 22, 58, "mittel", "Ausbildung", "gesundheitsaffin, körperlich aktiv"),
    ("Rechtsanwält:in", 30, 64, "hoch", "Studium", "detailgenau, misstrauisch bei Versprechen"),
    ("Steuerberater:in", 32, 64, "hoch", "Studium", "präzise, zeitkritisch, kostenbewusst bei Tools"),
    ("Ingenieur:in im Maschinenbau", 26, 62, "hoch", "Studium", "analytisch, technikaffin"),
    ("Produktionsmitarbeiter:in", 20, 62, "mittel", "Ausbildung", "Schichtarbeit, solide aber knappe Mittel"),
    ("Beamt:in in der Verwaltung", 25, 63, "mittel", "Ausbildung", "sicherer Job, regelorientiert"),
    ("Polizist:in", 22, 60, "mittel", "Ausbildung", "Schichtdienst, sicherheitsbewusst"),
    ("Freelance-Designer:in", 24, 55, "mittel", "Studium", "schwankendes Einkommen, kreativ, preisbewusst"),
    ("Content-Creator:in", 18, 35, "niedrig", "Abitur", "lebt von Reichweite, kennt Trends, testet gern"),
    ("Journalist:in", 26, 60, "mittel", "Studium", "kritisch, recherchiert, wenig Budget"),
    ("Selbstständige:r Berater:in", 32, 64, "hoch", "Studium", "Zeit ist Geld, schneller Entscheider"),
    ("Kleinunternehmer:in mit Online-Shop", 25, 55, "mittel", "Ausbildung", "knappe Marge, sucht Effizienz"),
    ("Landwirt:in", 28, 65, "mittel", "Ausbildung", "saisonal, praktisch, wenig digitalaffin"),
    ("Rentner:in mit Grundsicherung", 66, 85, "niedrig", "Hauptschule", "sehr knappes Budget, rechnet jeden Cent"),
    ("Rentner:in, gut situiert", 64, 82, "hoch", "Studium", "Zeit und Geld, qualitätsbewusst, reisefreudig"),
    ("Frührentner:in mit Gesundheitsproblemen", 55, 64, "niedrig", "Ausbildung", "eingeschränkt belastbar, knappes Geld"),
    ("Alleinerziehende:r in Teilzeit", 25, 45, "niedrig", "Ausbildung", "Zeit und Geld knapp, Kinderbetreuung"),
    ("Familie mit zwei kleinen Kindern (Doppelverdiener)", 28, 42, "mittel", "Studium", "Zeitmangel, Kosten für Kinder"),
    ("Hausfrau oder Hausmann", 30, 60, "mittel", "Ausbildung", "Haushaltsbudget des Partners, Familienorganisation"),
    ("Mutter oder Vater in Elternzeit", 28, 40, "mittel", "Studium", "weniger Einkommen, offen für Alltagshilfen"),
    ("Pendler:in aus dem Umland", 28, 58, "mittel", "Ausbildung", "lange Wege, Auto, zeitknapp"),
    ("Arbeitsuchende:r (Bürgergeld/ALG I)", 20, 62, "niedrig", "Hauptschule", "sehr knappes Budget, unsichere Lage"),
    ("Minijobber:in", 18, 70, "niedrig", "Realschule", "Zuverdienst, kaum Puffer"),
    ("Quereinsteiger:in ohne Abschluss", 22, 35, "niedrig", "Abitur", "wechselnde Jobs, sucht Perspektive"),
    ("Fachkraft aus dem Ausland (Blue Card)", 26, 45, "hoch", "Studium", "neu in Deutschland, gut bezahlt, sucht Orientierung"),
    ("Pflegende:r Angehörige:r neben dem Job", 45, 65, "mittel", "Ausbildung", "doppelt belastet, wenig Zeit"),
    ("Fitness- und Sportbegeisterte:r", 18, 40, "mittel", "Ausbildung", "gesundheitsbewusst, Wearables, Ernährungs-Apps"),
    ("Vielspieler:in (Gaming)", 16, 40, "mittel", "Abitur", "hohe Technikaffinität, gibt gern für Hobby aus"),
    ("Nachhaltigkeitsbewusste:r Konsument:in", 20, 45, "mittel", "Studium", "achtet auf Herkunft und Umwelt, zahlt teils mehr"),
    ("Führungskraft im Konzern", 38, 60, "hoch", "Studium", "statusbewusst, wenig Zeit, hohe Ansprüche"),
    ("Remote-Worker oder Digitalnomad", 25, 45, "hoch", "Studium", "ortsunabhängig, Tool-affin"),
    ("ÖPNV-abhängige:r Dorfbewohner:in", 18, 80, "niedrig", "Realschule", "wenig Infrastruktur, langsames Internet"),
    ("Studierende mit Kind", 22, 35, "niedrig", "Abitur", "knappes Geld, wenig Zeit"),
    ("Senior:in, digital skeptisch", 70, 88, "mittel", "Hauptschule", "misstrauisch bei Apps und Abos"),
    ("Senior:in, technikaffin (Silver Surfer)", 62, 80, "mittel", "Ausbildung", "nutzt Smartphone gern, neugierig"),
]

# Geschäftsentscheider: (Rolle, Alter von, bis, Unternehmensgröße, Branche, Situation)
B2B = [
    ("Geschäftsführer:in Kleinbetrieb (1–10 MA)", 35, 62, "1–10 MA", "Handwerk", "macht alles selbst, entscheidet allein, preissensibel"),
    ("Geschäftsführer:in KMU (10–50 MA)", 38, 62, "10–50 MA", "Produktion", "Wachstum und Fachkräftemangel"),
    ("Geschäftsführer:in Mittelstand (50–250 MA)", 40, 64, "50–250 MA", "Großhandel", "strategisch, braucht belastbaren ROI"),
    ("Solo-Selbstständige:r", 28, 60, "1 Person", "Dienstleistung", "jede Investition muss sich schnell lohnen"),
    ("Startup-Gründer:in (Pre-Seed)", 24, 40, "1–5 MA", "SaaS", "wenig Kapital, schnelle Experimente"),
    ("Startup-Gründer:in (Seed, B2C-App)", 25, 42, "5–20 MA", "Consumer-App", "wachstumsgetrieben, datenorientiert"),
    ("CFO / Finanzleiter:in Mittelstand", 38, 62, "100–500 MA", "Industrie", "risikoscheu, zahlengetrieben"),
    ("Controller:in", 28, 58, "50–250 MA", "Handel", "Kostenkontrolle, Berichte"),
    ("Buchhalter:in / Lohnbuchhaltung", 28, 62, "10–50 MA", "Dienstleistung", "wenig technikaffin, zeiteffizient"),
    ("Steuerberater:in (Kanzlei, 15 MA)", 35, 64, "15 MA", "Steuerberatung", "mandantenorientiert, DSGVO-bewusst"),
    ("IT-Leiter:in Mittelstand", 32, 60, "100–500 MA", "Industrie", "Sicherheit, Integration, Betrieb"),
    ("CISO / IT-Sicherheitsbeauftragte:r", 32, 58, "200–1000 MA", "Finanzen", "sehr risikoscheu, prüft Anbieter streng"),
    ("Datenschutzbeauftragte:r", 30, 62, "50–500 MA", "Mehrere", "rechtlich genau, Skepsis bei neuen Tools"),
    ("Compliance-Officer Konzern", 33, 60, "500+ MA", "Versicherung", "dokumentations- und regelorientiert"),
    ("Einkaufsleiter:in", 33, 62, "100–500 MA", "Automobilzulieferer", "verhandlungsstark, vergleicht Anbieter"),
    ("Vertriebsleiter:in B2B", 33, 60, "50–250 MA", "Maschinenbau", "ergebnisorientiert, schnelle Entscheidungen"),
    ("Account Manager:in", 26, 50, "50–250 MA", "IT-Dienstleistung", "kundennah, will Verkaufsargumente"),
    ("Marketingleiter:in", 30, 55, "50–250 MA", "Konsumgüter", "kreativ, Budget begrenzt, Kennzahlen-orientiert"),
    ("Performance-Marketing-Manager:in", 25, 45, "20–100 MA", "E-Commerce", "datengetrieben, testet viel"),
    ("E-Commerce-Manager:in", 26, 50, "20–50 MA", "Handel", "conversion-fokussiert, Tool-erfahren"),
    ("HR-Leiter:in", 35, 62, "300+ MA", "Dienstleistung", "mitarbeiterorientiert, Datenschutz wichtig"),
    ("Recruiter:in", 24, 50, "50–250 MA", "Personaldienstleistung", "schnelle Prozesse, Tool-affin"),
    ("Betriebsleiter:in / Produktionsleiter:in", 35, 62, "50–100 MA", "Produktion", "Zuverlässigkeit, wenig Zeit für Tests"),
    ("Logistikleiter:in", 33, 62, "100–200 MA", "Logistik", "Effizienz, Termintreue"),
    ("Facility-Manager:in", 30, 60, "50–200 MA", "Immobilien", "Kostendruck, Dienstleistersteuerung"),
    ("Praxisinhaber:in (Arzt/Zahnarzt)", 38, 66, "5–15 MA", "Gesundheit", "Unternehmer:in ohne Zeit, Bürokratiefrust"),
    ("Kanzlei-Partner:in (Recht)", 38, 66, "10–40 MA", "Recht", "risikobewusst, kostensensibel"),
    ("Inhaber:in Architekturbüro", 35, 64, "5–20 MA", "Bau", "projektgetrieben, kreativ"),
    ("Bauunternehmer:in", 38, 64, "20–100 MA", "Bau", "pragmatisch, wenig digitalaffin"),
    ("Hotelier oder Restaurantleiter:in", 35, 62, "20–80 MA", "Gastgewerbe", "Personal- und Energiekosten"),
    ("Agenturinhaber:in (Marketing/Design)", 30, 55, "5–25 MA", "Kreativ", "sucht Tools, die Zeit sparen"),
    ("Unternehmensberater:in (selbstständig)", 38, 66, "1 Person", "Beratung", "erfahren, hohe Ansprüche"),
    ("Vereinsvorstand oder NGO-Geschäftsführung", 35, 66, "5–50 MA", "Non-Profit", "sehr knappe Budgets, Ehrenamt"),
    ("Kommunale:r IT-Entscheider:in", 35, 62, "200+ MA", "Verwaltung", "Vergaberecht, Sicherheit, langsame Prozesse"),
    ("Schulleiter:in", 40, 64, "50–100 MA", "Bildung", "begrenzte Mittel, Datenschutz"),
    ("Innovationsmanager:in Konzern", 30, 55, "1000+ MA", "Industrie", "offen für Piloten, komplexe Freigaben"),
    ("Product Manager:in SaaS", 27, 50, "50–200 MA", "Software", "nutzerorientiert, priorisiert hart"),
    ("Head of Operations / COO Scale-up", 30, 50, "100–300 MA", "Scale-up", "Prozesse skalieren, Tool-erfahren"),
]

# --------------------------------------------------------------------------- Verteilungen
BUNDESLAENDER = [  # (Name, Bevölkerungsanteil in %)
    ("Nordrhein-Westfalen", 21.4), ("Bayern", 15.9), ("Baden-Württemberg", 13.4), ("Niedersachsen", 9.6),
    ("Hessen", 7.6), ("Sachsen", 4.8), ("Rheinland-Pfalz", 4.9), ("Berlin", 4.5), ("Schleswig-Holstein", 3.5),
    ("Brandenburg", 3.0), ("Sachsen-Anhalt", 2.6), ("Thüringen", 2.5), ("Hamburg", 2.3), ("Mecklenburg-Vorpommern", 1.9),
    ("Saarland", 1.2), ("Bremen", 0.8),
]
RHEIN_MAIN = ["Frankfurt am Main", "Offenbach", "Wiesbaden", "Darmstadt", "Mainz", "Hanau", "Rüsselsheim", "Bad Homburg",
              "Frankfurt-Bornheim", "Hofheim am Taunus", "Friedberg (Hessen)", "Aschaffenburg"]
ORTSTYP = [("Großstadt", 31), ("Mittelstadt", 27), ("Kleinstadt", 24), ("ländlicher Raum", 18)]
INCOME = {"niedrig": (1300, 2400), "mittel": (2400, 4600), "hoch": (4600, 9500)}
NAMES_F = ["Anna", "Sarah", "Lisa", "Julia", "Laura", "Katharina", "Elena", "Nadine", "Sabine", "Petra", "Yasemin", "Chiara",
           "Monika", "Sophie", "Melanie", "Ayşe", "Olga", "Ingrid", "Marta", "Nicole", "Jana", "Birgit", "Selin", "Carla"]
NAMES_M = ["Thomas", "Michael", "Stefan", "Markus", "Jan", "Daniel", "Mehmet", "Klaus", "Frank", "Jonas", "Tobias", "Kai",
           "Rainer", "Ahmed", "Dieter", "Lukas", "Bernd", "Andreas", "Milan", "Sven", "Oliver", "Emre", "Uwe", "Paul"]
NAMES_D = ["Alex", "Sascha", "Robin", "Kim", "Sam"]
LASTS = list("ABCDEFGHKLMNPRSTWZ")
HEALTH_CHRONIC = ["Bluthochdruck", "Diabetes Typ 2", "chronische Rückenschmerzen", "Asthma", "Allergien (Pollen/Hausstaub)",
                  "Migräne", "Schilddrüsenerkrankung", "Arthrose"]
HEALTH_OTHER = ["psychische Belastung (Erschöpfung/Stress)", "Schlafprobleme", "Seh- oder Hörbeeinträchtigung",
                "Mobilitätseinschränkung"]
WEIGHT = [("normalgewichtig", 48), ("leicht übergewichtig", 33), ("stark übergewichtig (Adipositas)", 17), ("untergewichtig", 2)]
VALUES = ["Sicherheit", "Zeit sparen", "Preis-Leistung", "Nachhaltigkeit", "Status und Qualität", "Familie", "Unabhängigkeit",
          "Gesundheit", "Einfachheit", "Innovation", "Zuverlässigkeit", "Gemeinschaft"]
TRAITS = ["skeptisch", "neugierig", "pragmatisch", "vorsichtig", "impulsiv", "detailorientiert", "sparsam", "aufgeschlossen",
          "ungeduldig", "loyal gegenüber Bekanntem"]
BUYING = ["risikoscheu", "pragmatisch", "experimentierfreudig", "preisgetrieben", "qualitätsgetrieben"]
AUTHORITY = ["entscheidet allein über das Budget", "braucht Freigabe der Geschäftsführung", "beeinflusst, entscheidet aber nicht selbst"]
EDU_MAP = {"Schule": "Schüler:in", "Hauptschule": "Hauptschulabschluss", "Realschule": "Realschulabschluss (Mittlere Reife)",
           "Abitur": "Abitur", "Ausbildung": "abgeschlossene Berufsausbildung", "Studium": "Hochschulabschluss"}


@dataclass
class Persona:
    idx: int
    name: str
    kind: str  # "c" Privatperson | "b" Geschäftsentscheider
    label: str
    age: int
    gender: str
    region: str
    ortstyp: str
    household: str
    income_eur: int | None
    education: str
    health: str
    weight: str
    tech: int
    price_sens: int
    values: list[str] = field(default_factory=list)
    trait: str = ""
    situation: str = ""
    company: str = ""
    authority: str = ""
    buying: str = ""

    def sheet(self) -> str:
        """Steckbrief für das Modell."""
        L = [f"Name: {self.name}, {self.age} Jahre, {self.gender}", f"Rolle/Lage: {self.label}",
             f"Wohnort: {self.ortstyp} in {self.region}", f"Situation: {self.situation}"]
        if self.kind == "b":
            L += [f"Unternehmen: {self.company}", f"Budgetverantwortung: {self.authority}", f"Entscheidungsstil: {self.buying}"]
        else:
            L += [f"Haushalt: {self.household}", f"Netto-Haushaltseinkommen: ca. {self.income_eur:,} €/Monat".replace(",", "."),
                  f"Bildung: {self.education}"]
        L += [f"Gesundheit: {self.health}", f"Gewicht: {self.weight}", f"Technikaffinität: {self.tech}/5",
              f"Preisbewusstsein: {self.price_sens}/5", f"Wichtige Werte: {', '.join(self.values)}", f"Persönlichkeit: {self.trait}"]
        return "\n".join(L)

    def public(self) -> dict:
        d = asdict(self)
        d["income_class"] = income_class(self.income_eur) if self.income_eur else ""
        d["age_group"] = age_group(self.age)
        return d


def age_group(age: int) -> str:
    return "unter 30" if age < 30 else "30–49" if age < 50 else "50–64" if age < 65 else "65+"


def income_class(eur: int | None) -> str:
    if not eur:
        return ""
    return "niedrig (bis 2.400 €)" if eur < 2400 else "mittel (2.400–4.600 €)" if eur < 4600 else "hoch (ab 4.600 €)"


def _weighted(rnd: random.Random, pairs):
    total = sum(w for _, w in pairs)
    x = rnd.uniform(0, total)
    for v, w in pairs:
        x -= w
        if x <= 0:
            return v
    return pairs[-1][0]


def _household(rnd: random.Random, age: int, label: str) -> str:
    if "Alleinerziehende" in label:
        return f"alleinerziehend, {rnd.choice([1, 1, 2, 3])} Kind(er)"
    if "Familie mit zwei" in label:
        return "verheiratet/Partnerschaft, 2 kleine Kinder"
    if "Studierende mit Kind" in label:
        return "Partnerschaft oder allein, 1 Kind"
    if "Elternzeit" in label:
        return "Partnerschaft, 1 Kleinkind"
    if age < 22:
        return "wohnt bei den Eltern oder in der WG"
    if age < 30:
        return rnd.choice(["Single, eigene Wohnung", "WG", "Paar ohne Kinder", "Paar ohne Kinder"])
    if age < 45:
        return rnd.choice(["Paar mit Kindern", "Paar mit Kindern", "Paar ohne Kinder", "Single", "alleinerziehend, 1 Kind"])
    if age < 65:
        return rnd.choice(["Paar, Kinder aus dem Haus", "Paar mit Jugendlichen", "Single", "Paar ohne Kinder"])
    return rnd.choice(["Paar im Ruhestand", "verwitwet, allein lebend", "Paar im Ruhestand"])


def _health(rnd: random.Random, age: int, label: str) -> str:
    if "Gesundheitsproblemen" in label:
        return rnd.choice(HEALTH_CHRONIC + HEALTH_OTHER[:2]) + " (belastend im Alltag)"
    p_chronic = min(0.65, 0.04 + age * 0.0085)
    x = rnd.random()
    if x < p_chronic:
        return rnd.choice(HEALTH_CHRONIC)
    if x < p_chronic + 0.09:
        return rnd.choice(HEALTH_OTHER)
    return "gesund"


def make_persona(idx: int, rnd: random.Random, arche: tuple, kind: str, regional: str) -> Persona:
    label, lo, hi = arche[0], arche[1], arche[2]
    age = rnd.randint(lo, hi)
    gender = _weighted(rnd, [("weiblich", 49.5), ("männlich", 49.5), ("divers", 1.0)])
    pool = NAMES_F if gender == "weiblich" else NAMES_M if gender == "männlich" else NAMES_D
    name = f"{rnd.choice(pool)} {rnd.choice(LASTS)}."
    if regional == "rhein-main":
        region, ortstyp = f"{rnd.choice(RHEIN_MAIN)} (Rhein-Main, Hessen)", _weighted(rnd, [("Großstadt", 60), ("Mittelstadt", 25), ("Kleinstadt", 15)])
    else:
        region, ortstyp = _weighted(rnd, BUNDESLAENDER), _weighted(rnd, ORTSTYP)
    values = rnd.sample(VALUES, 2)
    p = Persona(idx=idx, name=name, kind=kind, label=label, age=age, gender=gender, region=region, ortstyp=ortstyp,
                household="", income_eur=None, education="", health=_health(rnd, age, label),
                weight=_weighted(rnd, WEIGHT), tech=1, price_sens=3, values=values, trait=rnd.choice(TRAITS))
    tech_base = 4 if age < 35 else 3 if age < 55 else 2 if age < 70 else 2
    p.tech = max(1, min(5, tech_base + rnd.choice([-1, 0, 0, 1])))
    if kind == "c":
        cls, edu, situation = arche[3], arche[4], arche[5]
        lo_i, hi_i = INCOME[cls]
        p.household = _household(rnd, age, label)
        base = rnd.randint(lo_i, hi_i)
        if "alleinerziehend" in p.household:
            base = int(base * 0.85)
        p.income_eur = int(round(base, -1 if base < 3000 else -2))
        p.education = EDU_MAP.get(edu, edu)
        p.situation = situation
        p.price_sens = max(1, min(5, {"niedrig": 5, "mittel": 3, "hoch": 2}[cls] + rnd.choice([-1, 0, 0, 1])))
    else:
        size, branche, situation = arche[3], arche[4], arche[5]
        p.company = f"{branche}, {size}"
        p.authority = rnd.choice(AUTHORITY)
        p.buying = rnd.choice(BUYING)
        p.situation = situation
        p.household = ""
        p.education = "Hochschul- oder Fachschulabschluss"
        p.price_sens = max(1, min(5, (4 if size in ("1 Person", "1–5 MA", "1–10 MA") else 3) + rnd.choice([-1, 0, 0, 1])))
    return p


def sample(n: int, audience: str = "beide", regional: str = "de", seed: int = 1) -> list[Persona]:
    """Zieht n Personas. audience: privat|business|beide. regional: de|rhein-main. Reproduzierbar über seed."""
    rnd = random.Random(seed)
    pool: list[tuple[str, tuple]] = []
    if audience in ("privat", "beide"):
        pool += [("c", a) for a in B2C]
    if audience in ("business", "beide"):
        pool += [("b", a) for a in B2B]
    order = pool[:]
    rnd.shuffle(order)
    picks = []
    while len(picks) < n:  # erst jede Rolle einmal, dann mit Zurücklegen (neue Attribute)
        take = order if len(picks) == 0 or len(picks) % len(order) == 0 else [rnd.choice(order)]
        for kind, a in take:
            picks.append((kind, a))
            if len(picks) >= n:
                break
    return [make_persona(i, rnd, a, kind, regional) for i, (kind, a) in enumerate(picks[:n])]
