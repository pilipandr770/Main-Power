"""Datenmodell der Plattform (mandantenfähig: jede Klub-Tabelle trägt club_id, siehe tenancy.py).

Hinweis: Embeddings liegen als JSON-Liste in der Tabelle `profiles`.
Für 1.000–20.000 Profile reicht In-Memory-Cosinus (numpy) problemlos.
Ab ~50k Profilen: auf pgvector umstellen (siehe CLAUDE.md, Abschnitt "Skalierung").
"""
from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .tenancy import TenantMixin


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


DEFAULT_FORMATS = {
    "hub": {
        "name": "Main Power Hub",
        "short": "Hub",
        "tagline": "Relevante Kontakte",
        "image": "img/format-hub.png",
        "price_cents": 2500,
        "rhythm": "Zunächst monatlich, ab November zweimal im Monat",
        "description": (
            "Das Flaggschiff-Format der Community: ein kuratiertes Frühstückstreffen für ambitionierte "
            "Unternehmer:innen und Fachleute aus Frankfurt, mit persönlicher Vorstellungsrunde und gezieltem Matching."
        ),
    },
    "stammtisch": {
        "name": "Main Power Stammtisch",
        "short": "Stammtisch",
        "tagline": "Ehrlicher Austausch",
        "image": "img/format-stammtisch.png",
        "price_cents": 0,
        "rhythm": "Alle zwei Wochen, mittwochs",
        "description": "Ein offener Abend rund um das Thema Energie: ehrliche Gespräche, neue Kontakte, gute Atmosphäre.",
    },
    "laufen": {
        "name": "Main Power Laufen",
        "short": "Laufen",
        "tagline": "Bewegung & Energie",
        "image": "img/format-laufen.png",
        "price_cents": 0,
        "rhythm": "Einmal im Monat, samstags um 5:45 Uhr",
        "description": "Gemeinsam den Tag mit Bewegung und guten Gesprächen starten.",
    },
    "frauenkreis": {
        "name": "Main Power Frauenkreis",
        "short": "Frauenkreis",
        "tagline": "Geschützter Raum",
        "image": "img/format-frauenkreis.png",
        "price_cents": 0,
        "rhythm": "Einmal im Monat",
        "description": "Ein geschützter Raum für Frauen, die sich austauschen, stärken und vernetzen wollen.",
    },
    "community": {
        "name": "Community-Treffen",
        "short": "Community",
        "tagline": "Von Mitgliedern organisiert",
        "image": "img/format-hub.png",
        "price_cents": 0,
        "rhythm": "Individuell",
        "description": "Treffen, die Mitglieder selbst initiieren — thematisch, klein, konkret.",
    },
}


class Club(db.Model):
    """Ein Klub (Mandant). Branding und Einstellungen liegen je Klub in `settings` (Schlüssel `club.*`)."""
    __tablename__ = "clubs"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), unique=True, nullable=False)  # Subdomain unter PLATFORM_DOMAIN
    name = db.Column(db.String(120), nullable=False)
    domains = db.Column(db.String(500), default="")  # eigene Domains, kommagetrennt
    status = db.Column(db.String(20), default="active", nullable=False)  # active|suspended
    plan = db.Column(db.String(30), default="pilot")  # pilot|pro|business (Abrechnung folgt)
    contact_email = db.Column(db.String(255), default="")  # Ansprechpartner:in des Klubs (Vertrag/AVV)
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @property
    def domain_list(self) -> list[str]:
        return [d.strip().lower() for d in (self.domains or "").replace(";", ",").split(",") if d.strip()]

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class MeetingFormat(TenantMixin, db.Model):
    """Veranstaltungsformat eines Klubs (bei Main Power: Hub, Stammtisch, Laufen, Frauenkreis)."""
    __tablename__ = "meeting_formats"
    __table_args__ = (db.UniqueConstraint("club_id", "key", name="uq_format_club_key"),)

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    short = db.Column(db.String(40), default="")
    tagline = db.Column(db.String(160), default="")
    image = db.Column(db.String(200), default="")  # "img/…" (static) oder "upload:<datei>"
    price_cents = db.Column(db.Integer, default=0)
    price_note = db.Column(db.String(300), default="")
    rhythm = db.Column(db.String(160), default="")
    description = db.Column(db.Text, default="")
    details = db.Column(db.Text, default="")  # zusätzlicher Absatz auf der Formatseite
    featured = db.Column(db.Boolean, default=True)  # auf der Startseite zeigen
    active = db.Column(db.Boolean, default=True)
    system = db.Column(db.Boolean, default=False)  # "community" (von Mitgliedern organisiert) lässt sich nicht löschen
    sort = db.Column(db.Integer, default=0)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "short": self.short or self.name, "tagline": self.tagline,
                "image": self.image, "price_cents": self.price_cents or 0, "price_note": self.price_note,
                "rhythm": self.rhythm, "description": self.description, "details": self.details,
                "featured": bool(self.featured), "active": bool(self.active), "system": bool(self.system)}


