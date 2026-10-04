# Demo-Konten (nur mit `flask seed`, danach `flask remove-demo`)

Passwort aller Demo-Konten: `demo-passwort-123` · E-Mail: `vorname.nachname@demo.klub.local`
Superadmin: siehe `ADMIN_EMAIL` / `ADMIN_PASSWORD` in `.env`.

| Konto | Zweck in der Vorführung |
|---|---|
| tobias.kern | Gründer (SaaS) – gute Matches, hat angenommene Anfrage von/zu Julia und offene von Leonie |
| julia.wagner | Steuerberaterin – Top-Match für „Steuerberater gesucht“ |
| markus.albrecht | Fachanwalt Arbeitsrecht – offene Anfrage von Ivan |
| ivan.kovalenko | Cybersecurity-Berater – bezahlte Hub-Anmeldung, hat ein Treffen zur Freigabe eingereicht |
| mariam.haddad / hannah.vogel | Anmeldung bestätigt / reserviert |
| petra.lang | Matching und Verzeichnis abgelehnt – erscheint nirgends (Opt-in-Demo), Anmeldung storniert |
| felix.baumann | Neues Konto mit leerem Profil (Onboarding-Demo) |
| oleg.petrenko | Gesperrtes Konto |
| lisa.team | Rolle admin (Moderation, kann nicht löschen) |
| + 9 weitere (Marketing, Export, Logistik, Coaching, Foto, Finanzierung, Gastro, UX, Versicherung) | Breite Datenbasis fürs Matching |

Admin: Mitglieder → Person → **Bearbeiten** / Sperren / Löschen (Löschen nur Superadmin), Übersicht → **KI-Verbrauch**.

## Vorführung der KI-Funktionen (ca. 10 Minuten)
1. **Profil ausfüllen mit Anleitung** — als `felix.baumann` anmelden (leeres Profil): unter *Mein Profil* „Wozu diese Frage?“ aufklappen, Beispiel per Klick übernehmen, „Feedback holen“ (Aiko-Coach).
2. **Community und Kontaktseite** — *Community*: 3D-Netz „Dein Netz“, Neu-Karten, Termine mit Gesichtern. Person anklicken: „Was bringt euch dieser Kontakt?“ mit Nutzen für beide Seiten, Nachrichtenentwurf („In meine Anfrage übernehmen“) und passendem Termin.
3. **Automatische Einladung** — als Superadmin *Termine → „Themen-Frühstück: Cybersecurity im Mittelstand“ → Freigeben*: Aiko lädt passende Mitglieder mit persönlicher Begründung ein. Danach als `stefan.richter` anmelden: Übersicht zeigt „Aiko lädt dich ein“. Vorschau ohne Versand: *Termin → KI-Einladungen*.
4. **Organisation** — Admin-Übersicht: *Matching-Karte* (alle Profile, 3D) und *KI-Verbrauch* (Tokens/Kosten).
5. **Leistung „SEO-Check“ (Black Box)** — *Leistungen → SEO-Check für deine Website*: Adresse und Suchbegriffe eingeben (z. B. die Website eines Mitglieds, „Networking Musterstadt“), „Jetzt prüfen“: nach ca. 20 Sekunden Bericht von Aiko mit Score, priorisierten Maßnahmen und allen Prüfpunkten; „Drucken / als PDF speichern“ erzeugt eine saubere Druckansicht. Tobias hat bereits einen Beispielbericht.
6. **Leistung „Compliance-Check“** — *Leistungen → Compliance-Check für deine Website*: Adresse eingeben, „Jetzt prüfen“ (ca. 30 Sekunden): Impressum, Datenschutzerklärung, Cookies/Tracking, Shop-Pflichten, Sicherheits-Header; Bericht von Aiko mit Dringlichkeit und Druckansicht. Hinweis: statische Prüfung, keine Rechtsberatung — steht im Bericht.
7. **Leistung „Sicherheits-Check (passiv)“** — *Leistungen → Sicherheits-Check*: Domain eingeben (z. B. `andrii-it.de`), „Jetzt prüfen“ (ca. 20 Sekunden): TLS/Zertifikat, Sicherheits-Header, Cookies, SPF/DMARC/DKIM, DNSSEC, security.txt; Bericht von Aiko mit „wer setzt es um“ (Hoster, Mailanbieter, Agentur). Ausdrücklich passiv und kein Penetrationstest — der Bericht verweist für Tiefe auf einen Pentest (Verbindung zum späteren NIS2-Audit-Angebot).
8. **Leistung „Markt-Panel“** — *Leistungen → Markt-Panel*: Produkt beschreiben (z. B. ein Frühstücks-Abo), Preis A und B (z. B. 20 und 49 €), 100 Personas, Rhein-Main oder Deutschland → „Panel starten“. Fortschrittsbalken (ca. 2 Minuten), danach Kaufabsicht, Kaufwahrscheinlichkeit, Preiskurve, A/B-Vergleich, Segmente nach Alter/Einkommen/Technik/Gesundheit, Bedenken- und Nutzen-Themen mit exakten Zählungen, Empfehlungen und alle 100 Personas mit Steckbrief. Überall der Hinweis: **synthetisch, keine echten Menschen**. Ein fertiger Beispiellauf liegt bei Tobias.
9. **Leistung „Gesetzes-Suche“** — *Leistungen → Gesetzes-Suche*: Frage eingeben, z. B. „Mietminderung wegen Schimmel in der Wohnung“, Rechtsgebiet „Zivilrecht“ → „Gesetze suchen“ (ca. 10–15 Sekunden). Zeigt § 536 BGB **wörtlich zitiert** (amtlicher Text, keine KI-Erfindung) plus Erläuterung von Aiko, was der Paragraf bedeutet — nie eine Handlungsempfehlung. Durchsucht 204.978 Paragraphen aus dem kompletten deutschen Bundesrecht (gesetze-im-internet.de). Prominenter Hinweis oben und unten: **keine Rechtsberatung**. Tobias hat bereits zwei Beispielanfragen im Verlauf, eine davon zeigt, wie Aiko ehrlich sagt, wenn die Datenbank keine wirklich passende Norm findet.
