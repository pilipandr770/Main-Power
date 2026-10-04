"""Einmalige Migration einer bestehenden Ein-Klub-Datenbank auf Mandantenfähigkeit.

Idempotent — läuft bei jedem `flask init-db` und macht nur, was noch fehlt:
1. Tabelle `clubs` + Standardklub anlegen.
2. In jeder Mandanten-Tabelle `club_id` ergänzen und alle vorhandenen Zeilen dem Standardklub zuordnen.
3. `settings` von Primärschlüssel `key` auf `id` + eindeutig (club_id, key) umbauen.
4. Bisher global eindeutige Spalten (E-Mail, Telegram-ID, Termin-ID der Quelle, Leistungs-Slug) nur noch je Klub
   eindeutig machen (PostgreSQL). SQLite (nur lokale Entwicklung) kann Constraints nicht ändern: dort DB neu anlegen.
5. Formate des Standardklubs aus der neutralen Vorlage anlegen, falls noch keine existieren.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from .extensions import db
from .tenancy import TenantMixin

# Tabelle -> Spalten, die früher allein eindeutig waren und jetzt je Klub eindeutig sind
FORMERLY_UNIQUE = {"users": ["email", "telegram_user_id"], "events": ["external_id"], "services": ["slug"]}


def _tenant_tables() -> list[str]:
    return sorted({m.class_.__tablename__ for m in db.Model.registry.mappers if issubclass(m.class_, TenantMixin)})


def migrate_multitenant() -> list[str]:
    from .models import Club, MeetingFormat  # noqa: F401  (Tabellen registrieren)
    from .seed import ensure_default_club
    log: list[str] = []
    engine = db.engine
    dialect = engine.dialect.name
    insp = inspect(engine)

    # --- 3a. Alte settings-Tabelle (PK = key) beiseitelegen, damit create_all die neue Form anlegt
    legacy_settings = False
    if insp.has_table("settings") and "id" not in {c["name"] for c in insp.get_columns("settings")}:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE settings RENAME TO settings_legacy"))
        legacy_settings = True
        log.append("settings: alte Tabelle umbenannt (settings_legacy)")

    # --- 1. clubs anlegen (+ alle neuen Tabellen); bestehende Tabellen bleiben unverändert
    db.create_all()
    club = ensure_default_club()
    cid = club.id

    # --- 2. club_id in bestehende Mandanten-Tabellen
    insp = inspect(engine)
    for table in _tenant_tables():
        if not insp.has_table(table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "club_id" in cols:
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN club_id INTEGER NOT NULL DEFAULT {int(cid)}"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_club_id ON {table} (club_id)"))
            if dialect == "postgresql":
                conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN club_id DROP DEFAULT"))
                conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_club FOREIGN KEY (club_id) "
                                  f"REFERENCES clubs (id) ON DELETE CASCADE"))
        log.append(f"{table}: club_id ergänzt (Standardklub {club.slug})")

    # --- 3b. alte Einstellungen übernehmen
    if legacy_settings:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO settings (club_id, key, value) SELECT :cid, key, value FROM settings_legacy"),
                         {"cid": cid})
            conn.execute(text("DROP TABLE settings_legacy"))
        log.append("settings: Werte übernommen")

    # --- 4. Eindeutigkeit je Klub (nur PostgreSQL)
    if dialect == "postgresql":
        insp = inspect(engine)
        for table, columns in FORMERLY_UNIQUE.items():
            if not insp.has_table(table):
                continue
            for uc in insp.get_unique_constraints(table):
                if uc["column_names"] in ([c] for c in columns):
                    with engine.begin() as conn:
                        conn.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{uc["name"]}"'))
                    log.append(f"{table}: Constraint {uc['name']} entfernt")
            for ix in insp.get_indexes(table):
                if ix.get("unique") and ix["column_names"] in ([c] for c in columns):
                    with engine.begin() as conn:
                        conn.execute(text(f'DROP INDEX "{ix["name"]}"'))
                        conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{ix["name"]}" ON {table} ({ix["column_names"][0]})'))
                    log.append(f"{table}: eindeutiger Index {ix['name']} -> normaler Index")
        wanted = {"users": [("uq_user_club_email", "club_id, email"), ("uq_user_club_telegram", "club_id, telegram_user_id")],
                  "events": [("uq_event_club_external", "club_id, external_id")],
                  "services": [("uq_service_club_slug", "club_id, slug")]}
        insp = inspect(engine)
        for table, items in wanted.items():
            have = {uc["name"] for uc in insp.get_unique_constraints(table)}
            for name, cols in items:
                if name not in have:
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({cols})"))
                    log.append(f"{table}: {name} angelegt")

    # --- 5. Formate des Standardklubs
    from .club_presets import apply_formats, preset_formats
    from .tenancy import use_club
    with use_club(club):
        if MeetingFormat.query.count() == 0:
            apply_formats(preset_formats("neutral"))
            db.session.commit()
            log.append("Formate der neutralen Vorlage angelegt")
    return log