class _FormatRegistry(Mapping):
    """Formate des aktuellen Klubs wie ein dict (FORMATS["hub"], FORMATS.items(), "hub" in FORMATS).

    Iteration liefert nur aktive Formate; per Schlüssel sind auch deaktivierte abrufbar (für alte Termine).
    Ohne Klub-Kontext oder ohne Datensätze gelten die Voreinstellungen (DEFAULT_FORMATS).
    """

    def _data(self) -> dict:
        from flask import g, has_app_context
        if not has_app_context():
            return {k: dict(v, key=k, active=True, featured=k != "community") for k, v in DEFAULT_FORMATS.items()}
        cached = g.get("_formats")
        if cached is None:
            try:
                rows = MeetingFormat.query.order_by(MeetingFormat.sort, MeetingFormat.id).all()
            except Exception:  # Tabelle existiert noch nicht (Migration)
                db.session.rollback()
                rows = []
            if rows:
                cached = {r.key: r.as_dict() for r in rows}
            else:
                cached = {k: dict(v, key=k, active=True, featured=k != "community") for k, v in DEFAULT_FORMATS.items()}
            cached.setdefault("community", dict(DEFAULT_FORMATS["community"], key="community", active=True,
                                                featured=False, system=True))
            g._formats = cached
        return cached

    def __getitem__(self, key):
        return self._data()[key]

    def __iter__(self):
        return iter([k for k, v in self._data().items() if v.get("active", True)])

    def __len__(self):
        return len(list(iter(self)))

    def __contains__(self, key):
        return key in self._data()

    def featured(self) -> list[tuple[str, dict]]:
        return [(k, v) for k, v in self._data().items() if v.get("active", True) and v.get("featured") and k != "community"]


def invalidate_formats() -> None:
    from flask import g
    g.pop("_formats", None)


FORMATS = _FormatRegistry()

ROLES = ("member", "admin", "superadmin")

# Soziale Netzwerke im Profil: key -> (Label, Beispiel, erlaubte Domains). Spalte = f"{key}_url".
SOCIALS = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/in/…", ("linkedin.com",)),
    "xing": ("Xing", "https://www.xing.com/profile/…", ("xing.com",)),
    "instagram": ("Instagram", "https://www.instagram.com/…", ("instagram.com",)),
    "facebook": ("Facebook", "https://www.facebook.com/…", ("facebook.com", "fb.com")),
    "telegram": ("Telegram", "@dein_name oder https://t.me/…", ("t.me", "telegram.me")),
    "x": ("X", "https://x.com/…", ("x.com", "twitter.com")),
    "youtube": ("YouTube", "https://www.youtube.com/@…", ("youtube.com", "youtu.be")),
    "github": ("GitHub", "https://github.com/…", ("github.com",)),
    "tiktok": ("TikTok", "https://www.tiktok.com/@…", ("tiktok.com",)),
    "website": ("Website", "https://…", ()),
}


