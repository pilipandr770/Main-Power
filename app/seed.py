"""Startdaten: Inhalte von main-power.org, Leistungen, Beispieltermine und (optional) Demo-Mitglieder.

Demo-Mitglieder haben E-Mails @demo.main-power.local und lassen sich mit `flask remove-demo` entfernen.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from .extensions import db
from .models import Consent, Event, KnowledgeItem, Profile, Service, User, utcnow
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
     "Du beantwortest in deinem Profil vier Fragen: dein Tätigkeitsbereich, deine größte Herausforderung, womit du "
     "anderen helfen kannst und wonach du konkret suchst. Die KI vergleicht Bedarf und Angebot inhaltlich — nicht "
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
         price_hint="Festpreis nach Erstgespräch", provider_name="Andrii-IT · Partner von Main Power",
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
         price_hint="ab Pauschalpreis, je nach Umfang", provider_name="Andrii-IT · Partner von Main Power",
         provider_email="info@andrii-it.de"),
    dict(slug="nis2-dsgvo-compliance", sort=3, title="NIS2- und DSGVO-Check",
         summary="Bist du von NIS2 betroffen und was musst du konkret tun? Betroffenheitsprüfung, Lückenanalyse "
                 "und Maßnahmenplan für Geschäftsführung und Team.",
         description="Das NIS2-Umsetzungsgesetz erweitert den Kreis der regulierten Unternehmen deutlich — und "
                     "nimmt die Geschäftsführung persönlich in die Pflicht. Wir klären Betroffenheit, bewerten den "
                     "Ist-Stand und erstellen einen umsetzbaren Plan.",
         benefits="Klarheit über Betroffenheit und Fristen\nDokumentation für Prüfungen\nSchulung der Geschäftsführung",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="nach Aufwand", provider_name="Andrii-IT · Partner von Main Power",
         provider_email="info@andrii-it.de"),
    dict(slug="ki-verordnung", sort=4, title="KI im Unternehmen — rechtssicher einführen",
         summary="Du nutzt ChatGPT, Chatbots oder KI-Tools? Wir prüfen, welche Pflichten aus KI-Verordnung und "
                 "DSGVO für dich gelten, und helfen bei Richtlinien und Schulung.",
         description="Seit 2025 gilt die Pflicht zur KI-Kompetenz für Unternehmen, die KI einsetzen, seit August "
                     "2026 die Transparenzpflichten für Chatbots. Wir machen eine Bestandsaufnahme, ordnen Risiken ein "
                     "und liefern Nutzungsrichtlinie und Schulung.",
         benefits="Überblick über eingesetzte KI-Tools\nNutzungsrichtlinie für dein Team\nNachweis der KI-Kompetenz",
         member_benefit="Kostenloses Erstgespräch für Community-Mitglieder",
         price_hint="Paketpreis", provider_name="Andrii-IT · Partner von Main Power",
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
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="Main Power · Aiko",
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
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="Main Power · Aiko",
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
    member_benefit="Für Mitglieder kostenlos", price_hint="Kostenlos für Mitglieder", provider_name="Main Power · Aiko",
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
    provider_name="Main Power · Aiko", provider_email="")

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


def _seed_content() -> None:
    if KnowledgeItem.query.count() == 0:
        for i, (q, a, public) in enumerate(FAQ):
            db.session.add(KnowledgeItem(question=q, answer=a, public=public, sort=i))
    for s in SERVICES + [PANEL_SERVICE, SEO_SERVICE, COMPLIANCE_SERVICE, SECURITY_SERVICE]:
        if not Service.query.filter_by(slug=s["slug"]).first():
            db.session.add(Service(**s))
    db.session.commit()


def _seed_events() -> None:
    try:
        from .services.events_sync import sync_events
        res = sync_events()
        log.info("Termine von main-power.org importiert: %s", res)
    except Exception as exc:
        log.warning("Termin-Sync nicht möglich (%s) — lege Beispieltermine an", exc)
    if Event.query.filter(Event.starts_at >= utcnow()).count() == 0:
        base = datetime.utcnow().replace(hour=7, minute=0, second=0, microsecond=0)
        samples = [
            ("hub", "Main Power Hub", 12, 2500, "Hotel in Frankfurt (wird bekanntgegeben)"),
            ("stammtisch", "Main Power Stammtisch", 5, 0, "Frankfurt am Main"),
            ("laufen", "Main Power Laufen", 8, 0, "Treffpunkt Mainufer"),
            ("frauenkreis", "Main Power Frauenkreis", 15, 0, "Frankfurt am Main"),
        ]
        from .models import FORMATS
        for fmt, title, days, price, loc in samples:
            db.session.add(Event(format=fmt, title=title, description=FORMATS[fmt]["description"],
                                 starts_at=base + timedelta(days=days), price_cents=price, location=loc,
                                 status="published", source="admin", capacity=24 if fmt == "hub" else None))
        db.session.commit()


def _seed_admin() -> None:
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
                            preferred_formats="hub,stammtisch", allow_matching=True, visible_in_directory=True,
                            bio=f"{first} ist Teil der Main Power Community (Demo-Profil).")
        db.session.add(u)
        for kind in ("privacy", "values", "matching", "directory"):
            db.session.add(Consent(user=u, kind=kind, granted=True, version="demo", source="seed"))
    db.session.commit()
    for p in Profile.query.join(User).filter(User.email.like(f"%@{DEMO_DOMAIN}")):
        refresh_embeddings(p, commit=False)
    db.session.commit()

    # Demo-Mitglieder zum nächsten Hub anmelden -> Matching-Konsole hat Daten
    from .models import Registration
    hub = Event.query.filter(Event.format == "hub", Event.starts_at >= utcnow()).order_by(Event.starts_at).first()
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
    ("Lisa", "Team", "Community-Managerin, Main Power", "Community",
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
                            preferred_formats="hub,stammtisch", allow_matching=matching_on,
                            visible_in_directory=matching_on and kind != "gesperrt",
                            bio=f"{first} ist Teil der Main Power Community (Demo-Profil)." if headline else "")
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
    hub = Event.query.filter(Event.format == "hub", Event.starts_at >= now).order_by(Event.starts_at).first()
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


def seed(demo: bool = True) -> None:
    db.create_all()
    _seed_content()
    _seed_events()
    _seed_admin()
    if demo:
        _seed_demo_members()
        _seed_demo_scenarios()
        _seed_demo_avatars()
    admin = User.query.filter_by(role="superadmin").first()
    if admin and admin.profile and not admin.profile.embed_need:
        refresh_embeddings(admin.profile)


def remove_demo() -> int:
    from .services.gdpr import delete_user
    users = User.query.filter(User.email.like(f"%@{DEMO_DOMAIN}")).all()
    for u in users:
        delete_user(u)
    return len(users)
