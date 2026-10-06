"""Einladungen: Links erzeugen, prüfen, einlösen."""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from flask import url_for

from ..extensions import db
from ..models import Invite, utcnow

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
MAX_BULK = 100


def find(token: str | None) -> Invite | None:
    """Gültige Einladung dieses Klubs (der Mandantenfilter greift automatisch) oder None."""
    token = (token or "").strip()
    if not token or len(token) > 48:
        return None
    inv = Invite.query.filter_by(token=token).first()
    return inv if inv and inv.valid else None


def create(email: str = "", note: str = "", days: int = 30, max_uses: int = 1, created_by_id: int | None = None) -> Invite:
    inv = Invite(token=secrets.token_urlsafe(18), email=email.strip().lower()[:255], note=note.strip()[:200],
                 created_by_id=created_by_id, max_uses=max(1, min(500, max_uses)),
                 expires_at=utcnow() + timedelta(days=max(1, min(365, days))) if days else None)
    db.session.add(inv)
    return inv


def link(inv: Invite) -> str:
    return url_for("auth.register", einladung=inv.token, _external=True)


def parse_emails(text: str) -> tuple[list[str], list[str]]:
    """(gültige, ungültige) Adressen aus einem Textfeld (Zeilen, Komma, Semikolon); doppelte entfallen."""
    good: list[str] = []
    bad: list[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        e = raw.strip().lower()
        if not e:
            continue
        if EMAIL_RE.fullmatch(e):
            if e not in good:
                good.append(e)
        else:
            bad.append(raw[:60])
    return good, bad
