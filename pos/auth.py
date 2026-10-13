import secrets
from functools import wraps

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import audit, get_db, now, transaction
from .industries import INDUSTRIES
from .settings import apply_industry, get_all, set_many

bp = Blueprint("auth", __name__)

ROLES = {"cashier": 1, "manager": 2, "admin": 3}
CSRF_EXEMPT = {"mpesa.callback"}


def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()


def check_csrf():
    if request.method in ("POST", "PUT", "DELETE") and request.endpoint not in CSRF_EXEMPT:
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not token or not secrets.compare_digest(token, session.get("csrf", "")):
            if request.is_json or request.path.startswith("/api/"):
                resp = jsonify(error="Your session expired. Reload the page.")
                resp.status_code = 400
                abort(resp)
            abort(400, "Form expired. Go back, reload the page and try again.")


def login_required(role="cashier"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                if request.path.startswith("/api/"):
                    return jsonify(error="Please log in again"), 401
                return redirect(url_for("auth.login", next=request.path))
            if ROLES[g.user["role"]] < ROLES[role]:
                if request.path.startswith("/api/"):
                    return jsonify(error="You don't have permission for that"), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def can(role):
    return g.user is not None and ROLES[g.user["role"]] >= ROLES[role]


def create_user(db, username, full_name, password, role):
    username = (username or "").strip().lower()
    if not username or not full_name or len(password or "") < 4:
        raise ValueError("Username, full name and a password of at least 4 characters are required")
    if role not in ROLES:
        raise ValueError("Invalid role")
    db.execute("INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
               (username, full_name.strip(), generate_password_hash(password), role, now()))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    if get_all(db)["setup_done"] == "1":
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        f = request.form
        industry = f.get("industry", "general")
        if industry not in INDUSTRIES:
            flash("Choose an industry", "error")
        else:
            try:
                with transaction(db):
                    create_user(db, f.get("username"), f.get("full_name"), f.get("password"), "admin")
                    set_many(db, {"business_name": f.get("business_name") or "My Business",
                                  "business_phone": f.get("business_phone", ""), "setup_done": "1"})
                    apply_industry(db, industry, seed_categories=True, seed_samples=bool(f.get("samples")))
                flash("Setup complete. Log in with your new admin account.", "ok")
                return redirect(url_for("auth.login"))
            except ValueError as e:
                flash(str(e), "error")
    return render_template("setup.html", industries=INDUSTRIES)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        db = get_db()
        u = db.execute("SELECT * FROM users WHERE username = ? AND active = 1",
                       ((request.form.get("username") or "").strip().lower(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            session["csrf"] = secrets.token_urlsafe(24)
            audit(db, u["id"], "login")
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.pos"))
        flash("Wrong username or password", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
