"""Betroffenenrechte: Auskunft/Datenübertragbarkeit (Art. 15/20) und Löschung (Art. 17)."""
from __future__ import annotations

from .. import questionnaire
from ..extensions import db
from ..models import (ChatMessage, GoalCheckin, IntroRequest, LawQuery, Match, Notification, PanelRun, SeoReport,
                      ServiceInquiry, User)
from ..utils import fmt_dt
from . import media


def export_user(user: User) -> dict:
    p = user.profile
    return {
        "konto": {"email": user.email, "vorname": user.first_name, "nachname": user.last_name, "telefon": user.phone,
                  "rolle": user.role, "status": user.status, "erstellt": fmt_dt(user.created_at),
                  "letzter_login": fmt_dt(user.last_login_at), "telegram": user.telegram_username},
        "profil": None if not p else {
            k: getattr(p, k) for k in ("headline", "company", "industry", "city", "region", "bio", "q_focus",
                                       "q_challenge", "q_can_help", "q_looking_for", "expertise",
                                       "preferred_formats", "linkedin_url", "xing_url", "instagram_url",
                                       "website_url", "facebook_url", "telegram_url", "x_url", "youtube_url", "github_url",
                                       "tiktok_url", "socials_public", "event_invites", "photo", "visible_in_directory",
                                       "allow_matching", *questionnaire.CHOICE_FIELDS, *questionnaire.TEXT_FIELDS,
                                       "public_fields", "custom_answers")},
        "ziel_check_ins": [{"meilenstein": c.milestone, "status": c.status_label, "notiz": c.note,
                            "naechster_meilenstein": c.next_milestone, "aiko": c.ai_feedback, "zeit": fmt_dt(c.created_at)}
                           for c in GoalCheckin.query.filter_by(user_id=user.id).order_by(GoalCheckin.created_at)],
        "einwilligungen": [{"art": c.kind, "erteilt": c.granted, "version": c.version, "zeit": fmt_dt(c.created_at)}
                           for c in user.consents],
        "anmeldungen": [{"termin": r.event.title, "status": r.status, "zeit": fmt_dt(r.created_at)}
                        for r in user.registrations],
        "kontaktanfragen": [{"von": i.from_user_id, "an": i.to_user_id, "status": i.status, "nachricht": i.message,
                             "zeit": fmt_dt(i.created_at)}
                            for i in IntroRequest.query.filter((IntroRequest.from_user_id == user.id) |
                                                               (IntroRequest.to_user_id == user.id))],
        "aiko_chat": [{"rolle": m.role, "text": m.content, "kanal": m.channel, "zeit": fmt_dt(m.created_at)}
                      for m in ChatMessage.query.filter_by(user_id=user.id).order_by(ChatMessage.created_at)],
        "seo_berichte": [{"url": r.url, "keywords": r.keywords, "score": r.score, "zeit": fmt_dt(r.created_at)}
                         for r in SeoReport.query.filter_by(user_id=user.id)],
        "markt_panel": [{"produkt": r.product_name, "beschreibung": r.description, "personas": r.n_requested, "status": r.status,
                         "zeit": fmt_dt(r.created_at)} for r in PanelRun.query.filter_by(user_id=user.id)],
        "gesetzes_suche": [{"frage": q.question, "kategorie": q.category, "treffer": len(q.hits or []), "zeit": fmt_dt(q.created_at)}
                          for q in LawQuery.query.filter_by(user_id=user.id)],
        "anfragen_leistungen": [{"leistung": s.service.title, "nachricht": s.message, "zeit": fmt_dt(s.created_at)}
                                for s in ServiceInquiry.query.filter_by(user_id=user.id)],
        "hinweis": "Berechnete Matching-Vektoren (Embeddings) sind abgeleitete technische Daten und werden bei "
                   "Löschung ebenfalls entfernt.",
    }


def delete_user(user: User) -> None:
    uid = user.id
    Match.query.filter((Match.user_id == uid) | (Match.other_id == uid)).delete(synchronize_session=False)
    IntroRequest.query.filter((IntroRequest.from_user_id == uid) |
                              (IntroRequest.to_user_id == uid)).delete(synchronize_session=False)
    ChatMessage.query.filter_by(user_id=uid).delete(synchronize_session=False)
    GoalCheckin.query.filter_by(user_id=uid).delete(synchronize_session=False)
    Notification.query.filter_by(user_id=uid).delete(synchronize_session=False)
    SeoReport.query.filter_by(user_id=uid).delete(synchronize_session=False)
    for run in PanelRun.query.filter_by(user_id=uid):
        db.session.delete(run)
    LawQuery.query.filter_by(user_id=uid).delete(synchronize_session=False)
    ServiceInquiry.query.filter_by(user_id=uid).delete(synchronize_session=False)
    if user.profile and user.profile.photo:
        media.delete_avatar(user.profile.photo)
    db.session.delete(user)  # Profil, Einwilligungen, Anmeldungen via cascade
    db.session.commit()
