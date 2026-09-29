"""E-Mail-Versand über SMTP; ohne SMTP_HOST wird die Mail nur geloggt (lokale Entwicklung)."""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app, render_template

log = logging.getLogger(__name__)


def send_mail(to: str, subject: str, template: str, **ctx) -> bool:
    cfg = current_app.config
    body = render_template(f"email/{template}.txt", **ctx)
    if not cfg.get("SMTP_HOST"):
        log.warning("MAIL (nicht versendet, SMTP fehlt) an %s | %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    from email.utils import formataddr, parseaddr
    from . import club as club_settings
    club = club_settings.settings()
    _name, addr = parseaddr(cfg["SMTP_FROM"])
    msg["From"] = formataddr((club.get("name") or _name, addr)) if addr else cfg["SMTP_FROM"]
    if club.get("contact_email") and club["contact_email"] != addr:
        msg["Reply-To"] = club["contact_email"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=20) as s:
            if cfg.get("SMTP_TLS"):
                s.starttls()
            if cfg.get("SMTP_USER"):
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            s.send_message(msg)
        return True
    except Exception:
        log.exception("Mailversand fehlgeschlagen an %s", to)
        return False
