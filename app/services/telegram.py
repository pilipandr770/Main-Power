"""Telegram als geschlossener Community-Chat + Aiko im privaten Bot-Chat.

Ablauf:
1. Mitglied klickt „Telegram verbinden“ → t.me/<bot>?start=<token>. Der Bot verknüpft telegram_user_id mit dem Konto.
2. Mitglied klickt „Community-Chat beitreten“ → Bot erzeugt einen persönlichen Einladungslink (1 Person, 24 h gültig).
3. Wächter: Tritt jemand der Gruppe bei, der nicht als aktives Mitglied verknüpft ist, wird er sofort entfernt.
   Wird ein Konto gesperrt/gelöscht, entfernt der Bot die Person aus der Gruppe.
4. Private Nachrichten an den Bot beantwortet Aiko mit dem Kontext des Mitglieds.

Einrichtung: Bot als Admin in die Gruppe aufnehmen (Rechte: Mitglieder einladen, Mitglieder sperren).
"""
from __future__ import annotations

import logging
import time

import requests
from flask import current_app

from ..extensions import db
from ..models import User

log = logging.getLogger(__name__)


def _c():
    from . import club as club_settings
    return club_settings.settings()


def enabled() -> bool:
    return bool(current_app.config.get("TELEGRAM_BOT_TOKEN"))


def group_enabled() -> bool:
    return enabled() and bool(current_app.config.get("TELEGRAM_GROUP_CHAT_ID"))


def api(method: str, **params):
    token = current_app.config["TELEGRAM_BOT_TOKEN"]
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=35)
    data = r.json()
    if not data.get("ok"):
        log.warning("Telegram %s fehlgeschlagen: %s", method, data)
        raise RuntimeError(data.get("description", "Telegram-Fehler"))
    return data["result"]


def send(chat_id: int | str, text: str) -> None:
    try:
        api("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)
    except Exception:
        log.exception("sendMessage fehlgeschlagen")


def deep_link(user: User) -> str:
    token = user.ensure_telegram_token()
    db.session.commit()
    return f"https://t.me/{current_app.config['TELEGRAM_BOT_USERNAME']}?start={token}"


def create_invite(user: User) -> str:
    res = api("createChatInviteLink", chat_id=current_app.config["TELEGRAM_GROUP_CHAT_ID"],
              name=f"member-{user.id}", member_limit=1, expire_date=int(time.time()) + 86400)
    return res["invite_link"]


def kick(telegram_user_id: int) -> None:
    if not (group_enabled() and telegram_user_id):
        return
    gid = current_app.config["TELEGRAM_GROUP_CHAT_ID"]
    try:
        api("banChatMember", chat_id=gid, user_id=telegram_user_id, until_date=int(time.time()) + 60)
        api("unbanChatMember", chat_id=gid, user_id=telegram_user_id, only_if_banned=True)
    except Exception:
        log.exception("Kick fehlgeschlagen")


def announce(text: str) -> None:
    api("sendMessage", chat_id=current_app.config["TELEGRAM_GROUP_CHAT_ID"], text=text[:4000],
        disable_web_page_preview=True)


def _member_for(tg_id: int) -> User | None:
    return User.query.filter_by(telegram_user_id=tg_id, status="active").first()


def handle_update(update: dict) -> None:
    from .aiko import answer_member

    base = current_app.config["BASE_URL"]
    group_id = str(current_app.config.get("TELEGRAM_GROUP_CHAT_ID") or "")

    # Gruppenwächter
    cm = update.get("chat_member")
    if cm and str(cm.get("chat", {}).get("id")) == group_id:
        new = cm.get("new_chat_member", {})
        if new.get("status") in ("member", "restricted"):
            tg_id = new.get("user", {}).get("id")
            if tg_id and not _member_for(tg_id):
                log.info("Nicht verknüpfte Person %s aus Gruppe entfernt", tg_id)
                kick(tg_id)
        return

    msg = update.get("message") or {}
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    tg_user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    if not text:
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        user = User.query.filter_by(telegram_link_token=token).first() if token else None
        if not user:
            send(chat_id, f"Hallo! Ich bin {_c()['assistant_name']}, die KI-Assistentin von {_c()['name']}. Verbinde bitte zuerst dein Konto "
                          f"über {base}/app/community — dann kann ich dich persönlich begleiten.")
            return
        other = User.query.filter_by(telegram_user_id=tg_user.get("id")).first()
        if other and other.id != user.id:
            other.telegram_user_id = None
        user.telegram_user_id = tg_user.get("id")
        user.telegram_username = tg_user.get("username") or ""
        user.telegram_link_token = None
        db.session.commit()
        send(chat_id, f"Willkommen, {user.first_name}! Dein Konto ist verbunden. Den Community-Chat kannst du jetzt "
                      f"unter {base}/app/community betreten. Und hier kannst du mir jederzeit schreiben — "
                      f"ich bin {_c()['assistant_name']}, eine KI, und kenne dein Profil bei {_c()['name']}.")
        return

    user = _member_for(tg_user.get("id"))
    if not user:
        send(chat_id, f"Bitte verbinde zuerst dein Konto bei {_c()['name']}: {base}/app/community")
        return
    if text in ("/hilfe", "/help"):
        send(chat_id, "Frag mich z. B.: „Wen sollte ich beim nächsten Treffen kennenlernen?“ oder "
                      "„Welche Termine passen zu mir?“")
        return
    reply = answer_member(user, text, channel="telegram")
    send(chat_id, reply.replace("**", "").replace("_(", "(").replace(")_", ")"))


def poll_forever() -> None:  # nur lokal (flask telegram-poll)
    offset = None
    api("deleteWebhook")
    log.warning("Telegram Long-Polling gestartet (Strg+C zum Beenden)")
    while True:
        try:
            updates = api("getUpdates", timeout=30, offset=offset,
                          allowed_updates=["message", "chat_member"])
            for u in updates:
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception:
                    log.exception("Update-Fehler")
                    db.session.rollback()
        except Exception:
            log.exception("Polling-Fehler")
            time.sleep(3)
