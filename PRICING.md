# KI-Kosten und Preismodell (Entwurf, Stand 26.09.2026)

Alle KI-Funktionen laufen mit Claude Haiku 4.5 (1 $ Eingabe / 5 $ Ausgabe je 1 Mio. Token). Zahlen unten sind **gemessen** auf
dem Live-System (Tabelle `llm_usage`), gerundet. Sonnet kostet ca. das Dreifache.

| Funktion | Ø Token je Aufruf (ein / aus) | Kosten je Nutzung |
|---|---|---|
| Aiko-Chat (eine Antwort) | 2.400–3.700 / 250–300 | ca. 0,004 $ |
| Profil-Coach | 1.100 / 350 | ca. 0,003 $ |
| Kontakt-Assistent (je Kontakt, gecacht) | 1.800 / 400 | ca. 0,004 $ |
| Termin-Einladungen (je Termin, alle Personen) | 3.000 / 360 | ca. 0,005 $ |
| SEO-/Compliance-/Sicherheits-Check (Bericht) | 2.700 / 1.500 | ca. 0,01 $ |
| **Markt-Panel, 100 Personas** (inkl. Auswertung) | 207.000 / 37.000 | **ca. 0,39 $**, Dauer ca. 2 Minuten |
| Markt-Panel, 300 Personas | ca. 600.000 / 110.000 | ca. 1,2 $ |

## Vorschlag Tarife (Richtwerte, Netto)
| | Community (kostenlos) | Pro, 29 €/Monat | Business, 99 €/Monat |
|---|---|---|---|
| Aiko-Chat, Matching, Assistenten | Fair Use (z. B. 30 Chat-Nachrichten) | großzügig | unbegrenzt (Fair Use) |
| Checks (SEO, Compliance, Sicherheit) | 3 pro Monat | 20 pro Monat | 100 pro Monat |
| Markt-Panel | 1 Lauf mit 50 Personas | 5 Läufe à 100 Personas | 20 Läufe bis 300 Personas, optional Sonnet |
| Geschätzte KI-Kosten je Nutzer:in | unter 0,50 $ | ca. 2–3 $ | ca. 10–25 $ |

Einzelkauf (ohne Abo): Markt-Panel 100 Personas 19 €, 300 Personas 49 €; Check je 5 €. Marge über 90 %, weil die KI-Kosten bei
Haiku im Cent-Bereich liegen. Premium-Qualität (Sonnet) als Aufpreis anbieten.

## Umsetzung (noch offen)
1. `llm_usage.user_id` ergänzen, damit Kosten je Mitglied bekannt sind (derzeit nur je Zweck).
2. Kontingente je Tarif statt globaler Einstellung (`panel_monthly_limit` ist der Vorläufer).
3. Stripe-Abos und Einzelkäufe (bewusst zurückgestellt bis nach der Demo).
