"""Import der Termine aus dem Kalender von main-power.org (/api/events, Google-Kalender-basiert)."""
from __future__ import annotations

import logging
import re
from datetime import datetime

import requests
from flask import current_app

from ..extensions import db
from ..models import FORMATS, Event

log = logging.getLogger(__name__)
FORMAT_ALIASES = {"energie": "stammtisch"}
# Meeting-Links, Einwahlnummern und PINs gehören nicht in öffentliche Beschreibungen
_STRIP = re.compile(r"(Join with Google Meet|Or dial|More phone numbers|Learn more about Meet|meet\.google\.com|PIN:).*",
                    re.I | re.S)


def _clean(desc: str) -> str:
    desc = _STRIP.sub("", desc or "")
    desc = re.sub(r"<[^>]+>", "\n", desc)
    return re.sub(r"\n{2,}", "\n", desc).strip()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)


def sync_events() -> dict:
    url = current_app.config["EVENTS_SYNC_URL"]
    r = requests.get(url, timeout=15, headers={"User-Agent": "MainPowerPlatform/1.0"})
    r.raise_for_status()
    data = r.json()
    created = updated = 0
    for item in data.get("events", []):
        ext = str(item.get("id"))
        fmt = FORMAT_ALIASES.get(item.get("format", ""), item.get("format", "community"))
        if fmt not in FORMATS:
            fmt = "community"
        ev = Event.query.filter_by(external_id=ext).first()
        if ev is None:
            ev = Event(external_id=ext, source="sync", status="published")
            db.session.add(ev)
            created += 1
        else:
            updated += 1
        ev.format = fmt
        ev.title = item.get("title") or FORMATS[fmt]["name"]
        ev.description = _clean(item.get("description", "")) or FORMATS[fmt]["description"]
        ev.location = item.get("location") or ev.location or ""
        ev.starts_at = _parse(item["start"])
        ev.ends_at = _parse(item["end"]) if item.get("end") else None
        ev.all_day = bool(item.get("allDay"))
        ev.time_pending = bool(item.get("timePending"))
        ev.price_cents = int(round(float(item.get("price") or 0) * 100))
    db.session.commit()
    log.info("Event-Sync: %s neu, %s aktualisiert", created, updated)
    return {"created": created, "updated": updated}
