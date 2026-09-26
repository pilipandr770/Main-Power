# Demo-Konten (nur mit `flask seed`, danach `flask remove-demo`)

Passwort aller Demo-Konten: `demo-passwort-123` · E-Mail: `vorname.nachname@demo.main-power.local`
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
5. **Leistung „SEO-Check“ (Black Box)** — *Leistungen → SEO-Check für deine Website*: Adresse und Suchbegriffe eingeben (z. B. `www.main-power.org`, „Networking Frankfurt“), „Jetzt prüfen“: nach ca. 20 Sekunden Bericht von Aiko mit Score, priorisierten Maßnahmen und allen Prüfpunkten; „Drucken / als PDF speichern“ erzeugt eine saubere Druckansicht. Tobias hat bereits einen Beispielbericht zu main-power.org.
6. **Leistung „Compliance-Check“** — *Leistungen → Compliance-Check für deine Website*: Adresse eingeben, „Jetzt prüfen“ (ca. 30 Sekunden): Impressum, Datenschutzerklärung, Cookies/Tracking, Shop-Pflichten, Sicherheits-Header; Bericht von Aiko mit Dringlichkeit und Druckansicht. Hinweis: statische Prüfung, keine Rechtsberatung — steht im Bericht.
