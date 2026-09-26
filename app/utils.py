from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import abort
from flask_login import current_user

TZ = ZoneInfo("Europe/Berlin")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jan", "Feb", "März", "Apr", "Mai", "Juni", "Juli", "Aug", "Sep", "Okt", "Nov", "Dez"]
MONTHS_LONG = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
               "Oktober", "November", "Dezember"]


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(TZ)


def local_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def fmt_event_date(event) -> str:
    d = to_local(event.starts_at)
    base = f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS_LONG[d.month - 1]} {d.year}"
    if event.all_day or event.time_pending:
        return base + (" · Uhrzeit folgt" if event.time_pending else "")
    return base + f" · {d:%H:%M} Uhr"


def fmt_dt(dt: datetime | None) -> str:
    d = to_local(dt)
    return f"{d:%d.%m.%Y %H:%M}" if d else "—"


def money(cents: int | None) -> str:
    cents = cents or 0
    return "kostenlos" if cents == 0 else (f"{cents / 100:.2f} €".replace(".", ",").replace(",00 €", " €"))


def clean_url(url: str) -> str:
    """Nur http(s)-Links zulassen (verhindert javascript:-URLs in Profilen)."""
    url = (url or "").strip()
    if not url:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I) and not re.match(r"^https?://", url, re.I):
        return ""  # javascript:, data:, mailto: …
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or " " in url or \
            not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(:\d+)?", (p.netloc or "").lower()):
        return ""
    return url[:300]


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def superadmin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user.is_authenticated or not current_user.is_superadmin:
            abort(403)
        return fn(*a, **kw)
    return wrapper


def clean_social(key: str, value: str) -> str:
    """Prüft einen Social-Link gegen die erlaubten Domains des Netzwerks; leer = ungültig/entfernt."""
    from .models import SOCIALS
    value = (value or "").strip()
    if not value:
        return ""
    if key == "telegram" and re.fullmatch(r"@?[A-Za-z0-9_]{4,32}", value):
        return "https://t.me/" + value.lstrip("@")
    if key in ("instagram", "x", "tiktok", "github") and re.fullmatch(r"@[A-Za-z0-9_.-]{2,40}", value):
        base = {"instagram": "https://www.instagram.com/", "x": "https://x.com/", "tiktok": "https://www.tiktok.com/@",
                "github": "https://github.com/"}[key]
        return base + value.lstrip("@")
    if "://" not in value and re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", value, re.I):
        value = "https://" + value
    url = clean_url(value)
    hosts = SOCIALS[key][2]
    if url and hosts:
        host = (urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) for h in hosts):
            return ""
    return url