class User(UserMixin, TenantMixin, db.Model):
    __tablename__ = "users"
    __table_args__ = (db.UniqueConstraint("club_id", "email", name="uq_user_club_email"),
                      db.UniqueConstraint("club_id", "telegram_user_id", name="uq_user_club_telegram"))

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)  # eindeutig je Klub
    password_hash = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False, default="")
    phone = db.Column(db.String(40), default="")
    role = db.Column(db.String(20), nullable=False, default="member")
    status = db.Column(db.String(20), nullable=False, default="active")  # active|blocked
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)

    telegram_user_id = db.Column(db.BigInteger, nullable=True, index=True)
    telegram_username = db.Column(db.String(80))
    telegram_link_token = db.Column(db.String(64), unique=True, nullable=True)

    profile = db.relationship("Profile", uselist=False, back_populates="user", cascade="all, delete-orphan")
    consents = db.relationship("Consent", back_populates="user", cascade="all, delete-orphan",
                               order_by="Consent.created_at.desc()")
    registrations = db.relationship("Registration", back_populates="user", cascade="all, delete-orphan")

    # --- auth helpers ---
    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    @property
    def is_active(self) -> bool:  # Flask-Login
        return self.status == "active"

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def photo_name(self) -> str | None:
        return self.profile.photo if self.profile and self.profile.photo else None

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + (self.last_name[:1] or "")).upper()

    def ensure_telegram_token(self) -> str:
        if not self.telegram_link_token:
            self.telegram_link_token = secrets.token_urlsafe(24)
        return self.telegram_link_token

    def has_consent(self, kind: str) -> bool:
        latest = (Consent.query.filter_by(user_id=self.id, kind=kind)
                  .order_by(Consent.created_at.desc(), Consent.id.desc()).first())
        return bool(latest and latest.granted)


