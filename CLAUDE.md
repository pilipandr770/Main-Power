# CLAUDE.md — Main Power Plattform

Kontext für Claude Code. Auftraggeber: Main Power Community (Asset Beissenov), Frankfurt. Umsetzung: Andrii-IT.
Kommunikation mit dem Entwickler: Russisch. UI-Texte, Kommentare im Code und Commit-Messages: Deutsch.

## Befehle
- `flask init-db && flask seed` — lokale DB mit Demo-Daten (SQLite `instance/mainpower.db`)
- `flask run` — Dev-Server; `pytest -q` — Smoke-Tests (offline)
- `flask sync-events`, `flask reembed`, `flask remove-demo`, `flask create-admin <email>`
- `flask telegram-poll` (lokal) / `flask telegram-set-webhook` (Prod)

## Architektur
- Flask App-Factory (`app/__init__.py`), Blueprints: `public`, `auth`, `member` (`/app`), `admin` (`/admin`), `webhooks`.
- Kein JS-Framework. Serverseitige Jinja-Templates + `static/js/app.js` (Chat, Navigation, Bestätigungen).
- Formulare ohne WTForms-Klassen: `request.form` + manuelle Validierung; CSRF via Flask-WTF (`csrf_token()` in jedem POST-Formular, `X-CSRFToken` bei fetch). Webhooks und `/api/aiko` (öffentlich, zustandslos) sind CSRF-exempt.
- Zeiten in der DB: naive UTC. Anzeige via Filter `|local`, `|dt`, `|event_date` (Europe/Berlin). Eingaben mit `utils.local_to_utc`.

## Konventionen (wichtig)
- **Keine Inline-Styles und keine Inline-Skripte** — die CSP (`style-src 'self'; script-src 'self'`) blockiert sie. Nur Klassen aus `static/css/app.css` (Utilities: `mt-s/m/l`, `mb-0/m/l`, `w-0…w-100`, `h-0…h-100`, `maxw`, `split`, `pre`, `nowrap`, …).
- Keine Google Fonts / externen CDNs (DSGVO). Systemschrift wie auf main-power.org.
- Markenfarben als CSS-Variablen: `--night #0c0a09`, `--bone #f4eee9`, `--ember #fe4716`, `--ash #aaa19b`, `--cream #f0e9e2`.
- Tonalität: du-Form, ruhig, hochwertig, keine Pfeile in Buttons, keine GROSSBUCHSTABEN-Labels.
- Jede Admin-Aktion mit `audit(...)` protokollieren. Einwilligungen nur über `record_consent` (append-only).
- Nutzer-URLs immer durch `utils.clean_url` (verhindert `javascript:`).
- Kontaktdaten anderer Mitglieder nur nach `IntroRequest.status == "accepted"` anzeigen. Aiko darf sie nie ausgeben.

## Datenschutz-Designentscheidungen
- Kein Scraping sozialer Netzwerke. Profil-Links werden nur gespeichert und angenommenen Kontakten gezeigt.
- Matching und Verzeichnis-Sichtbarkeit sind getrennte, optionale Einwilligungen (Opt-in, standardmäßig aus).
- Öffentliche Aiko speichert serverseitig nichts (Verlauf nur in sessionStorage des Browsers).
- Mitglieder-Chatverlauf mit Aiko löschbar; Datenexport/Löschung self-service (`services/gdpr.py`).
- `events_sync` entfernt Google-Meet-Links/Einwahl-PINs aus öffentlichen Beschreibungen; Online-Links nur im Feld `online_url`, sichtbar für Angemeldete.

## Matching
`score(A→B) = 0.6·cos(need_A, offer_B) + 0.4·cos(need_B, offer_A)`. Embeddings als JSON in `profiles.embed_need/offer`, Vergleich in numpy. Begründungen per LLM (ein Batch-Call für bis zu k Kandidaten), gecacht in `matches` mit `profile_version`-Hash; Fallback-Template ohne API-Key. Anzeige für Mitglieder als Label (`|fit`), Rohwerte nur im Admin.

### Fragebogen und hybrides Matching
- Auswahllisten, Sichtbarkeit und `structured_fit` in `app/questionnaire.py`; neue Profilspalten in `Profile` (Blöcke
  A–F: Wer du bist, Ziel, Wen du suchst, Was du gibst, Zusammenarbeit, Kennenlernen). Alles optional, kein Umsatz,
  keine Art.-9-Daten.
- Score = 0.65 · semantisch (kalibriert je Embedding-Anbieter auf 0–1) + 0.35 · strukturiert. „Nicht kontaktieren zu“
  (`not_wanted`, eigenes Embedding `embed_avoid`) schließt sehr ähnliche Angebote in beide Richtungen aus, mittlere
  Ähnlichkeit wertet nur ab.
