import os
from datetime import timedelta


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///mainpower.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    APP_NAME = "Main Power"
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024  # Foto-Upload (Limit 5 MB) + Formularrest
    UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "")  # leer = <instance>/uploads
    BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
    MAIN_SITE_URL = os.environ.get("MAIN_SITE_URL", "https://www.main-power.org")
    CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hallo@main-power.org")
    TIMEZONE = "Europe/Berlin"

    # Sessions / cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    REMEMBER_COOKIE_SECURE = _bool("COOKIE_SECURE", False)
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    PERMANENT_SESSION_LIFETIME = timedelta(days=14)
    WTF_CSRF_TIME_LIMIT = None

    # Rate limiting
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_IN_MEMORY_FALLBACK_ENABLED = True  # fällt Redis aus, zählt jeder Worker vorübergehend selbst

    # AI
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    # Preise in USD je 1 Mio. Token (nur für die Kostenschätzung im Admin; Standard = Claude Haiku 4.5)
    LLM_PRICE_IN = float(os.environ.get("LLM_PRICE_IN", "1.0"))
    LLM_PRICE_OUT = float(os.environ.get("LLM_PRICE_OUT", "5.0"))
    EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "auto")  # auto|voyage|openai|local
    VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
    VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3.5")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")

    # Gesetzes-Suche: internes Nachbarprojekt (Qdrant + Bundesrecht-Korpus), nur über Docker-Netzwerk erreichbar.
    # Leer = Funktion in der Plattform ausgeblendet.
    LAWS_API_URL = os.environ.get("LAWS_API_URL", "").rstrip("/")

    # Events sync from main-power.org
    EVENTS_SYNC_URL = os.environ.get("EVENTS_SYNC_URL", "https://www.main-power.org/api/events")

    # Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    TELEGRAM_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "")
    TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

    # Stripe (feature flag)
    STRIPE_ENABLED = _bool("STRIPE_ENABLED", False)
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    # Mail (falls back to logging if SMTP_HOST is empty)
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM", "Main Power <hallo@main-power.org>")
    SMTP_TLS = _bool("SMTP_TLS", True)

    # Mehrere Klubs (SaaS). Der Klub wird pro Request über den Host bestimmt: eigene Domain (Club.domains) oder
    # <slug>.<PLATFORM_DOMAIN>. Unbekannte Hosts landen beim Standardklub, außer STRICT_HOSTS=1 (dann 404).
    DEFAULT_CLUB_SLUG = os.environ.get("DEFAULT_CLUB_SLUG", "mainpower")
    DEFAULT_CLUB_NAME = os.environ.get("DEFAULT_CLUB_NAME", "Main Power")
    DEFAULT_CLUB_DOMAINS = os.environ.get("DEFAULT_CLUB_DOMAINS", "")
    PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "")
    STRICT_HOSTS = _bool("STRICT_HOSTS", False)
    # Plattform-Konsole (/plattform): eigene Anmeldung, unabhängig von den Klub-Konten. Leer = Konsole gesperrt.
    PLATFORM_ADMIN_EMAIL = os.environ.get("PLATFORM_ADMIN_EMAIL", "").strip().lower()
    PLATFORM_ADMIN_PASSWORD = os.environ.get("PLATFORM_ADMIN_PASSWORD", "")

    # Demo-Modus: Admins dürfen zwischen Mitgliedskonten wechseln. In Produktion AUS lassen (ENABLE_IMPERSONATION nicht setzen).
    ENABLE_IMPERSONATION = _bool("ENABLE_IMPERSONATION", False)

    # Legal
    IMPRESSUM_URL = os.environ.get("IMPRESSUM_URL", "https://www.main-power.org/impressum")
    CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "2026-09")


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    ANTHROPIC_API_KEY = ""
    EMBEDDING_PROVIDER = "local"
    STRIPE_ENABLED = False
    TELEGRAM_BOT_TOKEN = ""
    LAWS_API_URL = ""