class Profile(TenantMixin, db.Model):
    __tablename__ = "profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    user = db.relationship("User", back_populates="profile")

    headline = db.Column(db.String(160), default="")   # z. B. "Steuerberaterin, Kanzlei XY"
    company = db.Column(db.String(160), default="")
    industry = db.Column(db.String(120), default="")
    city = db.Column(db.String(120), default="Frankfurt am Main")
    region = db.Column(db.String(60), default="rhein-main", index=True)
    bio = db.Column(db.Text, default="")

    # Die vier Hub-Fragen
    q_focus = db.Column(db.Text, default="")         # Tätigkeitsbereich
    q_challenge = db.Column(db.Text, default="")     # größte berufliche Herausforderung
    q_can_help = db.Column(db.Text, default="")      # womit ich anderen helfen kann
    q_looking_for = db.Column(db.Text, default="")   # wonach ich konkret suche

    expertise = db.Column(db.String(500), default="")          # kommagetrennt
    preferred_formats = db.Column(db.String(200), default="")  # kommagetrennt (Keys aus FORMATS)

    # Erweiterter Fragebogen (questionnaire.py). Auswahlfelder als kommagetrennte Keys, alles optional.
    role = db.Column(db.String(30), default="")            # A: Wer du bist
    stage = db.Column(db.String(30), default="")
    team_size = db.Column(db.String(20), default="")
    customer_types = db.Column(db.String(60), default="")
    markets = db.Column(db.String(300), default="")
    languages = db.Column(db.String(200), default="")
    goal_category = db.Column(db.String(30), default="")   # B: Ziel
    goal_12m = db.Column(db.Text, default="")
    milestone_90d = db.Column(db.Text, default="")
    q_tried = db.Column(db.Text, default="")
    partner_types = db.Column(db.String(300), default="")  # C: Wen du suchst (q_looking_for = ideale Person)
    first_outcome = db.Column(db.String(30), default="")
    not_wanted = db.Column(db.Text, default="")             # nie sichtbar, nur Filter im Matching
    resources = db.Column(db.String(200), default="")      # D: Was du gibst (q_can_help)
    asked_for = db.Column(db.Text, default="")
    help_mode = db.Column(db.String(30), default="")
    proud_of = db.Column(db.Text, default="")
    network_time = db.Column(db.String(20), default="")    # E: Zusammenarbeit
    meeting_mode = db.Column(db.String(20), default="")
    work_values = db.Column(db.String(200), default="")
    talk_topics = db.Column(db.Text, default="")           # F: Zum Kennenlernen
    unknown_fact = db.Column(db.Text, default="")
    public_fields = db.Column(db.String(200), default="")  # freigegebene Felder aus SHAREABLE_PRIVATE (Opt-in)
    custom_answers = db.Column(db.JSON)                    # Antworten auf klubeigene Fragen {key: Text | [Optionen]}
    milestone_set_at = db.Column(db.DateTime)              # Start des aktuellen 90-Tage-Zeitraums

    linkedin_url = db.Column(db.String(300), default="")
    xing_url = db.Column(db.String(300), default="")
    instagram_url = db.Column(db.String(300), default="")
    website_url = db.Column(db.String(300), default="")
    facebook_url = db.Column(db.String(300), default="")
    telegram_url = db.Column(db.String(300), default="")
    x_url = db.Column(db.String(300), default="")
    youtube_url = db.Column(db.String(300), default="")
    github_url = db.Column(db.String(300), default="")
    tiktok_url = db.Column(db.String(300), default="")
    event_invites = db.Column(db.Boolean, default=True, nullable=False)  # Aiko darf passende Termine vorschlagen
    socials_public = db.Column(db.Boolean, default=False, nullable=False)  # Links für alle Mitglieder sichtbar
    photo = db.Column(db.String(120))  # Dateiname in instance/uploads/avatars (nur vom Server vergeben)

    visible_in_directory = db.Column(db.Boolean, default=False, nullable=False)
    allow_matching = db.Column(db.Boolean, default=False, nullable=False)

    embed_need = db.Column(db.JSON)
    embed_offer = db.Column(db.JSON)
    embed_avoid = db.Column(db.JSON)  # "Womit möchtest du nicht kontaktiert werden?"
    embed_provider = db.Column(db.String(60))
    embed_updated_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def social_links(self) -> list[tuple[str, str, str]]:
        """[(key, Label, URL)] aller gepflegten Netzwerke."""
        return [(k, v[0], getattr(self, f"{k}_url") or "") for k, v in SOCIALS.items() if getattr(self, f"{k}_url")]

    @property
    def expertise_list(self) -> list[str]:
        return [t.strip() for t in (self.expertise or "").split(",") if t.strip()]

    @property
    def formats_list(self) -> list[str]:
        return [t.strip() for t in (self.preferred_formats or "").split(",") if t.strip()]

    def is_public(self, field: str) -> bool:
        """Darf ein anderes Mitglied (oder eine KI-Antwort an ein anderes Mitglied) dieses Feld sehen?"""
        from .questionnaire import ALWAYS_PRIVATE, SHAREABLE_PRIVATE, split
        if field in ALWAYS_PRIVATE:
            return False
        if field in SHAREABLE_PRIVATE:
            return field in split(self.public_fields)
        return True

    def shown(self, field: str) -> str:
        return (getattr(self, field) or "") if self.is_public(field) else ""

    @property
    def need_text(self) -> str:
        from .questionnaire import GOAL_CATEGORIES, PARTNER_TYPES, custom_texts, label, labels
        seeks = labels(PARTNER_TYPES, self.partner_types)
        return " \n".join(filter(None, [
            self.q_challenge, self.q_looking_for, self.goal_12m, self.milestone_90d, custom_texts(self)[0],
            ("Sucht: " + ", ".join(seeks)) if seeks else "",
            ("Ziel: " + label(GOAL_CATEGORIES, self.goal_category)) if self.goal_category else ""]))

    @property
    def offer_text(self) -> str:
        from .questionnaire import RESOURCES, ROLES, custom_texts, label, labels
        gives = labels(RESOURCES, self.resources)
        return " \n".join(filter(None, [
            self.headline, label(ROLES, self.role), self.industry, self.q_focus, self.q_can_help, self.asked_for,
            custom_texts(self)[1],
            self.proud_of, self.expertise, self.bio, ("Bringt ein: " + ", ".join(gives)) if gives else ""]))

    @property
    def completeness(self) -> int:
        """Matching-Qualität: gewichtet nach Nutzen fürs Matching, nicht nach Anzahl der Felder."""
        weighted = [
            (3, self.q_can_help), (3, self.q_looking_for), (2, self.partner_types), (2, self.goal_12m),
            (2, self.q_focus), (2, self.q_challenge), (1, self.role), (1, self.stage), (1, self.headline),
            (1, self.industry), (1, self.resources), (1, self.expertise), (1, self.languages),
            (1, self.bio or self.proud_of),
            (1, self.linkedin_url or self.xing_url or self.website_url or self.instagram_url)]
        total = sum(w for w, _ in weighted)
        return round(100 * sum(w for w, f in weighted if f and str(f).strip()) / total)

    @property
    def is_matchable(self) -> bool:
        return bool(self.allow_matching and (self.q_can_help or self.asked_for or self.resources)
                    and (self.q_looking_for or self.q_challenge or self.partner_types or self.goal_12m))


