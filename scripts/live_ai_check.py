"""Live-Check der KI-Pfade mit echtem Modell auf einer Wegwerf-SQLite-DB mit Demo-Daten (keine Produktionsdaten).

Aufruf (ANTHROPIC_API_KEY in der Umgebung oder in .env):  python scripts/live_ai_check.py
Prüft Interview, Check-in-Feedback, Matching-Begründungen, Kontakt-Assistent, Profil-Coach und Klub-Auswertung –
inklusive Kontrolle, dass private Angaben anderer Mitglieder in keiner KI-Antwort auftauchen. Kosten: wenige Cent.
"""
import json
import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp(prefix="mp-ai-check-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_tmp, "live.db").replace(os.sep, "/")
os.environ.setdefault("SECRET_KEY", "live-check-only")
os.environ["EVENTS_SYNC_URL"] = ""

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import LLMUsage, User  # noqa: E402
from app.tenancy import default_club, use_club  # noqa: E402

app = create_app()
app.config["SERVER_NAME"] = "live-check.local"
DEMO = "demo.main-power.local"
problems = []


def show(title, value):
    print(f"\n=== {title}")
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
    print(textwrap.shorten(text, 1600, placeholder=" …") if len(text) > 1600 else text)


def must_not_contain(label, text, secrets):
    for s in secrets:
        if s and s[:25] in text:
            problems.append(f"{label}: enthält private Angabe „{s[:40]}…“")


if not app.config.get("ANTHROPIC_API_KEY"):
    sys.exit("ANTHROPIC_API_KEY fehlt (Umgebung oder .env) – ohne Schlüssel gibt es nichts live zu prüfen.")

with app.app_context():
    db.drop_all()
    from app.seed import seed
    seed(demo=True)
    with use_club(default_club()), app.test_request_context():
        print("Modell:", app.config.get("ANTHROPIC_MODEL"))
        by = lambda first: User.query.filter(User.email.like(f"{first}.%@{DEMO}")).one()  # noqa: E731
        tobias, julia, felix = by("tobias"), by("julia"), by("felix")
        private_tobias = [tobias.profile.q_tried, tobias.profile.milestone_90d, tobias.profile.not_wanted,
                          tobias.profile.q_challenge]

        # 1) Interview: mehrere Felder aus einer Antwort
        from app.services import interview
        props, ai = interview.extract(felix.profile, "q_focus",
                                      "Ich habe vor einem Jahr eine Firma gegründet, wir bauen eine App, die "
                                      "Handwerksbetriebe mit Azubis zusammenbringt. Gerade suche ich eine Investorin "
                                      "und jemanden mit Vertriebserfahrung. Ich spreche Deutsch und Ukrainisch.")
        show(f"Interview (KI={ai})", [{p['field']: p['display']} for p in props])
        if not ai or len(props) < 3:
            problems.append("Interview: KI hat weniger als 3 Felder erkannt")

        # 2) Ziel-Check-in mit Aiko-Feedback
        from app.services import goals
        c = goals.record_checkin(tobias, "haengt", "Pitch-Deck steht, aber erst zwei Gespräche mit Angels.")
        show("Check-in-Feedback", c.ai_feedback)
        if not c.ai_feedback or "Glückwunsch" in c.ai_feedback[:20]:
            problems.append("Check-in: kein oder unpassendes Feedback")

        # 3) Matching-Begründungen (Tobias' Sicht) – fremde private Angaben dürfen nicht vorkommen
        from app.models import Match
        from app.services import matching
        Match.query.filter_by(user_id=tobias.id).delete()
        db.session.commit()
        ms = matching.top_matches(tobias, k=3)
        show("Matching-Begründungen", [f"{m.other.first_name}: {m.reason} | {m.opener}" for m in ms])
        for m in ms:
            op = m.other.profile
            must_not_contain(f"Begründung {m.other.first_name}", m.reason + m.opener,
                             [op.q_tried, op.milestone_90d, op.not_wanted] +
                             ([op.goal_12m] if not op.is_public("goal_12m") else []))

        # 4) Kontakt-Assistent: Julia sieht Tobias – Tobias' private Angaben bleiben draußen
        from app.services import insights
        pi = insights.pair_insight(julia, tobias, force=True)
        show(f"Kontakt-Assistent Julia→Tobias (KI={pi['ai']})", pi)
        must_not_contain("Kontakt-Assistent", json.dumps(pi, ensure_ascii=False), private_tobias)

        # 5) Profil-Coach mit Ziel
        coach = insights.profile_coach({"q_focus": "IT", "goal_12m": "wachsen", "milestone_90d": "mehr Kunden"})
        show(f"Profil-Coach (KI={coach['ai']})", coach)

        # 6) Klub-Auswertung
        from app.services import club_insights
        rep = club_insights.build_report()
        show(f"Klub-Auswertung (KI={rep['ai']})", {k: rep[k] for k in ("nachfrage", "luecken", "ideen")})
        must_not_contain("Klub-Auswertung", json.dumps(rep, ensure_ascii=False), private_tobias)
        if not rep["ai"] or not rep["ideen"]:
            problems.append("Klub-Auswertung: keine KI-Ideen")

        usage = LLMUsage.query.all()
        print(f"\nToken: {sum(u.input_tokens for u in usage)} ein / {sum(u.output_tokens for u in usage)} aus, "
              f"{len(usage)} Aufrufe: {sorted({u.purpose for u in usage})}")
print("\nERGEBNIS:", "OK" if not problems else "\n".join(problems))
