"""Startdaten je Klub: Branding-Vorlage, Formate, FAQ, Leistungen, Beispieltermine und (optional) Demo-Mitglieder.

Standardklub (DEFAULT_CLUB_SLUG) = Main Power; weitere Klubs legt create_club() an (Plattform-Konsole oder CLI).

Demo-Mitglieder haben E-Mails @demo.main-power.local und lassen sich mit `flask remove-demo` entfernen.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from .extensions import db
from flask import current_app

from .models import (Club, Consent, Event, GoalCheckin, KnowledgeItem, MeetingFormat, Profile, Service, User,
                     utcnow)
from .tenancy import use_club
from .services.matching import refresh_embeddings

log = logging.getLogger(__name__)
DEMO_DOMAIN = "demo.main-power.local"

FAQ = [
    ("Muss ich mich vorher anmelden?",
     "Ja, für die meisten Formate ist eine Anmeldung nötig — so können wir Locations passend planen und dir einen "
     "Platz sicher zusagen.", True),
    ("Was kostet die Teilnahme?",
     "Main Power Hub kostet 25 € pro Person: 18 € für das Frühstück und 7 € Organisationsbeitrag. Stammtisch, "
     "Laufen und Frauenkreis sind kostenlos.", True),
    ("Muss ich etwas erzählen, oder kann ich einfach zuhören?",
     "Niemand muss sprechen. Du kannst erzählen, zuhören oder einfach dabei sein — beides ist willkommen.", True),
    ("In welcher Sprache erhalte ich Informationen?",
     "Die Website und die Orientierung durch Aiko sind auf Deutsch. Bei Fragen zu einem konkreten Treffen schreib "
     "uns gerne.", True),
    ("Was mache ich, wenn ich kurzfristig doch nicht kann?",
     "Bitte gib uns kurz Bescheid, damit wir den Platz an jemand anderen weitergeben können. Auf der Plattform "
     "kannst du dich im Termin selbst abmelden.", True),
    ("Kann ich als Unternehmen oder Expert:in mitwirken?",
     "Ja, sehr gerne. Schreib uns an hallo@main-power.org und erzähl uns kurz, wie du Main Power mitgestalten "
     "möchtest.", True),
    ("Wie funktioniert das Matching?",
     "Du beschreibst in deinem Profil, was du machst, was du erreichen willst, wen du suchst und womit du anderen "
     "helfen kannst – im Formular oder im Gespräch mit Aiko. Die KI vergleicht Bedarf und Angebot inhaltlich — nicht "
     "nur über Schlagworte — und schlägt dir Menschen vor, die sich gegenseitig weiterhelfen können. Matching ist "
     "freiwillig und jederzeit abschaltbar.", True),
    ("Wer sieht meine Kontaktdaten?",
     "Niemand, ohne dass du zustimmst. Andere Mitglieder können dir eine Kontaktanfrage senden. Erst wenn du sie "
     "annimmst, sehen beide Seiten E-Mail, Telefon und Profil-Links.", True),
    ("Was ist der Community-Chat?",
     "Ein geschlossener Telegram-Chat nur für verifizierte Mitglieder. Du verbindest dein Konto einmalig mit "
     "Telegram und erhältst dann einen persönlichen Einladungslink.", False),
    ("Kann ich selbst ein Treffen organisieren?",
     "Ja. Unter „Termine“ kannst du ein Community-Treffen vorschlagen — zum Beispiel ein thematisches Frühstück. "
     "Nach einer kurzen Prüfung erscheint es im Kalender.", False),
]

SERVICES = [
    dict(slug="ki-chatbots", sort=1, title="KI-Chatbots & digitale Assistenten",
         summary="Ein Assistent für deine Website, WhatsApp oder Telegram, der Anfragen beantwortet, Termine "
                 "vorbereitet und Leads qualifiziert — DSGVO-konform mit Hosting in der EU.",
         description="Wir entwickeln KI-Assistenten, die dein Angebot kennen und im Ton deiner Marke antworten: "
                     "auf der Website, in WhatsApp oder Telegram. Inklusive Datenschutzkonzept, Kennzeichnung nach "
                     "KI-Verordnung und Übergabe an dein Team, wenn ein Mensch gefragt ist.",
         benefits="Antworten rund um die Uhr, auch außerhalb der Bürozeiten\nWeniger Routinefragen im Postfach\n"
                  "Leads mit vollständigen Angaben statt halber E-Mails\nHosting in Deutschland, Auftragsverarbeitung inklusive",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="Festpreis nach Erstgespräch", provider_name="Andrii-IT",
         provider_email="info@andrii-it.de"),
    dict(slug="cybersecurity-check", sort=2, title="Cybersecurity-Check für Selbstständige & KMU",
         summary="Wie angreifbar ist dein Unternehmen von außen? Ein Check deiner Website, E-Mail-Sicherheit und "
                 "öffentlich sichtbaren Systeme — mit verständlichem Bericht und klaren nächsten Schritten.",
         description="Wir prüfen, was Angreifer:innen über dein Unternehmen sehen: Website, Domain- und "
                     "E-Mail-Konfiguration (SPF, DKIM, DMARC), veraltete Software, offene Dienste. Du bekommst "
                     "einen Bericht ohne Fachchinesisch und eine priorisierte Maßnahmenliste.",
         benefits="Schwachstellen finden, bevor andere es tun\nNachweis für Versicherer und Kund:innen\n"
                  "Priorisierte Maßnahmen statt Panik",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="ab Pauschalpreis, je nach Umfang", provider_name="Andrii-IT",
         provider_email="info@andrii-it.de"),
    dict(slug="nis2-dsgvo-compliance", sort=3, title="NIS2- und DSGVO-Check",
         summary="Bist du von NIS2 betroffen und was musst du konkret tun? Betroffenheitsprüfung, Lückenanalyse "
                 "und Maßnahmenplan für Geschäftsführung und Team.",
         description="Das NIS2-Umsetzungsgesetz erweitert den Kreis der regulierten Unternehmen deutlich — und "
                     "nimmt die Geschäftsführung persönlich in die Pflicht. Wir klären Betroffenheit, bewerten den "
                     "Ist-Stand und erstellen einen umsetzbaren Plan.",
         benefits="Klarheit über Betroffenheit und Fristen\nDokumentation für Prüfungen\nSchulung der Geschäftsführung",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="nach Aufwand", provider_name="Andrii-IT",
         provider_email="info@andrii-it.de"),
    dict(slug="ki-verordnung", sort=4, title="KI im Unternehmen — rechtssicher einführen",
         summary="Du nutzt ChatGPT, Chatbots oder KI-Tools? Wir prüfen, welche Pflichten aus KI-Verordnung und "
                 "DSGVO für dich gelten, und helfen bei Richtlinien und Schulung.",
         description="Seit 2025 gilt die Pflicht zur KI-Kompetenz für Unternehmen, die KI einsetzen, seit August "
                     "2026 die Transparenzpflichten für Chatbots. Wir machen eine Bestandsaufnahme, ordnen Risiken ein "
                     "und liefern Nutzungsrichtlinie und Schulung.",
         benefits="Überblick über eingesetzte KI-Tools\nNutzungsrichtlinie für dein Team\nNachweis der KI-Kompetenz",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="Paketpreis", provider_name="Andrii-IT",
         provider_email="info@andrii-it.de"),
]

SEO_SERVICE = dict(
    slug="seo-check", sort=0, title="SEO-Check für deine Website",
    summary="Aiko prüft deine Seite in einer Minute und liefert einen Bericht mit priorisierten Maßnahmen.",
    description="Du gibst die Adresse deiner Website (und bis zu fünf Suchbegriffe) ein. Wir prüfen die Seite technisch und "
                "inhaltlich — HTTPS, Ladezeit, robots.txt und Sitemap, Titel, Beschreibung, Überschriften, Bilder, "
                "strukturierte Daten, Social-Vorschau und deine Keywords. Aiko fasst das in einem verständlichen "
                "Bericht zusammen: was gut ist, was fehlt und was du zuerst tun solltest. Eine Momentaufnahme der geprüften "
                "Seite; wir versprechen keine bestimmten Platzierungen.",
    benefits="Bericht in unter einer Minute\nKonkrete Maßnahmen, nach Wirkung sortiert\nFür Einsteiger:innen verständlich, ohne SEO-Vorwissen\nBerichte lassen sich speichern und ausdrucken",
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="KI-Assistenz Aiko",
    provider_email="")

COMPLIANCE_SERVICE = dict(
    slug="compliance-check", sort=1, title="Compliance-Check für deine Website",
    summary="Aiko prüft, ob deine Website die wichtigsten Pflichtangaben und Datenschutz-Grundlagen erfüllt.",
    description="Du gibst die Adresse deiner Website ein. Wir prüfen automatisch: Impressum (Anschrift, E-Mail, Vertretung, "
                "Register), Datenschutzerklärung (Pflichtinhalte, eingesetzte Dienste), Cookie-Einwilligung und "
                "Tracking-Dienste, externe Schriftarten, bei Shops AGB, Widerrufsbelehrung und Barrierefreiheit sowie "
                "grundlegende Sicherheits-Header. Aiko fasst das in einem verständlichen Bericht zusammen und sortiert "
                "die Maßnahmen nach Dringlichkeit. Automatische, statische Prüfung — keine Rechtsberatung.",
    benefits="Bericht in unter einer Minute\nDringlichste Lücken zuerst\nOhne juristisches Vorwissen verständlich\nBerichte lassen sich speichern und ausdrucken",
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="KI-Assistenz Aiko",
    provider_email="")

SECURITY_SERVICE = dict(
    slug="sicherheits-check", sort=2, title="Sicherheits-Check (passiv)",
    summary="Aiko prüft die öffentlich sichtbare Sicherheit deiner Domain: Verschlüsselung, Header, Cookies und E-Mail-Schutz.",
    description="Du gibst deine Domain ein. Wir prüfen wie ein normaler Besucher und wie ein Mailserver: HTTPS und "
                "Zertifikat, TLS-Version, Weiterleitung auf https, HSTS, Sicherheits-Header, Cookie-Schutz, SPF, DMARC, "
                "DKIM, DNSSEC, CAA und security.txt. Aiko erklärt die Ergebnisse verständlich und sagt, was dein Hoster, "
                "deine Agentur oder dein Mailanbieter umstellen sollte. Es gibt keinen Portscan und keine Angriffe — "
                "das ist ausdrücklich kein Penetrationstest.",
    benefits="Bericht in unter einer Minute\nPassiv: keine Tests gegen deine Systeme\nKonkrete Aufgaben für Hoster und Mailanbieter\nBerichte lassen sich speichern und ausdrucken",
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="KI-Assistenz Aiko",
    provider_email="")

LAWS_SERVICE = dict(
    slug="gesetzes-suche", sort=3, title="Gesetzes-Suche: Bundesrecht zitiert und erklärt",
    summary="Aiko findet passende Paragraphen im deutschen Bundesrecht, zitiert sie wörtlich und erklärt, was sie bedeuten.",
    description="Du stellst eine Frage oder gibst ein Stichwort ein. Eine semantische Suche über den kompletten deutschen "
                "Bundesgesetzkorpus (gesetze-im-internet.de) findet passende Paragraphen; Aiko zitiert sie wörtlich und "
                "erklärt in eigenen Worten, was der Text bedeutet. Wichtig: Das ist keine Rechtsberatung und keine "
                "Handlungsempfehlung — Aiko bewertet nie deinen Einzelfall, sondern erklärt ausschließlich, was im "
                "Gesetzestext steht. Kennzeichnung als KI gemäß Art. 50 KI-VO. Für eine verbindliche Einschätzung immer "
                "eine Rechtsanwältin oder einen Rechtsanwalt konsultieren.",
    benefits="Wörtliches Zitat mit amtlicher Quelle\nErläuterung in verständlicher Sprache\nKeine Rechtsberatung, keine Handlungsempfehlung\nAnfragen lassen sich speichern und ausdrucken",
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="KI-Assistenz Aiko",
    provider_email="")

PANEL_SERVICE = dict(
    slug="markt-panel", sort=-1, title="Markt-Panel: Idee oder Produkt an KI-Personas testen",
    summary="100 fiktive Personas mit unterschiedlichem Alter, Einkommen, Beruf und Lebenslage bewerten dein Produkt — vor dem Launch.",
    description="Du beschreibst dein Produkt, deine Idee oder ein Update (optional mit ein oder zwei Preisen). Ein synthetisches Panel aus "
                "bis zu 100 fiktiven Personas — Privatpersonen und Geschäftsentscheider, aus ganz Deutschland oder der Region Rhein-Main — "
                "beantwortet den Fragebogen: kauft sie, zu welchem Preis, warum nicht, was müsste sich ändern. Du bekommst Kaufabsicht, "
                "Preiskurve, Auswertung nach Zielgruppen, einen A/B-Preisvergleich, Themen der Bedenken und Empfehlungen von Aiko. "
                "Wichtig: Die Personas sind KI-generiert und fiktiv. Das Ergebnis liefert Hinweise und Hypothesen für Positionierung, "
                "Preis und Botschaft; es ersetzt keine Befragung echter Kund:innen und sagt keine Umsätze voraus.",
    benefits="Feedback in ca. 2 Minuten statt Wochen\nKaufabsicht, Preiskurve und A/B-Preistest\nAuswertung nach Alter, Einkommen, Technikaffinität u. a.\nKonkrete Empfehlungen, was du vor dem Launch ändern solltest",
    member_benefit="Für Mitglieder: 3 Läufe pro Monat inklusive", price_hint="Mitglieder: 3 Läufe/Monat inklusive",
    provider_name="KI-Assistenz Aiko", provider_email="")

DEMO_MEMBERS = [
    ("Julia", "Wagner", "Steuerberaterin, eigene Kanzlei", "Steuerberatung",
     "Steuerberatung für Gründer:innen und Selbstständige, Schwerpunkt digitale Buchhaltung",
     "Mehr Mandant:innen aus der Kreativ- und Tech-Szene gewinnen",
     "Steuerliche Fragen bei Gründung, Holding-Strukturen, Umsatzsteuer im EU-Ausland",
     "Kooperation mit Unternehmensberater:innen und Agenturen, die Gründer:innen betreuen",
     "Steuerrecht, Gründung, Buchhaltung, DATEV"),
    ("Markus", "Albrecht", "Fachanwalt für Arbeitsrecht", "Recht",
     "Arbeitsrecht für Arbeitgeber, Kündigungen, Arbeitsverträge, Betriebsvereinbarungen",
     "Mittelständische Unternehmen erreichen, bevor der Konflikt eskaliert",
     "Arbeitsverträge, Abmahnungen, Kündigungsschutz, Mitarbeiterführung aus rechtlicher Sicht",
     "HR-Verantwortliche und Geschäftsführer:innen von KMU im Rhein-Main-Gebiet",
     "Arbeitsrecht, Kündigung, Arbeitsvertrag"),
    ("Leonie", "Brandt", "Marketingstrategin für Start-ups", "Marketing",
     "Positionierung, Go-to-Market und Content-Strategie für junge B2B-Unternehmen",
     "Ich brauche verlässliche Entwickler:innen für Landingpages und Automatisierung",
     "Markenpositionierung, LinkedIn-Strategie, Launch-Kampagnen, Messaging",
     "Tech-Partner:in für Web und Automatisierung, Start-ups mit Marketingbedarf",
     "Marketing, Positionierung, LinkedIn, Start-ups"),
    ("Tobias", "Kern", "Gründer, IT-Start-up (SaaS für Handwerk)", "Software",
     "Wir bauen eine Software für Auftragsplanung im Handwerk",
     "Finanzierung der nächsten Phase und eine erfahrene Mitgründerin für Vertrieb",
     "Produktentwicklung, Softwarearchitektur, Erfahrungen mit Förderprogrammen",
     "Partner:in für ein IT-Start-up mit Vertriebserfahrung, Business Angels, Steuerberatung für Beteiligungen",
     "SaaS, Handwerk, Produktentwicklung"),
    ("Aylin", "Demir", "Exportberaterin", "Außenhandel",
     "Beratung für Mittelständler beim Markteintritt in der Türkei und im Nahen Osten",
     "Rechtliche Absicherung von Vertriebsverträgen im Ausland",
     "Exportrecht, Zoll, Markteintritt Türkei und Golfstaaten, interkulturelle Verhandlung",
     "Anwälte mit internationalem Vertragsrecht, Unternehmen mit Exportplänen",
     "Export, Außenhandel, Zoll, Türkei"),
    ("Daniel", "Hoffmann", "Geschäftsführer, Logistikunternehmen (40 MA)", "Logistik",
     "Regionale Kontraktlogistik und Lagerhaltung für E-Commerce",
     "Fachkräfte finden und Prozesse digitalisieren, IT-Sicherheit ist ein offenes Thema",
     "Logistik, Lagerflächen, Erfahrung mit Personalführung im Wachstum",
     "IT-Sicherheitsberatung, Digitalisierung von Lagerprozessen, Arbeitsrecht",
     "Logistik, E-Commerce, Lager"),
    ("Sofia", "Reuter", "Business Coach & Moderatorin", "Coaching",
     "Coaching für Führungskräfte, Moderation von Strategieworkshops",
     "Sichtbarkeit bei Unternehmer:innen, die gerade wachsen",
     "Führung, Konfliktklärung, Workshop-Moderation, Resilienz",
     "Unternehmen im Wachstum, Veranstaltungsformate als Speakerin",
     "Coaching, Führung, Moderation"),
    ("Jonas", "Weber", "Fotograf & Videograf", "Kreativwirtschaft",
     "Business-Porträts, Imagefilme und Event-Fotografie",
     "Kontinuierliche Aufträge statt Einzelprojekte",
     "Porträts für LinkedIn, Imagefilme, Content für Social Media",
     "Agenturen und Coaches mit regelmäßigem Content-Bedarf",
     "Fotografie, Video, Content"),
    ("Nadine", "Schulz", "Finanzierungsberaterin, Business Angel", "Finanzen",
     "Begleitung von Start-ups bei Finanzierungsrunden, eigene Angel-Investments",
     "Gute Dealflow-Qualität aus der Region",
     "Pitchdeck-Feedback, Finanzplanung, Kontakte zu Investor:innen und Förderbanken",
     "Gründer:innen mit skalierbaren Geschäftsmodellen, insbesondere Software",
     "Finanzierung, Business Angel, Pitch"),
    ("Kerem", "Yilmaz", "Inhaber, Restaurantgruppe (3 Standorte)", "Gastronomie",
     "Gastronomie in Frankfurt, Catering für Firmenevents",
     "Firmenkund:innen für Catering gewinnen und Personal halten",
     "Catering, Eventlocations, Erfahrung als Arbeitgeber in der Gastronomie",
     "Unternehmen mit Eventbedarf, Marketing-Unterstützung, Arbeitsrecht",
     "Gastronomie, Catering, Events"),
    ("Clara", "Neumann", "UX-Designerin (freiberuflich)", "Design",
     "Nutzerzentriertes Design für Apps und Plattformen",
     "Langfristige Kund:innen im B2B-Umfeld",
     "UX-Research, Prototyping, Designsysteme, Usability-Tests",
     "Start-ups und Software-Teams, die ein Produkt bauen",
     "UX, Design, Prototyping"),
    ("Stefan", "Richter", "Versicherungsmakler Gewerbe", "Versicherung",
     "Gewerbeversicherungen, Cyberversicherung, betriebliche Altersvorsorge",
     "Kund:innen den Wert einer Cyberversicherung verständlich machen",
     "Risikoanalyse, Cyberversicherung, Haftpflicht für Selbstständige",
     "IT-Sicherheitsexpert:innen als Partner, Gründer:innen und KMU",
     "Versicherung, Cyberversicherung, Gewerbe"),
]


# Erweiterter Fragebogen für die Demo-Profile (Vorname -> Felder). Wird nur gesetzt, solange `role` leer ist.
DEMO_QUESTIONNAIRE = {
    "Julia": dict(role="inhaber", stage="etabliert", team_size="2-9", customer_types="b2b,b2c", languages="de,en",
                  goal_category="kunden", goal_12m="30 neue Mandant:innen aus der Tech- und Kreativszene gewinnen.",
                  milestone_90d="Zwei feste Kooperationen mit Agenturen, die Gründer:innen betreuen.",
                  q_tried="Google Ads — viele Klicks, kaum passende Anfragen.", partner_types="kooperation,kunden",
                  first_outcome="kennenlernen", resources="knowhow,kontakte", help_mode="kostenlos",
                  asked_for="Welche Rechtsform bei der Gründung sinnvoll ist und wann sich eine Holding lohnt.",
                  proud_of="Über 60 Gründungen steuerlich begleitet, davon drei bis zur Finanzierungsrunde.",
                  network_time="3-5", meeting_mode="beides", work_values="verlaesslichkeit,qualitaet",
                  talk_topics="Rennrad, gute Tabellen und Steuern ohne Fachchinesisch.", public_fields="goal_12m"),
    "Markus": dict(role="berater", stage="etabliert", team_size="2-9", customer_types="b2b", languages="de,en",
                   goal_category="kunden", goal_12m="Fünf feste Beratungsmandate mit KMU aus dem Rhein-Main-Gebiet.",
                   partner_types="kooperation,kunden", first_outcome="kennenlernen", resources="knowhow",
                   help_mode="kostenlos", asked_for="Was bei einer Kündigung oder Abmahnung zu beachten ist.",
                   proud_of="Eine Betriebsvereinbarung für 300 Mitarbeitende ohne Einigungsstelle verhandelt.",
                   network_time="1-2", meeting_mode="vor_ort", work_values="fairness,verlaesslichkeit"),
    "Leonie": dict(role="selbstaendig", stage="wachstum", team_size="solo", customer_types="b2b", languages="de,en",
                   goal_category="team", goal_12m="Ein festes Netzwerk aus zwei Entwickler:innen für Web und Automatisierung.",
                   partner_types="team,kunden", first_outcome="projekt", resources="knowhow,kontakte",
                   help_mode="bezahlt", asked_for="Wie man ein B2B-Produkt auf LinkedIn launcht.",
                   proud_of="Ein Launch mit 400 qualifizierten Leads in sechs Wochen.", network_time="3-5",
                   meeting_mode="online", work_values="tempo,offenheit"),
    "Tobias": dict(role="gruender", stage="gruendung", team_size="2-9", customer_types="b2b", markets="DACH",
                   languages="de,en,ru", goal_category="finanzierung",
                   goal_12m="Seed-Runde über 500.000 € abschließen und eine Mitgründerin für den Vertrieb finden.",
                   milestone_90d="Pitch-Deck fertig und Gespräche mit fünf Business Angels geführt.",
                   q_tried="Zwei Förderanträge (einer abgelehnt) und Kaltakquise bei Investor:innen über LinkedIn.",
                   partner_types="investor,mitgruender,mentor", first_outcome="rat",
                   not_wanted="Versicherungen, Versicherungsmakler, Finanzprodukte und Network-Marketing.",
                   resources="knowhow", help_mode="kostenlos",
                   asked_for="Welche Softwarearchitektur für ein kleines Team passt.",
                   proud_of="40 Handwerksbetriebe nutzen unsere Beta jeden Tag.", network_time="3-5",
                   meeting_mode="beides", work_values="tempo,offenheit",
                   talk_topics="Das Handwerk von morgen und Bergsteigen.",
                   unknown_fact="Hat eine Tischlerlehre gemacht, bevor er programmieren lernte.",
                   public_fields="goal_12m"),
    "Aylin": dict(role="berater", stage="etabliert", team_size="solo", customer_types="b2b",
                  markets="Türkei, Golfstaaten, DACH", languages="de,tr,en", goal_category="kunden",
                  goal_12m="Zehn Mittelständler beim Markteintritt in der Türkei begleiten.",
                  partner_types="kooperation,experte", first_outcome="kennenlernen", resources="kontakte,knowhow",
                  help_mode="kostenlos", asked_for="Wie man einen Vertriebspartner in der Türkei prüft.",
                  proud_of="Markteintritt eines Maschinenbauers in Istanbul in acht Monaten.", meeting_mode="beides",
                  work_values="verlaesslichkeit,langfristig"),
    "Daniel": dict(role="geschaeftsfuehrung", stage="etabliert", team_size="10-49", customer_types="b2b",
                   languages="de,en,pl", goal_category="digital",
                   goal_12m="Lager digitalisieren und die IT-Sicherheit auf NIS-2-Niveau bringen.",
                   milestone_90d="Lagerverwaltungssoftware ausgewählt, Sicherheits-Check beauftragt.",
                   partner_types="dienstleister,experte", first_outcome="angebot",
                   resources="auftraege,raeume,knowhow", help_mode="kostenlos",
                   asked_for="Wie man E-Commerce-Logistik skaliert, ohne dass die Qualität leidet.",
                   meeting_mode="vor_ort", work_values="verlaesslichkeit,qualitaet"),
    "Sofia": dict(role="selbstaendig", stage="etabliert", team_size="solo", customer_types="b2b",
                  languages="de,en,it", goal_category="kunden", partner_types="kunden,kooperation",
                  resources="knowhow", help_mode="mentoring", meeting_mode="beides", work_values="offenheit,fairness"),
    "Jonas": dict(role="selbstaendig", stage="wachstum", team_size="solo", customer_types="b2b,b2c",
                  languages="de,en", goal_category="kunden", goal_12m="Drei Retainer-Kund:innen mit monatlichem Content.",
                  partner_types="kunden,kooperation", resources="knowhow", help_mode="bezahlt",
                  meeting_mode="vor_ort"),
    "Nadine": dict(role="investor", stage="etabliert", team_size="solo", customer_types="b2b", languages="de,en,ru",
                   goal_12m="In zwei Software-Start-ups aus der Region investieren.",
                   partner_types="startups,peers", first_outcome="kennenlernen",
                   resources="kapital,kontakte,knowhow", help_mode="mentoring",
                   asked_for="Ob ein Pitch-Deck bereit für Investor:innen ist.",
                   proud_of="Sechs Beteiligungen, davon zwei erfolgreiche Exits.", meeting_mode="beides",
                   work_values="langfristig,offenheit", public_fields="goal_12m"),
    "Kerem": dict(role="inhaber", stage="etabliert", team_size="50-249", customer_types="b2c,b2b",
                  languages="de,tr,en", goal_category="kunden",
                  goal_12m="Den Catering-Umsatz mit Firmenkund:innen verdoppeln.", partner_types="kunden,kooperation",
                  resources="raeume,auftraege", help_mode="kostenlos", meeting_mode="vor_ort"),
    "Clara": dict(role="selbstaendig", stage="wachstum", team_size="solo", customer_types="b2b",
                  languages="de,en,fr", goal_category="kunden", partner_types="kunden", resources="knowhow",
                  help_mode="bezahlt", meeting_mode="online"),
    "Stefan": dict(role="berater", stage="etabliert", team_size="2-9", customer_types="b2b", languages="de",
                   goal_category="kunden", partner_types="kunden,kooperation", resources="knowhow",
                   help_mode="kostenlos", meeting_mode="vor_ort"),
    "Ivan": dict(role="selbstaendig", stage="wachstum", team_size="solo", customer_types="b2b",
                 languages="de,uk,ru,en", goal_category="kunden", partner_types="kooperation,kunden",
                 resources="knowhow", help_mode="kostenlos", meeting_mode="beides"),
    "Mariam": dict(role="selbstaendig", stage="etabliert", team_size="solo", customer_types="b2b",
                   languages="de,ar,en", partner_types="kooperation", resources="kontakte,knowhow",
                   help_mode="kostenlos"),
    "Hannah": dict(role="berater", stage="wachstum", team_size="solo", customer_types="b2b", languages="de,en",
                   partner_types="kooperation,dienstleister", resources="knowhow", help_mode="bezahlt"),
}


def _seed_demo_questionnaire() -> None:
    """Demo-Profile um den erweiterten Fragebogen ergänzen (idempotent, auch für bestehende Demo-Datenbanken)."""
    changed = []
    for p in Profile.query.join(User).filter(User.email.like(f"%@{DEMO_DOMAIN}")):
        data = DEMO_QUESTIONNAIRE.get(p.user.first_name)
        if not data or p.role:
            continue
        for k, v in data.items():
            setattr(p, k, v)
        changed.append(p)
    for p in changed:
        refresh_embeddings(p, commit=False)
        # Demo: Tobias' 90 Tage sind um (Check-in fällig), die anderen laufen noch
        p.milestone_set_at = utcnow() - timedelta(days=92 if p.user.first_name == "Tobias" else 30)
    julia = next((p for p in changed if p.user.first_name == "Julia"), None)
    if julia and not GoalCheckin.query.filter_by(user_id=julia.user_id).first():
        db.session.add(GoalCheckin(
            user_id=julia.user_id, goal=julia.goal_12m, milestone="Erste Kooperation mit einer Gründungsagentur.",
            status="erreicht", note="Vertrag mit einer Agentur aus Offenbach, zwei Mandate darüber gewonnen.",
            next_milestone=julia.milestone_90d, created_at=utcnow() - timedelta(days=30),
            ai_feedback="Starker Schritt – eine Kooperation, die direkt Mandate bringt, ist genau der Hebel für dein "
                        "Ziel. Als Nächstes: Frag die Agentur nach einer gemeinsamen Sprechstunde für Gründer:innen. "
                        "Leonie arbeitet mit vielen jungen B2B-Teams – ein Austausch mit ihr könnte die zweite "
                        "Kooperation anstoßen."))
    db.session.commit()


# Frühere Standardantworten, die bei `flask init-db` auf den aktuellen Text gehoben werden – nur wenn der Klub sie
# unverändert übernommen hat (eigene Formulierungen bleiben unangetastet).
OUTDATED_FAQ = [  # (Vorlage, Frage, frühere Standardantwort)
    ("mainpower", "Wie funktioniert das Matching?",
     "Du beantwortest in deinem Profil vier Fragen: dein Tätigkeitsbereich, deine größte Herausforderung, womit du "
     "anderen helfen kannst und wonach du konkret suchst. Die KI vergleicht Bedarf und Angebot inhaltlich — nicht "
     "nur über Schlagworte — und schlägt dir Menschen vor, die sich gegenseitig weiterhelfen können. Matching ist "
     "freiwillig und jederzeit abschaltbar."),
    ("neutral", "Wie werde ich Mitglied?",
     "Registriere dich kostenlos und beantworte in deinem Profil vier Fragen. Danach schlägt dir die KI passende "
     "Menschen vor."),
]


def refresh_default_faq() -> int:
    """Veraltete Standardantworten in allen Klubs aktualisieren. Gibt die Zahl geänderter Einträge zurück."""
    from .club_presets import preset_faq
    changed = 0
    for preset, question, old in OUTDATED_FAQ:
        new = next((a for q, a, _ in preset_faq(preset) if q == question), None)
        if not new or new == old:
            continue
        for item in KnowledgeItem.query.execution_options(all_clubs=True).filter_by(question=question, answer=old):
            item.answer = new
            changed += 1
    db.session.commit()
    return changed


def _seed_content(preset: str = "mainpower") -> None:
    from .club_presets import apply_faq, apply_formats, preset_faq, preset_formats
    if MeetingFormat.query.count() == 0:
        apply_formats(preset_formats(preset))
    if KnowledgeItem.query.count() == 0:
        apply_faq(preset_faq(preset))
    for s in SERVICES + [PANEL_SERVICE, SEO_SERVICE, COMPLIANCE_SERVICE, SECURITY_SERVICE, LAWS_SERVICE]:
        if not Service.query.filter_by(slug=s["slug"]).first():
            db.session.add(Service(**s))
    db.session.commit()


def _seed_events() -> None:
    from .services import club as club_settings
    if club_settings.settings().get("events_sync_url"):
        try:
            from .services.events_sync import sync_events
            res = sync_events()
            log.info("Termine importiert: %s", res)
        except Exception as exc:
            log.warning("Termin-Sync nicht möglich (%s) — lege Beispieltermine an", exc)
    if Event.query.filter(Event.starts_at >= utcnow()).count() == 0:
        from .models import FORMATS
        base = utcnow().replace(hour=7, minute=0, second=0, microsecond=0)
        city = club_settings.settings().get("city") or ""
        for i, (key, f) in enumerate(FORMATS.featured()[:4]):
            db.session.add(Event(format=key, title=f["name"], description=f["description"],
                                 starts_at=base + timedelta(days=5 + 4 * i), price_cents=f["price_cents"], location=city,
                                 status="published", source="admin", capacity=24 if f["price_cents"] else None))
        db.session.commit()


def _first_paid_event():
    return (Event.query.filter(Event.starts_at >= utcnow(), Event.price_cents > 0).order_by(Event.starts_at).first()
            or Event.query.filter(Event.starts_at >= utcnow()).order_by(Event.starts_at).first())


def _preferred_formats() -> str:
    from .models import FORMATS
    return ",".join(k for k, _ in FORMATS.featured()[:2])


def _seed_admin() -> None:
    """Erster Superadmin des Standardklubs (aus .env)."""
    email = os.environ.get("ADMIN_EMAIL", "admin@main-power.local").lower()
    if User.query.filter_by(email=email).first():
        return
    pw = os.environ.get("ADMIN_PASSWORD", "admin-passwort-bitte-aendern")
    u = User(email=email, first_name="Asset", last_name="Beissenov", role="superadmin")
    u.set_password(pw)
    u.profile = Profile(headline="Founder, Connector & Ecosystem Builder", company="Main Power Community",
                        industry="Community & Netzwerk", q_focus="Aufbau der Main Power Community in Frankfurt",
                        q_can_help="Kontakte in die Frankfurter Unternehmerszene, Moderation, Community-Aufbau",
                        q_looking_for="Expert:innen, die Themen-Frühstücke mitgestalten",
                        q_challenge="Die Community auf weitere Regionen skalieren")
    db.session.add(u)
    db.session.commit()
    log.warning("Superadmin angelegt: %s / %s  (Passwort sofort ändern!)", email, pw)


def _seed_demo_members() -> None:
    if User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).count():
        return
    for first, last, headline, industry, focus, challenge, can_help, looking, expertise in DEMO_MEMBERS:
        u = User(email=f"{first.lower()}.{last.lower()}@{DEMO_DOMAIN}", first_name=first, last_name=last)
        u.set_password("demo-passwort-123")
        u.profile = Profile(headline=headline, industry=industry, q_focus=focus, q_challenge=challenge,
                            q_can_help=can_help, q_looking_for=looking, expertise=expertise,
                            preferred_formats=_preferred_formats(), allow_matching=True, visible_in_directory=True,
                            bio=f"{first} ist Teil der {_club_name()} (Demo-Profil).")
        db.session.add(u)
        for kind in ("privacy", "values", "matching", "directory"):
            db.session.add(Consent(user=u, kind=kind, granted=True, version="demo", source="seed"))
    db.session.commit()
    for p in Profile.query.join(User).filter(User.email.like(f"%@{DEMO_DOMAIN}")):
        refresh_embeddings(p, commit=False)
    db.session.commit()

    # Demo-Mitglieder zum nächsten Hub anmelden -> Matching-Konsole hat Daten
    from .models import Registration
    hub = _first_paid_event()
    if hub:
        for u in User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).limit(9):
            db.session.add(Registration(event_id=hub.id, user_id=u.id, status="reserved", amount_cents=hub.price_cents))
        db.session.commit()


# Zusätzliche Demo-Konten für Vorführungen: (Vorname, Nachname, Rolle, Branche, Tätigkeit, Herausforderung, kann helfen,
# sucht, Expertise, Besonderheit). Besonderheit: normal | privat (kein Matching/Verzeichnis) | neu (Profil leer)
#                                                | gesperrt | moderator
DEMO_SCENARIOS = [
    ("Ivan", "Kovalenko", "IT-Security-Berater (freiberuflich)", "Cybersecurity",
     "Penetrationstests und Security-Audits für KMU, NIS-2-Vorbereitung",
     "Geschäftsführer:innen sehen Cybersecurity als Kostenfaktor statt als Schutz des Geschäfts",
     "Security-Check der eigenen Systeme, Phishing-Schulungen, Notfallplan bei Angriffen",
     "Steuerberatung und Rechtsberatung, die Mandant:innen auch bei Datenschutzvorfällen begleiten",
     "Cybersecurity, NIS-2, Pentest, Datenschutz", "normal"),
    ("Mariam", "Haddad", "HR-Beraterin & Recruiterin", "Personal",
     "Personalgewinnung und Onboarding für wachsende Unternehmen im Rhein-Main-Gebiet",
     "Gute Fachkräfte finden, ohne dass Kund:innen sich mit Jobportalen abmühen",
     "Stellenprofile, Recruiting-Prozesse, Gehaltsbänder, Employer Branding",
     "Arbeitsrechtliche Beratung für Verträge und eine Marketingagentur für Karriereseiten",
     "Recruiting, Onboarding, Employer Branding, HR", "normal"),
    ("Hannah", "Vogel", "Nachhaltigkeitsberaterin (ESG)", "Nachhaltigkeit",
     "CSRD-/ESG-Berichte und CO₂-Bilanzen für mittelständische Unternehmen",
     "Unternehmen beginnen erst mit dem Thema, wenn die Berichtspflicht schon greift",
     "CO₂-Bilanz, ESG-Reporting, Fördermittel für Energieeffizienz",
     "Softwarepartner für Datenerfassung und Finanzierungsexpert:innen für Investitionen",
     "ESG, CSRD, CO₂-Bilanz, Förderung", "normal"),
    ("Petra", "Lang", "Immobilienmaklerin", "Immobilien",
     "Vermittlung von Gewerbeflächen und Praxisräumen in Frankfurt",
     "Selbstständige und Start-ups finden schwer passende Büros",
     "Bürosuche, Marktüberblick zu Mieten in Frankfurt", "Kontakt zu Gründer:innen mit Raumbedarf",
     "Gewerbeimmobilien, Büro, Miete", "privat"),
    ("Felix", "Baumann", "", "", "", "", "", "", "", "neu"),
    ("Oleg", "Petrenko", "Import/Export-Händler", "Handel",
     "Handel mit Baustoffen zwischen Deutschland und Osteuropa", "Zahlungsausfälle bei Neukunden",
     "Kontakte zu Lieferanten in Osteuropa", "Inkasso- und Rechtsberatung", "Handel, Import, Export", "gesperrt"),
    ("Lisa", "Team", "Community-Managerin", "Community",
     "Organisation der Main-Power-Formate und Betreuung der Mitglieder",
     "Passende Gäste für jedes Format finden", "Fragen zu Formaten, Terminen und Anmeldung",
     "Expert:innen für Themen-Frühstücke", "Community, Events, Moderation", "moderator"),
]


def _seed_demo_scenarios() -> None:
    """Vielfältige Konten für Live-Demos: mit/ohne Einwilligung, leeres Profil, gesperrt, Moderator, Kontaktanfragen."""
    from .models import IntroRequest, Registration
    if User.query.filter_by(email=f"ivan.kovalenko@{DEMO_DOMAIN}").first():
        return
    for first, last, headline, industry, focus, challenge, can_help, looking, expertise, kind in DEMO_SCENARIOS:
        u = User(email=f"{first.lower()}.{last.lower()}@{DEMO_DOMAIN}", first_name=first, last_name=last)
        u.set_password("demo-passwort-123")
        matching_on = kind in ("normal", "gesperrt", "moderator")
        u.profile = Profile(headline=headline, industry=industry, q_focus=focus, q_challenge=challenge,
                            q_can_help=can_help, q_looking_for=looking, expertise=expertise,
                            preferred_formats=_preferred_formats(), allow_matching=matching_on,
                            visible_in_directory=matching_on and kind != "gesperrt",
                            bio=f"{first} ist Teil der {_club_name()} (Demo-Profil)." if headline else "")
        if kind == "gesperrt":
            u.status = "blocked"
        if kind == "moderator":
            u.role = "admin"
        db.session.add(u)
        for ck in ("privacy", "values") + (("matching", "directory") if matching_on else ()):
            db.session.add(Consent(user=u, kind=ck, granted=True, version="demo", source="seed"))
    db.session.commit()
    for p in Profile.query.join(User).filter(User.email.like(f"%@{DEMO_DOMAIN}")):
        if p.q_can_help:
            refresh_embeddings(p, commit=False)
    db.session.commit()

    def by(first: str, last: str) -> User:
        return User.query.filter_by(email=f"{first.lower()}.{last.lower()}@{DEMO_DOMAIN}").one()

    # Kontaktanfragen in allen Zuständen (Kontaktdaten sind nur bei "accepted" sichtbar)
    now = utcnow()
    for a, b, status, msg in [
        (("Tobias", "Kern"), ("Julia", "Wagner"), "accepted", "Hallo Julia, ich gründe gerade und suche eine Steuerberaterin."),
        (("Ivan", "Kovalenko"), ("Markus", "Albrecht"), "pending", "Hallo Markus, lass uns über Datenschutzvorfälle sprechen."),
        (("Leonie", "Brandt"), ("Tobias", "Kern"), "pending", "Ich unterstütze dich gern beim Go-to-Market."),
        (("Daniel", "Hoffmann"), ("Hannah", "Vogel"), "declined", "Wir brauchen eine CO₂-Bilanz für die Flotte."),
    ]:
        ua, ub = by(*a), by(*b)
        db.session.add(IntroRequest(from_user_id=ua.id, to_user_id=ub.id, message=msg, status=status,
                                    responded_at=now if status != "pending" else None))

    # Anmeldungen: bezahlt, Warteliste-ähnlich (reserviert) und storniert
    hub = _first_paid_event()
    if hub:
        for who, status in [(("Ivan", "Kovalenko"), "paid"), (("Mariam", "Haddad"), "registered"),
                            (("Hannah", "Vogel"), "reserved"), (("Petra", "Lang"), "cancelled")]:
            u = by(*who)
            if not Registration.query.filter_by(event_id=hub.id, user_id=u.id).first():
                db.session.add(Registration(event_id=hub.id, user_id=u.id, status=status,
                                            amount_cents=hub.price_cents if status == "paid" else 0))
    # Ein Mitglieder-Treffen wartet auf Freigabe (Moderations-Demo)
    if not Event.query.filter_by(source="member", title="Themen-Frühstück: Cybersecurity im Mittelstand").first():
        db.session.add(Event(format="community", title="Themen-Frühstück: Cybersecurity im Mittelstand",
                             description="Kleine Runde: Was Unternehmen bei NIS-2 jetzt wissen müssen.",
                             starts_at=now + timedelta(days=12), location="Frankfurt Westend", capacity=12,
                             status="pending", source="member", created_by_id=by("Ivan", "Kovalenko").id))
    db.session.commit()


AVATAR_COLORS = [(254, 71, 22), (231, 179, 91), (94, 129, 172), (129, 161, 193), (163, 190, 140), (180, 142, 173),
                 (208, 135, 112), (143, 188, 187), (191, 97, 106), (112, 128, 144)]


def _seed_demo_avatars() -> None:
    """Platzhalter-Fotos (Farbfläche mit Initialen) für Demo-Konten, damit Verzeichnis und Profilseiten lebendig wirken."""
    from PIL import Image, ImageDraw, ImageFont

    from .services import media
    users = (User.query.join(Profile).filter(User.email.like(f"%@{DEMO_DOMAIN}"), Profile.photo.is_(None),
                                             Profile.headline != "").all())
    for i, u in enumerate(users):
        base = AVATAR_COLORS[i % len(AVATAR_COLORS)]
        img = Image.new("RGB", (512, 512), base)
        d = ImageDraw.Draw(img)
        for r in range(0, 360, 6):  # sanfter Verlauf
            shade = tuple(max(0, c - r // 5) for c in base)
            d.ellipse((256 - 360 + r, 256 - 360 + r, 256 + 360 - r, 256 + 360 - r), fill=shade)
        font = ImageFont.load_default(size=190)
        text = u.initials
        box = d.textbbox((0, 0), text, font=font)
        d.text((256 - (box[2] - box[0]) / 2 - box[0], 256 - (box[3] - box[1]) / 2 - box[1]), text, fill=(255, 255, 255),
               font=font)
        name = f"demo-{u.id}.jpg"
        img.save(media.avatar_dir() / name, "JPEG", quality=85)
        u.profile.photo = name
    db.session.commit()


def _club_name() -> str:
    from .services import club as club_settings
    return club_settings.settings().get("full_name") or "Community"


def ensure_default_club() -> Club:
    """Standardklub (Main Power) anlegen, falls es noch keinen gibt — hält bestehende Installationen lauffähig."""
    slug = current_app.config.get("DEFAULT_CLUB_SLUG", "mainpower")
    club = Club.query.execution_options(all_clubs=True).filter_by(slug=slug).first()
    if club is None:
        club = Club(slug=slug, name=current_app.config.get("DEFAULT_CLUB_NAME", "Main Power"),
                    domains=current_app.config.get("DEFAULT_CLUB_DOMAINS", ""))
        db.session.add(club)
        db.session.commit()
    return club


def _seed_club(demo: bool, preset: str) -> None:
    _seed_content(preset)
    _seed_events()
    if demo:
        _seed_demo_members()
        _seed_demo_scenarios()
        _seed_demo_questionnaire()
        _seed_demo_avatars()


def seed(demo: bool = True, club: Club | None = None, preset: str = "mainpower") -> None:
    db.create_all()
    from .services.plans import ensure_defaults
    ensure_defaults()  # plattformweite Tarife und Zusatzpakete
    club = club or ensure_default_club()
    with use_club(club):
        _seed_club(demo=False, preset=preset)
        if club.slug == current_app.config.get("DEFAULT_CLUB_SLUG", "mainpower"):
            _seed_admin()
        if demo:
            _seed_club(demo=True, preset=preset)
        admin = User.query.filter_by(role="superadmin").first()
        if admin and admin.profile and not admin.profile.embed_need:
            refresh_embeddings(admin.profile)


def create_club(slug: str, name: str, admin_email: str, admin_password: str, admin_first: str = "Admin",
                admin_last: str = "", domains: str = "", preset: str = "neutral", demo: bool = False,
                contact_email: str = "") -> Club:
    """Neuen Klub mit Vorlage, Leistungen und erstem Superadmin anlegen (Plattform-Konsole, CLI)."""
    from .club_presets import apply_preset
    from .services import club as club_settings
    club = Club(slug=slug, name=name, domains=domains, contact_email=contact_email or admin_email)
    db.session.add(club)
    db.session.commit()
    with use_club(club):
        apply_preset(preset, replace_faq=True)
        club_settings.save({"name": name, "full_name": name, "contact_email": contact_email or admin_email})
        db.session.commit()
        _seed_club(demo=demo, preset=preset)
        admin = User(email=admin_email.lower(), first_name=admin_first or "Admin", last_name=admin_last,
                     role="superadmin")
        admin.set_password(admin_password)
        admin.profile = Profile(headline=f"Organisation, {name}", company=name, industry="Community & Netzwerk")
        db.session.add(admin)
        db.session.commit()
    return club


def remove_demo() -> int:
    from .services.gdpr import delete_user
    users = User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).all()
    for u in users:
        delete_user(u)
    return len(users)