class Consent(TenantMixin, db.Model):
    """Append-only Einwilligungsprotokoll (Art. 7 Abs. 1 DSGVO: Nachweisbarkeit)."""
    __tablename__ = "consents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user = db.relationship("User", back_populates="consents")
    kind = db.Column(db.String(40), nullable=False)  # siehe CONSENT_KINDS
    granted = db.Column(db.Boolean, nullable=False)
    version = db.Column(db.String(20), nullable=False)
    source = db.Column(db.String(40), default="web")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


CONSENT_KINDS = {
    "privacy": "Datenschutzhinweise gelesen, Verarbeitung zur Kontoführung",
    "values": "Werte und Struktur der Community akzeptiert",
    "matching": "Profil für KI-gestütztes Matching und persönliche Empfehlungen verwenden",
    "directory": "Profil für andere Mitglieder im Mitgliederverzeichnis sichtbar",
    "newsletter": "Informationen zu neuen Terminen per E-Mail",
}


class Event(TenantMixin, db.Model):
    __tablename__ = "events"
    __table_args__ = (db.UniqueConstraint("club_id", "external_id", name="uq_event_club_external"),)

    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(200), nullable=True)
    source = db.Column(db.String(20), default="admin")  # sync|admin|member
    format = db.Column(db.String(30), default="community", index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, default="")
    location = db.Column(db.String(255), default="")
    online_url = db.Column(db.String(400), default="")  # nur für angemeldete Teilnehmende sichtbar
    starts_at = db.Column(db.DateTime, nullable=False, index=True)
    ends_at = db.Column(db.DateTime)
    all_day = db.Column(db.Boolean, default=False)
    time_pending = db.Column(db.Boolean, default=False)
    price_cents = db.Column(db.Integer, default=0)
    capacity = db.Column(db.Integer)  # None = unbegrenzt
    status = db.Column(db.String(20), default="published", index=True)  # published|pending|cancelled
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_by = db.relationship("User")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    registrations = db.relationship("Registration", back_populates="event", cascade="all, delete-orphan")

    @property
    def format_info(self) -> dict:
        return FORMATS.get(self.format, FORMATS["community"])

    @property
    def is_paid(self) -> bool:
        return (self.price_cents or 0) > 0

    @property
    def active_registrations(self):
        return [r for r in self.registrations if r.status in ("registered", "paid", "reserved")]

    @property
    def seats_left(self):
        if not self.capacity:
            return None
        return max(0, self.capacity - len(self.active_registrations))


