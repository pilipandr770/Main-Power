import logging
import os

import click
from dotenv import load_dotenv
from flask import Flask, abort, render_template, request, session
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
    from . import tenancy
    tenancy.install(app)

    @app.before_request
    def _bind_club():
        """Klub aus dem Host bestimmen — vor allen anderen Handlern, damit jede Abfrage auf ihn begrenzt ist."""
        if request.endpoint == "static":
            return None
        if request.path.startswith("/plattform"):
            tenancy.set_club(None)  # Plattform-Konsole arbeitet klubübergreifend (eigene Anmeldung)
            return None
        club = tenancy.resolve_request_club(request.host)
        if club is None:
            abort(404)
        tenancy.set_club(club)
        if not club.is_active and not request.path.startswith("/healthz"):
            return render_template("public/error.html", code=503,
                                   msg="Dieser Klub ist gerade pausiert. Bitte versuch es später erneut."), 503
        return None

    from .models import User

    @login_manager.user_loader
    def load_user(uid):
        user = db.session.get(User, int(uid))
        # Sitzung eines anderen Klubs (z. B. gleiche Domain nach Umzug) nie übernehmen
        return user if user is not None and user.club_id == tenancy.current_club_id() else None

    from .blueprints.admin import bp as admin_bp
    from .blueprints.auth import bp as auth_bp
    from .blueprints.member import bp as member_bp
    from .blueprints.public import bp as public_bp
    from .blueprints.platform import bp as platform_bp
    from .blueprints.webhooks import bp as webhooks_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(member_bp, url_prefix="/app")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(webhooks_bp, url_prefix="/webhooks")
    app.register_blueprint(platform_bp, url_prefix="/plattform")
    csrf.exempt(webhooks_bp)

    _register_static_busting(app)
    _register_template_helpers(app)
    _register_security_headers(app)
    _register_errors(app)
    _register_cli(app)
    return app


def _register_static_busting(app: Flask) -> None:
    """Hängt ?v=<Änderungszeit> an Static-URLs: nach einem Deploy holen Browser und Cloudflare sofort die neue Datei."""
    cache: dict[str, int] = {}

    @app.url_defaults
    def add_version(endpoint, values):
        if endpoint != "static" or "filename" not in values or "v" in values:
            return
        name = values["filename"]
        if name not in cache:
            try:
                cache[name] = int(os.path.getmtime(os.path.join(app.static_folder, name)))
            except OSError:
                cache[name] = 0
        if cache[name]:
            values["v"] = cache[name]