- Privat per Standard: Ziel, Meilenstein, Herausforderung, „Schon versucht“ (`public_fields` = Opt-in),
  `not_wanted` immer. Andere Mitglieder sehen sie weder im Profil noch in KI-Texten: Karten für die KI immer über
  `matching.card(p)`; `own=True` nur, wenn die Antwort an die Person selbst geht.
- Profil-Interview (`services/interview.py`, `/app/profil/interview`): Aiko fragt nach Priorität, KI schlägt Werte
  vor, gespeichert wird nur Bestätigtes über `questionnaire.apply_values` (prüft Optionen und Längen).
- Ziel-Check-ins (`services/goals.py`, `/app/ziel`): Meilenstein startet 90-Tage-Zeitraum (`milestone_set_at`);
  `flask goal-checkins` (Cron täglich) erinnert einmal pro Zeitraum; `GoalCheckin` mit Aiko-Feedback, nur für die Person.
- Klubeigene Fragen (`ClubQuestion`, Admin „Eigene Fragen“, max. 8): Antworten in `Profile.custom_answers`, optional als
  Bedarf/Angebot im Matching, im Klub-Export enthalten.
- Auswertung für die Klubleitung (`services/club_insights.py`, `/admin/auswertung`): nur Summen und sichtbare Texte
  (anonymisiert, gemischt), keine privaten Felder; Terminideen verlinken auf ein vorbelegtes neues Event.

### Skalierung
Bis ~20k Profile reicht In-Memory. Danach: `pgvector` (`CREATE EXTENSION vector`), Spalten `vector(1024)`, HNSW-Index, `top_matches`/`semantic_search` auf SQL (`<=>`) umstellen. Region (`profiles.region`) ist schon vorhanden → Mandanten/Regionen später per Filter bzw. eigener `communities`-Tabelle.

## Mehrere Klubs (Mandanten, Branch `white-label`)
- `app/tenancy.py`: alle Tabellen mit `TenantMixin` (`club_id`). Pro Request wird der Klub aus dem Host bestimmt
  (`Club.domains` oder `<slug>.<PLATFORM_DOMAIN>`, sonst Standardklub). Ein Session-Event filtert JEDE ORM-Abfrage
  automatisch auf den aktuellen Klub; neue Objekte bekommen `club_id` beim Flush. Klubübergreifend nur bewusst mit
  `.execution_options(all_clubs=True)`. Hintergrund-Threads/CLI: `with use_club(club): ...`.
- Branding/Texte/Rechtliches je Klub: `services/club.py` (`club.<feld>` in Templates, Settings `club.*`), Formate in
  `meeting_formats` (`FORMATS` ist ein DB-Register). Vorlagen `club_presets.py` (mainpower, neutral), Export/Import JSON.
- Admin: „Klub & Branding“, „Formate“ (nur Superadmin). Betreiber-Konsole `/plattform` (eigene Env-Anmeldung).
- `flask init-db` migriert eine Einzelklub-DB idempotent (`migrations_mt.py`). `flask create-club`, `flask list-clubs`,
  CLI-Befehle mit `--club <slug>`.
- Nicht je Klub: Telegram-Bot, SMTP, Stripe, Anthropic-Key (plattformweit). KI-Verbrauch wird je Klub gezählt.
- Nie Texte aus dem vertraulichen Main-Power-Konzept in andere Presets übernehmen.

## Roadmap nach dem MVP (mit Andrii abgestimmt, 26.09.2026)
Bewusst zurückgestellt, damit MVP und Demo schlank bleiben: Passwort-Reset und E-Mail-Verifizierung (SMTP), 2FA, Stripe,
Sicherheitsrunde (White-Team-Audit), Cybersecurity-Angebote (automatisierter Blackbox-Pentest-Service, allgemeine Lektionen).
Ohne SMTP_HOST wird der Link „Passwort vergessen“ im Login ausgeblendet.

## Offene Punkte / TODO
- [ ] Tarife/Mitgliedschaften (Stripe Subscriptions) — nach Termin mit Asset am 28.09. klären; aktuell nur Ticketzahlung pro Termin.
- [ ] Erstattungen bei Abmeldung bezahlter Termine (derzeit manuell).
- [ ] Flask-Migrate initialisieren (`flask db init && flask db migrate -m init`) sobald das Schema stabil ist.
- [ ] E-Mail-Verifizierung bei Registrierung (Token-Infrastruktur wie bei Passwort-Reset vorhanden).
- [ ] Erinnerungs-Mails/Telegram-Nachrichten vor Terminen + „Wen du beim Hub treffen solltest“-Briefing an Teilnehmende.
- [ ] Datenschutzhinweise juristisch finalisieren; DSFA (Art. 35) für das Matching prüfen.
- [ ] Parsing öffentlicher Profile: bewusst NICHT umgesetzt (ToS, Art. 14 DSGVO). Falls gewünscht, nur mit ausdrücklicher Einwilligung und offiziellen APIs.
- [ ] Rate-Limit-Storage auf Redis bei mehreren Workern.
