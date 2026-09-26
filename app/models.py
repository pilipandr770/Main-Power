"""Datenmodell der Main Power Plattform.

Hinweis: Embeddings liegen als JSON-Liste in der Tabelle `profiles`.
Für 1.000–20.000 Profile reicht In-Memory-Cosinus (numpy) problemlos.
Ab ~50k Profilen: auf pgvector umstellen (siehe CLAUDE.md, Abschnitt "Skalierung").
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


FORMATS = {
    "hub": {
        "name": "Main Power Hub",
        "short": "Hub",
        "tagline": "Relevante Kontakte",
        "image": "format-hub.png",
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
        "image": "format-stammtisch.png",
        "price_cents": 0,
        "rhythm": "Alle zwei Wochen, mittwochs",
        "description": "Ein offener Abend rund um das Thema Energie: ehrliche Gespräche, neue Kontakte, gute Atmosphäre.",
    },
    "laufen": {
        "name": "Main Power Laufen",
        "short": "Laufen",
        "tagline": "Bewegung & Energie",
        "image": "format-laufen.png",
        "price_cents": 0,
        "rhythm": "Einmal im Monat, samstags um 5:45 Uhr",
        "description": "Gemeinsam den Tag mit Bewegung und guten Gesprächen starten.",
    },
    "frauenkreis": {
        "name": "Main Power Frauenkreis",
        "short": "Frauenkreis",
        "tagline": "Geschützter Raum",
        "image": "format-frauenkreis.png",
        "price_cents": 0,
        "rhythm": "Einmal im Monat",
        "description": "Ein geschützter Raum für Frauen, die sich austauschen, stärken und vernetzen wollen.",
    },
    "community": {
        "name": "Community-Treffen",
        "short": "Community",
        "tagline": "Von Mitgliedern organisiert",
        "image": "format-hub.png",
        "price_cents": 0,
        "rhythm": "Individuell",
        "description": "Treffen, die Mitglieder selbst initiieren — thematisch, klein, konkret.",
    },
}

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


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(80), nullable=False)
    last_name = db.Column(db.String(80), nullable=False, default="")
    phone = db.Column(db.String(40), default="")
    role = db.Column(db.String(20), nullable=False, default="member")
    status = db.Column(db.String(20), nullable=False, default="active")  # active|blocked
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime)

    telegram_user_id = db.Column(db.BigInteger, unique=True, nullable=True)
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


class Profile(db.Model):
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

    @property
    def need_text(self) -> str:
        return " \n".join(filter(None, [self.q_challenge, self.q_looking_for]))

    @property
    def offer_text(self) -> str:
        return " \n".join(filter(None, [self.headline, self.industry, self.q_focus, self.q_can_help,
                                         self.expertise, self.bio]))

    @property
    def completeness(self) -> int:
        fields = [self.headline, self.industry, self.q_focus, self.q_challenge, self.q_can_help,
                  self.q_looking_for, self.expertise, self.bio,
                  self.linkedin_url or self.xing_url or self.website_url or self.instagram_url]
        return round(100 * sum(1 for f in fields if f and str(f).strip()) / len(fields))

    @property
    def is_matchable(self) -> bool:
        return bool(self.allow_matching and self.q_can_help and (self.q_looking_for or self.q_challenge))


class Consent(db.Model):
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
    "values": "Werte und Struktur von Main Power akzeptiert",
    "matching": "Profil für KI-gestütztes Matching und persönliche Empfehlungen verwenden",
    "directory": "Profil für andere Mitglieder im Mitgliederverzeichnis sichtbar",
    "newsletter": "Informationen zu neuen Terminen per E-Mail",
}


class Event(db.Model):
    __tablename__ = "events"

    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(200), unique=True, nullable=True)
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


class Registration(db.Model):
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


class Match(db.Model):
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


class IntroRequest(db.Model):
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


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel = db.Column(db.String(20), default="web")  # web|telegram
    role = db.Column(db.String(20), nullable=False)  # user|assistant
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class PairInsight(db.Model):
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


class Notification(db.Model):
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


class LLMUsage(db.Model):
    """Token-Verbrauch je KI-Aufruf (nur Zähler, keine Inhalte)."""
    __tablename__ = "llm_usage"

    id = db.Column(db.Integer, primary_key=True)
    purpose = db.Column(db.String(40), default="", index=True)  # aiko_public|aiko_member|matching
    model = db.Column(db.String(80), default="")
    input_tokens = db.Column(db.Integer, default=0, nullable=False)
    output_tokens = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)


class Service(db.Model):
    __tablename__ = "services"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
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


class ServiceInquiry(db.Model):
    __tablename__ = "service_inquiries"

    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id", ondelete="CASCADE"), nullable=False)
    service = db.relationship("Service")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = db.relationship("User")
    message = db.Column(db.Text, default="")
    status = db.Column(db.String(20), default="new")  # new|contacted|won|lost
    created_at = db.Column(db.DateTime, default=utcnow)


class KnowledgeItem(db.Model):
    """FAQ + Wissensbasis für Aiko (öffentlich und intern)."""
    __tablename__ = "knowledge_items"

    id = db.Column(db.Integer, primary_key=True)
    question = db.Column(db.String(300), nullable=False)
    answer = db.Column(db.Text, nullable=False)
    public = db.Column(db.Boolean, default=True)  # auch auf der Startseite / im öffentlichen Chat
    sort = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, default="")

    DEFAULTS = {
        "member_events_require_approval": "1",
        "auto_invites": "1",
        "aiko_extra_instructions": "",
        "telegram_group_title": "Main Power Community",
        "announcement": "",
    }

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str:
        row = db.session.get(cls, key)
        if row is not None:
            return row.value or ""
        return cls.DEFAULTS.get(key, default or "")

    @classmethod
    def set(cls, key: str, value: str) -> None:
        row = db.session.get(cls, key)
        if row is None:
            row = cls(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    actor = db.relationship("User")
    action = db.Column(db.String(80), nullable=False)
    target = db.Column(db.String(120), default="")
    details = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