class Registration(TenantMixin, db.Model):
    __tablename__ = "registrations"
    __table_args__ = (db.UniqueConstraint("event_id", "user_id", name="uq_registration"),)

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event = db.relationship("Event", back_populates="registrations")
    user = db.relationship("User", back_populates="registrations")
    # registered (kostenlos) | pending_payment | paid | reserved (bezahlt vor Ort) | cancelled
    status = db.Column(db.String(20), default="registered", nullable=False)
    stripe_session_id = db.Column(db.String(255))
    amount_cents = db.Column(db.Integer, default=0)
    note = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)


class Match(TenantMixin, db.Model):
    """Cache für berechnete Matches + KI-Begründung (Paar ist gerichtet: für user_id empfohlen)."""
    __tablename__ = "matches"
    __table_args__ = (db.UniqueConstraint("user_id", "other_id", name="uq_match_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    other_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    other = db.relationship("User", foreign_keys=[other_id])
    score = db.Column(db.Float, default=0.0)
    reason = db.Column(db.Text, default="")
    opener = db.Column(db.Text, default="")
    profile_version = db.Column(db.String(64))  # Hash beider Profile -> Invalidierung
    dismissed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)


class IntroRequest(TenantMixin, db.Model):
    """Kontaktanfrage: Kontaktdaten werden erst nach ausdrücklichem Opt-in (accepted) geteilt."""
    __tablename__ = "intro_requests"

    id = db.Column(db.Integer, primary_key=True)
    from_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    to_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    from_user = db.relationship("User", foreign_keys=[from_user_id])
    to_user = db.relationship("User", foreign_keys=[to_user_id])
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="pending")  # pending|accepted|declined|withdrawn
    created_at = db.Column(db.DateTime, default=utcnow)
    responded_at = db.Column(db.DateTime)


