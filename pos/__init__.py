"""MultiPOS — a multi-industry point of sale with M-Pesa, receipts, reports and double-entry accounts."""
import os
import secrets

from flask import Flask, g, redirect, request, session, url_for

from . import db as dbm
from .money import fmt, fmt_qty
from .settings import ensure_defaults, features, get_all, terms


def _secret_key(instance_path):
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=os.environ.get("POS_DATABASE", os.path.join(app.instance_path, "pos.db")),
        SECRET_KEY=_secret_key(app.instance_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)

    with app.app_context():
        db = dbm.connect(app.config["DATABASE"])
        dbm.init_schema(db)
        ensure_defaults(db)
        db.close()

    app.teardown_appcontext(dbm.close_db)

    from .auth import bp as auth_bp, load_user, check_csrf
    from .views.main import bp as main_bp
    from .views.api import bp as api_bp
    from .views.admin import bp as admin_bp
    from .views.reports import bp as reports_bp
    from .views.accounting import bp as accounting_bp
    from .views.mpesa import bp as mpesa_bp
    for bp in (auth_bp, main_bp, api_bp, admin_bp, reports_bp, accounting_bp, mpesa_bp):
        app.register_blueprint(bp)

    @app.before_request
    def _before():
        load_user()
        check_csrf()
        if request.endpoint and not request.endpoint.startswith(("auth.", "static", "mpesa.")):
            if get_all(dbm.get_db())["setup_done"] != "1":
                return redirect(url_for("auth.setup"))

    @app.context_processor
    def _ctx():
        s = get_all(dbm.get_db())
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return {"S": s, "F": features(s), "T": terms(s), "user": g.get("user"), "csrf_token": session["csrf"]}

    app.jinja_env.filters["money"] = fmt
    app.jinja_env.filters["qty"] = fmt_qty
    return app
