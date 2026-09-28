"""Mandantenfähigkeit (mehrere Klubs auf einer Installation).

Prinzip: Jede mandantenbezogene Tabelle trägt `club_id` (TenantMixin). Solange im aktuellen Kontext ein Klub gesetzt
ist (`g.club_id`, pro Request aus dem Host ermittelt), hängt ein Session-Event an JEDE ORM-Abfrage automatisch
`WHERE club_id = <aktueller Klub>` — auch an Session.get, Beziehungen, Aggregate, Joins sowie Bulk-UPDATE/DELETE.
Neue Objekte bekommen den aktuellen Klub beim Flush. Dadurch kann Code ohne eigenes Zutun keine Daten eines
anderen Klubs lesen oder ändern.

Ohne gesetzten Klub (CLI, Migration, Plattform-Konsole, Tests) ist nichts gefiltert. Bewusst klubübergreifende
Abfragen im Request nutzen `.execution_options(all_clubs=True)`.
"""
from __future__ import annotations

from contextlib import contextmanager

from flask import current_app, g, has_app_context
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from .extensions import db


class TenantMixin:
    """Mixin für alle Tabellen, deren Zeilen genau einem Klub gehören."""
    club_id = db.Column(db.Integer, db.ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False, index=True)


def current_club_id() -> int | None:
    if not has_app_context():
        return None
    return g.get("club_id")


def current_club():
    from .models import Club
    cid = current_club_id()
    if cid is None:
        return None
    club = g.get("club_obj")
    if club is None or club.id != cid:
        club = db.session.get(Club, cid)
        g.club_obj = club
    return club


def _forget_tenant_objects() -> None:
    """Objekte des bisherigen Klubs aus der Session entfernen: Session.get() liest sonst aus der Identity-Map,
    ohne dass der Klub-Filter greift."""
    for obj in list(db.session.identity_map.values()):
        # expunge kaskadiert (User -> Profile), daher vorher prüfen, ob das Objekt noch in der Session ist
        if isinstance(obj, TenantMixin) and obj in db.session:
            db.session.expunge(obj)


def set_club(club) -> None:
    new_id = club.id if club is not None else None
    if g.get("club_id") != new_id:
        _forget_tenant_objects()
    g.club_id = new_id
    g.club_obj = club
    for key in ("club_settings", "_formats"):  # Caches des vorherigen Klubs verwerfen
        g.pop(key, None)


@contextmanager
def use_club(club):
    """Temporär in einem Klub arbeiten (CLI, Hintergrund-Threads, Seed)."""
    prev_id, prev_obj = g.get("club_id"), g.get("club_obj")
    set_club(club)
    try:
        yield club
    finally:
        if prev_id != (club.id if club is not None else None):
            _forget_tenant_objects()
        g.club_id, g.club_obj = prev_id, prev_obj
        for key in ("club_settings", "_formats"):
            g.pop(key, None)


def default_club():
    from .models import Club
    slug = current_app.config.get("DEFAULT_CLUB_SLUG", "mainpower")
    return (Club.query.execution_options(all_clubs=True).filter_by(slug=slug).first()
            or Club.query.execution_options(all_clubs=True).order_by(Club.id).first())


def club_for_host(host: str):
    """Klub zu einem Hostnamen: eigene Domain (Club.domains) oder Subdomain der Plattform-Domain."""
    from .models import Club
    host = (host or "").split(":")[0].lower().strip(".")
    if not host:
        return None
    clubs = Club.query.execution_options(all_clubs=True).all()
    for c in clubs:
        if host in c.domain_list:
            return c
    base = (current_app.config.get("PLATFORM_DOMAIN") or "").lower().strip(".")
    if base and host.endswith("." + base):
        slug = host[: -len(base) - 1]
        return next((c for c in clubs if c.slug == slug), None)
    return None


def resolve_request_club(host: str):
    club = club_for_host(host)
    if club is None and not current_app.config.get("STRICT_HOSTS"):
        club = default_club()
    return club


def install(app) -> None:
    """Session-Events registrieren (einmal pro Prozess)."""
    if getattr(install, "_done", False):
        return
    install._done = True

    @event.listens_for(Session, "do_orm_execute")
    def _scope_to_club(state):
        cid = current_club_id()
        if cid is None or state.execution_options.get("all_clubs"):
            return
        if state.is_select or state.is_update or state.is_delete:
            state.statement = state.statement.options(
                with_loader_criteria(TenantMixin, lambda cls: cls.club_id == cid, include_aliases=True))

    @event.listens_for(Session, "before_flush")
    def _assign_club(session, flush_context, instances):
        cid = current_club_id()
        for obj in session.new:
            if isinstance(obj, TenantMixin) and obj.club_id is None:
                if cid is None:
                    club = default_club() if has_app_context() else None
                    cid = club.id if club else None
                if cid is None:
                    raise RuntimeError(f"{type(obj).__name__} ohne Klub angelegt (kein Klub im Kontext)")
                obj.club_id = cid
            elif isinstance(obj, TenantMixin) and current_club_id() is not None and obj.club_id != current_club_id():
                raise RuntimeError("Objekt eines anderen Klubs kann in diesem Kontext nicht angelegt werden")