def _register_template_helpers(app: Flask) -> None:
    from .models import FORMATS, IntroRequest, Notification
    from .services.payments import stripe_enabled
    from .services.llm import llm_enabled
    from .utils import fmt_dt, fmt_event_date, money, to_local

    from .services.matching import fit_label
    from .services import club as club_settings
    from .tenancy import current_club
    app.jinja_env.filters.update(event_date=fmt_event_date, dt=fmt_dt, money=money, local=to_local, fit=fit_label,
                                 media=club_settings.media_url)
    from . import questionnaire
    app.jinja_env.globals["qn"] = questionnaire  # Auswahllisten des Fragebogens in allen Templates

    @app.context_processor
    def inject():
        pending = unread = 0
        goal_due = False
        impersonating, switch_users = None, []
        from flask import has_request_context
        if not has_request_context():  # z. B. E-Mail-Vorlagen außerhalb eines Requests
            return {"FORMATS": FORMATS, "cfg": app.config, "club": club_settings.settings(), "club_obj": current_club()}
        if current_user.is_authenticated and app.config.get("ENABLE_IMPERSONATION") and session.get("impersonator_id"):
            from .models import User
            impersonating = db.session.get(User, int(session["impersonator_id"]))
            if impersonating and impersonating.is_admin:
                switch_users = (User.query.filter(User.status == "active", User.role != "superadmin")
                                .order_by(User.first_name).limit(60).all())
            else:
                impersonating = None
        if current_user.is_authenticated:
            pending = IntroRequest.query.filter_by(to_user_id=current_user.id, status="pending").count()
            unread = Notification.query.filter(Notification.user_id == current_user.id, Notification.read_at.is_(None),
                                               Notification.kind != "goal_checkin").count()
            from .services.goals import is_due
            goal_due = bool(current_user.profile and is_due(current_user.profile))
        return {"FORMATS": FORMATS, "cfg": app.config, "club": club_settings.settings(), "club_obj": current_club(), "pending_intros": pending, "unread_invites": unread, "goal_due": goal_due, "impersonating": impersonating, "switch_users": switch_users,
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


def _club_option(fn):
    """--club <slug> für CLI-Befehle; ohne Angabe gilt der Standardklub."""
    return click.option("--club", "club_slug", default=None, help="Klub-Slug (Standard: DEFAULT_CLUB_SLUG)")(fn)


def _club_by_slug(slug):
    from .models import Club
    from .seed import ensure_default_club
    if not slug:
        return ensure_default_club()
    club = Club.query.execution_options(all_clubs=True).filter_by(slug=slug).first()
    if club is None:
        raise click.ClickException(f"Klub '{slug}' nicht gefunden")
    return club


def _register_cli(app: Flask) -> None:
    from .tenancy import use_club

    @app.cli.command("init-db")
    def init_db():
        """Tabellen anlegen, Mandanten-Migration ausführen und fehlende Spalten ergänzen."""
        from .migrations_mt import migrate_multitenant
        for line in migrate_multitenant():
            click.echo(line)
        for c in add_missing_columns():
            click.echo(f"Spalte ergaenzt: {c}")
        from .seed import refresh_default_faq
        n = refresh_default_faq()
        if n:
            click.echo(f"FAQ aktualisiert: {n} Standardantworten")
        click.echo("Datenbank initialisiert.")

    @app.cli.command("list-clubs")
    def list_clubs():
        """Alle Klubs mit Domains und Status."""
        from .models import Club
        for c in Club.query.execution_options(all_clubs=True).order_by(Club.id):
            click.echo(f"{c.id:>3}  {c.slug:<20} {c.status:<10} {c.name}  [{c.domains}]")

    @app.cli.command("create-club")
    @click.argument("slug")
    @click.argument("name")
    @click.option("--admin-email", required=True)
    @click.option("--admin-password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--domains", default="")
    @click.option("--preset", default="neutral")
    @click.option("--demo/--no-demo", default=False)
    def create_club_cmd(slug, name, admin_email, admin_password, domains, preset, demo):
        """Neuen Klub mit Vorlage und erstem Superadmin anlegen."""
        from .seed import create_club
        club = create_club(slug, name, admin_email, admin_password, domains=domains, preset=preset, demo=demo)
        click.echo(f"Klub {club.slug} (id {club.id}) angelegt.")

    @app.cli.command("seed")
    @click.option("--demo/--no-demo", default=True, help="Demo-Mitglieder anlegen")
    @_club_option
    def seed_cmd(demo, club_slug):
        """Formate, FAQ, Leistungen, Beispieltermine (+ Demo-Mitglieder) für einen Klub."""
        from .seed import seed
        db.create_all()
        seed(demo=demo, club=_club_by_slug(club_slug))
        click.echo("Seed abgeschlossen.")

    @app.cli.command("remove-demo")
    @_club_option
    def remove_demo_cmd(club_slug):
        """Alle Demo-Mitglieder eines Klubs löschen — vor dem Go-live."""
        from .seed import remove_demo
        with use_club(_club_by_slug(club_slug)):
            click.echo(f"{remove_demo()} Demo-Konten gelöscht.")

    @app.cli.command("create-admin")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    @click.option("--first-name", default="Asset")
    @click.option("--last-name", default="Beissenov")
    @click.option("--role", default="superadmin", type=click.Choice(["admin", "superadmin"]))
    @_club_option
    def create_admin(email, password, first_name, last_name, role, club_slug):
        """Admin-Konto in einem Klub anlegen oder Passwort setzen."""
        with use_club(_club_by_slug(club_slug)):
            _create_admin(email, password, first_name, last_name, role)
        click.echo(f"{role} {email} bereit.")

    def _create_admin(email, password, first_name, last_name, role):
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

    @app.cli.command("sync-events")
    @_club_option
    def sync_cmd(club_slug):
        """Termine aus der Quelle jedes Klubs importieren (Cron: stündlich). Ohne --club: alle aktiven Klubs."""
        from .models import Club
        from .services import club as club_settings
        from .services.events_sync import sync_events
        if club_slug:
            clubs = [_club_by_slug(club_slug)]
        else:
            clubs = Club.query.execution_options(all_clubs=True).filter_by(status="active").all()
        for c in clubs:
            with use_club(c):
                if not club_settings.settings().get("events_sync_url"):
                    continue
                try:
                    click.echo(f"{c.slug}: {sync_events()}")
                except Exception as exc:  # eine kaputte Quelle darf die anderen Klubs nicht blockieren
                    click.echo(f"{c.slug}: Fehler {exc}")

    @app.cli.command("reembed")
    @_club_option
    def reembed_cmd(club_slug):
        """Profil-Embeddings neu berechnen (nach Providerwechsel). Ohne --club: alle Klubs."""
        from .models import Club
        from .services.matching import reembed_all
        clubs = [_club_by_slug(club_slug)] if club_slug else Club.query.execution_options(all_clubs=True).all()
        for c in clubs:
            with use_club(c):
                click.echo(f"{c.slug}: {reembed_all()} Profile neu eingebettet.")

    @app.cli.command("goal-checkins")
    @_club_option
    def goal_checkins_cmd(club_slug):
        """90-Tage-Erinnerungen verschicken (Cron: täglich). Ohne --club: alle aktiven Klubs."""
        from .models import Club
        from .services.goals import send_reminders
        clubs = [_club_by_slug(club_slug)] if club_slug else \
            Club.query.execution_options(all_clubs=True).filter_by(status="active").all()
        for c in clubs:
            with use_club(c):
                click.echo(f"{c.slug}: {send_reminders()} Erinnerungen")

    @app.cli.command("telegram-set-webhook")
    def tg_webhook():
        from .services import telegram
        url = f"{app.config['BASE_URL']}/webhooks/telegram"
        telegram.api("setWebhook", url=url, secret_token=app.config["TELEGRAM_WEBHOOK_SECRET"],
                     allowed_updates=["message", "chat_member"], drop_pending_updates=True)
        click.echo(f"Webhook gesetzt: {url}")

    @app.cli.command("telegram-poll")
    @_club_option
    def tg_poll(club_slug):
        """Lokale Entwicklung ohne öffentliche URL (Telegram-Bot gehört derzeit zu einem Klub)."""
        from .services import telegram
        with use_club(_club_by_slug(club_slug)):
            telegram.poll_forever()