class ChatMessage(TenantMixin, db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel = db.Column(db.String(20), default="web")  # web|telegram
    role = db.Column(db.String(20), nullable=False)  # user|assistant
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PairInsight(TenantMixin, db.Model):
    """Zwischengespeicherte KI-Einschätzung 'Was bringt dir dieser Kontakt?' (spart Tokens, ändert sich nur mit den Profilen)."""
    __tablename__ = "pair_insights"
    __table_args__ = (db.UniqueConstraint("user_id", "other_id", name="uq_pair_insight"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    other_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    version = db.Column(db.String(64))
    data = db.Column(db.JSON)
    ai = db.Column(db.Boolean, default=True)  # False = Vorlage ohne KI
    created_at = db.Column(db.DateTime, default=utcnow)


class Notification(TenantMixin, db.Model):
    """Persönliche Hinweise, z. B. KI-Einladung zu einem Termin, der zum Profil passt."""
    __tablename__ = "notifications"
    __table_args__ = (db.UniqueConstraint("user_id", "event_id", "kind", name="uq_notification_event"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(30), default="event_invite")
    event_id = db.Column(db.Integer, db.ForeignKey("events.id", ondelete="CASCADE"))
    event = db.relationship("Event")
    title = db.Column(db.String(200), default="")
    body = db.Column(db.Text, default="")
    score = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    read_at = db.Column(db.DateTime)


class SeoReport(TenantMixin, db.Model):
    """Ergebnis der Leistung „SEO-Check“ (Prüfung einer öffentlichen Seite + Bericht von Aiko)."""
    __tablename__ = "seo_reports"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = db.Column(db.String(20), default="seo", nullable=False)  # seo|compliance
    url = db.Column(db.String(500), nullable=False)
    keywords = db.Column(db.String(300), default="")
    score = db.Column(db.Integer, default=0)
    data = db.Column(db.JSON)  # {"result": {...}, "report": {...}}
    ai = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PanelRun(TenantMixin, db.Model):
    """Ein Lauf des synthetischen Markt-Panels (fiktive KI-Personas bewerten ein Produkt)."""
    __tablename__ = "panel_runs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, default="")
    audience = db.Column(db.String(20), default="beide")  # privat|business|beide
    regional = db.Column(db.String(20), default="de")  # de|rhein-main
    unit = db.Column(db.String(20), default="einmalig")  # einmalig|monat|jahr
    price_a = db.Column(db.Float)
    price_b = db.Column(db.Float)
    n_requested = db.Column(db.Integer, default=100)
    n_done = db.Column(db.Integer, default=0)
    n_failed = db.Column(db.Integer, default=0)
    seed = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default="queued", index=True)  # queued|running|done|failed
    error = db.Column(db.String(300), default="")
    model = db.Column(db.String(80), default="")
    tokens_in = db.Column(db.Integer, default=0)
    tokens_out = db.Column(db.Integer, default=0)
    result = db.Column(db.JSON)  # aggregierte Kennzahlen
    report = db.Column(db.JSON)  # Bericht von Aiko
    ai = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    finished_at = db.Column(db.DateTime)

    responses = db.relationship("PanelResponse", backref="run", cascade="all, delete-orphan", order_by="PanelResponse.idx")


class PanelResponse(TenantMixin, db.Model):
    __tablename__ = "panel_responses"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.Integer, db.ForeignKey("panel_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    idx = db.Column(db.Integer, default=0)
    variant = db.Column(db.String(1), default="A")
    persona = db.Column(db.JSON)
    answers = db.Column(db.JSON)
    ok = db.Column(db.Boolean, default=True)


class LawQuery(TenantMixin, db.Model):
    """Anfrage an die Gesetzes-Suche: Fundstellen aus dem Bundesrecht-Korpus (verbatim) + Einordnung von Aiko.

    Zitiert und ordnet nur ein — keine Rechtsberatung, keine Handlungsempfehlung (siehe services/laws.py).
    """
    __tablename__ = "law_queries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    question = db.Column(db.String(1000), nullable=False)  # heißt bewusst nicht "query" — das überschreibt db.Model.query!
    category = db.Column(db.String(60), default="")
    hits = db.Column(db.JSON)  # [{slug, law_abbreviation, law_title, category, enbez, titel, text, stand, source_url, score}]
    answer = db.Column(db.JSON)  # {erlaeuterungen: [{id, text}], nicht_passend: [...], hinweis, ai}
    ai = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class LLMUsage(TenantMixin, db.Model):
    """Token-Verbrauch je KI-Aufruf (nur Zähler, keine Inhalte)."""
    __tablename__ = "llm_usage"

    id = db.Column(db.Integer, primary_key=True)
    purpose = db.Column(db.String(40), default="", index=True)  # aiko_public|aiko_member|matching
    model = db.Column(db.String(80), default="")
    input_tokens = db.Column(db.Integer, default=0, nullable=False)
    output_tokens = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class Service(TenantMixin, db.Model):
    __tablename__ = "services"
    __table_args__ = (db.UniqueConstraint("club_id", "slug", name="uq_service_club_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    summary = db.Column(db.String(300), default="")
    description = db.Column(db.Text, default="")
    benefits = db.Column(db.Text, default="")  # eine Zeile = ein Vorteil
    member_benefit = db.Column(db.String(300), default="")  # Vorteil für Community-Mitglieder
    price_hint = db.Column(db.String(120), default="")
    provider_name = db.Column(db.String(160), default="")
    provider_email = db.Column(db.String(255), default="")
    active = db.Column(db.Boolean, default=True)
    sort = db.Column(db.Integer, default=0)

    @property
    def benefits_list(self) -> list[str]:
        return [b.strip() for b in (self.benefits or "").splitlines() if b.strip()]


class ServiceInquiry(TenantMixin, db.Model):
    __tablename__ = "service_inquiries"

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    service = db.relationship("Service")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User")
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="new")  # new|contacted|won|lost
    created_at = db.Column(db.DateTime, default=utcnow)


class KnowledgeItem(TenantMixin, db.Model):
    """FAQ + Wissensbasis für Aiko (öffentlich und intern)."""
    __tablename__ = "knowledge_items"

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(300), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    public = db.Column(db.Boolean, default=True)  # auch auf der Startseite / im öffentlichen Chat
    sort = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)


class GoalCheckin(TenantMixin, db.Model):
    """Rückblick auf einen 90-Tage-Meilenstein (vom Mitglied selbst gemeldet) mit Aikos Vorschlag für den nächsten Schritt."""
    __tablename__ = "goal_checkins"

    STATUS = {"erreicht": "Erreicht", "auf_kurs": "Auf gutem Weg", "haengt": "Hängt gerade", "neu": "Neu ausgerichtet"}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    goal = db.Column(db.Text, default="")
    milestone = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="auf_kurs")
    note = db.Column(db.Text, default="")
    next_milestone = db.Column(db.Text, default="")
    ai_feedback = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    @property
    def status_label(self) -> str:
        return self.STATUS.get(self.status, self.status)


class ClubQuestion(TenantMixin, db.Model):
    """Zusätzliche Frage eines Klubs im Profil (z. B. „Welche Branche im Verband?“). Antworten in Profile.custom_answers."""
    __tablename__ = "club_questions"
    __table_args__ = (db.UniqueConstraint("club_id", "key", name="uq_club_question_key"),)

    KINDS = {"text": "Freitext", "choice": "Auswahl (eine Antwort)", "multi": "Auswahl (mehrere Antworten)"}
    USES = {"need": "Bedarf (was die Person sucht)", "offer": "Angebot (was die Person bietet)",
            "none": "Nicht fürs Matching"}

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), nullable=False)
    label = db.Column(db.String(200), nullable=False)
    help = db.Column(db.String(300), default="")
    kind = db.Column(db.String(10), default="text")
    options = db.Column(db.JSON)          # Liste von Antwortoptionen bei choice/multi
    use = db.Column(db.String(10), default="none")
    public = db.Column(db.Boolean, default=True, nullable=False)   # im Profil für andere sichtbar
    active = db.Column(db.Boolean, default=True, nullable=False)
    sort = db.Column(db.Integer, default=0)

    @property
    def option_list(self) -> list[str]:
        return [str(o) for o in (self.options or []) if str(o).strip()]

    def clean(self, raw) -> str | list[str]:
        """Eingabe (Formular/KI) auf erlaubte Werte begrenzen."""
        if self.kind == "text":
            return str(raw or "").strip()[:600]
        values = raw if isinstance(raw, list) else [raw]
        allowed = [o for o in self.option_list if o in {str(v) for v in values}]
        return allowed if self.kind == "multi" else (allowed[0] if allowed else "")

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "help": self.help, "kind": self.kind, "options": self.option_list,
                "use": self.use, "public": self.public, "active": self.active, "sort": self.sort}


class Setting(TenantMixin, db.Model):
    __tablename__ = "settings"
    __table_args__ = (db.UniqueConstraint("club_id", "key", name="uq_setting_club_key"),)

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), nullable=False)
    value = db.Column(db.Text, default="")

    DEFAULTS = {
        "member_events_require_approval": "1",
        "auto_invites": "1",
        "panel_monthly_limit": "3",   # Markt-Panel-Läufe je Mitglied und Monat (Admins unbegrenzt)
        "panel_max_personas": "100",  # Obergrenze Personas je Lauf
        "aiko_extra_instructions": "",
        "telegram_group_title": "",  # leer = Klubname
        "announcement": "",
    }

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str:
        row = cls.query.filter_by(key=key).first()
        if row is not None:
            return row.value or ""
        return cls.DEFAULTS.get(key, default or "")

    @classmethod
    def set(cls, key: str, value: str) -> None:
        row = cls.query.filter_by(key=key).first()
        if row is None:
            row = cls(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value


class AuditLog(TenantMixin, db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    actor = db.relationship("User")
    action = db.Column(db.String(80), nullable=False)
    target = db.Column(db.String(120), default="")
    details = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
