import logging
import os

import click
from dotenv import load_dotenv
from flask import Flask, render_template, request
from flask_login import current_user

load_dotenv()

from .config import Config  # noqa: E402
from .extensions import csrf, db, limiter, login_manager, migrate  # noqa: E402


def create_app(config_object=Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    from werkzeug.middleware.proxy_fix import ProxyFix
    hops = int(os.environ.get("PROXY_HOPS", "1"))  # 1 = nur Caddy/Nginx; 2 = Cloudflare-Proxy + Caddy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=1, x_host=1)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not app.config.get("TESTING") and app.config["SECRET_KEY"].startswith("dev-") \
            and os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("SECRET_KEY muss in Produktion gesetzt sein")

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from .models import User

    @login_manager.user_loader
    def load_user(uid):
        return db.session.get(User, int(uid))

    from .blueprints.admin import bp as admin_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.member import bp as member_bp
    from .blueprints.public import bp as public_bp
    from .blueprints.webhooks import bp as webhooks_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(member_bp, url_prefix="/app")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(webhooks_bp, url_prefix="/webhooks")
    csrf.exempt(webhooks_bp)

    _register_template_helpers(app)
    _register_security_headers(app)
    _register_errors(app)
    _register_cli(app)
    return app


def _register_template_helpers(app: Flask) -> None:
    from .models import FORMATS, IntroRequest, Notification
    from .services.payments import stripe_enabled
    from .services.llm import llm_enabled
    from .utils import fmt_dt, fmt_event_date, money, to_local

    from .services.matching import fit_label
    app.jinja_env.filters.update(event_date=fmt_event_date, dt=fmt_dt, money=money, local=to_local, fit=fit_label)

    @app.context_processor
    def inject():
        pending = unread = 0
        if current_user.is_authenticated:
            pending = IntroRequest.query.filter_by(to_user_id=current_user.id, status="pending").count()
            unread = Notification.query.filter_by(user_id=current_user.id, read_at=None).count()
        return {"FORMATS": FORMATS, "cfg": app.config, "pending_intros": pending, "unread_invites": unread,
                "stripe_on": stripe_enabled(), "ai_on": llm_enabled()}


def _register_security_headers(app: Flask) -> None:
    csp = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
           "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://checkout.stripe.com")

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith(("/app", "/admin")) and not request.path.startswith("/app/foto/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp


def _register_errors(app: Flask) -> None:
    @app.errorhandler(403)
    def e403(e):
        return render_template("public/error.html", code=403,
                               msg="Für diesen Bereich fehlt dir die Berechtigung."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("public/error.html", code=404,
                               msg="Diese Seite gibt es nicht (mehr)."), 404

    @app.errorhandler(429)
    def e429(e):
        if request.accept_mimetypes.best == "application/json" or request.is_json:
            return {"error": "Zu viele Anfragen. Bitte kurz warten."}, 429
        return render_template("public/error.html", code=429,
                               msg="Zu viele Versuche. Bitte warte einen Moment."), 429

    @app.errorhandler(500)
    def e500(e):
        db.session.rollback()
        return render_template("public/error.html", code=500,
                               msg="Da ist etwas schiefgelaufen. Wir kümmern uns darum."), 500


def add_missing_columns() -> list[str]:
    """Leichte Schema-Nachführung ohne Alembic: fehlende Spalten bestehender Tabellen per ALTER TABLE ergänzen.

    Neue Tabellen legt db.create_all() an. Umbenennen/Löschen/Typwechsel bleibt Sache von Flask-Migrate.
    """
    from sqlalchemy import inspect, text
    added = []
    insp = inspect(db.engine)
    for table in db.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(db.engine.dialect)}'
            default = col.default.arg if col.default is not None and getattr(col.default, "is_scalar", False) else None
            if default is not None:
                lit = "TRUE" if default is True else "FALSE" if default is False else (str(default) if isinstance(default, (int, float)) else "'" + str(default).replace("'", "''") + "'")
                ddl += f" DEFAULT {lit}"
            if not col.nullable and default is not None:
                ddl += " NOT NULL"
            with db.engine.begin() as conn:
                conn.execute(text(ddl))
            added.append(f"{table.name}.{col.name}")
    return added


def _register_cli(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """Tabellen anlegen und fehlende Spalten ergänzen (für Produktion später: flask db migrate/upgrade)."""
        db.create_all()
        for c in add_missing_columns():
            click.echo(f"Spalte ergaenzt: {c}")
        click.echo("Datenbank initialisiert.")

    @app.cli.command("seed")
    @click.option("--demo/--no-demo", default=True, help="Demo-Mitglieder anlegen")
    def seed_cmd(demo):
        """Formate, FAQ, Leistungen, Beispieltermine (+ Demo-Mitglieder)."""
        from .seed import seed
        seed(demo=demo)
        click.echo("Seed abgeschlossen.")

    @app.cli.command("remove-demo")
    def remove_demo_cmd():
        """Alle Demo-Mitglieder (@demo.main-power.local) löschen — vor dem Go-live."""
        from .seed import remove_demo
        click.echo(f"{remove_demo()} Demo-Konten gelöscht.")

    @app.cli.command("create-admin")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--first-name", default="Asset")
    @click.option("--last-name", default="Beissenov")
    @click.option("--role", default="superadmin", type=click.Choice(["admin", "superadmin"]))
    def create_admin(email, password, first_name, last_name, role):
        from .models import Profile, User
        u = User.query.filter_by(email=email.lower()).first()
        if not u:
            u = User(email=email.lower(), first_name=first_name, last_name=last_name)
            u.profile = Profile()
            db.session.add(u)
        u.set_password(password)
        u.role = role
        u.status = "active"
        db.session.commit()
        click.echo(f"{role} {email} bereit.")

    @app.cli.command("sync-events")
    def sync_cmd():
        """Termine von main-power.org importieren (Cron: stündlich)."""
        from .services.events_sync import sync_events
        click.echo(sync_events())

    @app.cli.command("reembed")
    def reembed_cmd():
        """Alle Profil-Embeddings neu berechnen (nach Providerwechsel)."""
        from .services.matching import reembed_all
        click.echo(f"{reembed_all()} Profile neu eingebettet.")

    @app.cli.command("telegram-set-webhook")
    def tg_webhook():
        from .services import telegram
        url = f"{app.config['BASE_URL']}/webhooks/telegram"
        telegram.api("setWebhook", url=url, secret_token=app.config["TELEGRAM_WEBHOOK_SECRET"],
                     allowed_updates=["message", "chat_member"], drop_pending_updates=True)
        click.echo(f"Webhook gesetzt: {url}")

    @app.cli.command("telegram-poll")
    def tg_poll():
        """Lokale Entwicklung ohne öffentliche URL."""
        from .services import telegram
        telegram.poll_forever()
