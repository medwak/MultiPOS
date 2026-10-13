"""Admin & manager screens: settings, industry, catalogue, stock, people."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import sales as svc
from ..auth import ROLES, create_user, login_required
from ..db import audit, get_db, now, transaction
from ..industries import FEATURES, INDUSTRIES
from ..money import to_cents
from ..mpesa import MpesaClient, MpesaError
from ..printing import PrintError, send
from ..settings import SECRET_KEYS, apply_industry, features, get_all, set_many
from werkzeug.security import generate_password_hash

bp = Blueprint("admin", __name__, url_prefix="/admin")

SETTINGS_FIELDS = ["business_name", "business_address", "business_phone", "business_email", "kra_pin", "currency",
                   "default_tax_rate", "service_charge_rate", "receipt_prefix", "receipt_footer", "receipt_width",
                   "printer_target", "kitchen_printer_target", "mpesa_mode", "mpesa_consumer_key",
                   "mpesa_consumer_secret", "mpesa_shortcode", "mpesa_passkey", "mpesa_transaction_type",
                   "mpesa_party_b", "mpesa_callback_base"]
CHECKBOXES = ["prices_include_tax", "auto_print", "allow_negative_stock"]


# ---------- settings ----------

@bp.route("/settings", methods=["GET", "POST"])
@login_required("admin")
def settings():
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "industry":
            ind = f.get("industry")
            if ind in INDUSTRIES:
                with transaction(db):
                    apply_industry(db, ind, seed_categories=bool(f.get("seed_categories")),
                                   seed_samples=bool(f.get("seed_samples")))
                    audit(db, g.user["id"], "industry", ind)
                flash(f"Switched to {INDUSTRIES[ind]['label']}. Features were set to that industry's defaults —"
                      " fine-tune them below.", "ok")
        elif action == "features":
            set_many(db, {f"feature.{k}": "1" if f.get(k) else "0" for k in FEATURES})
            flash("Features saved", "ok")
        else:
            vals = {}
            for k in SETTINGS_FIELDS:
                if k in f:
                    v = f.get(k, "").strip()
                    if k in SECRET_KEYS and v == "********":
                        continue  # unchanged secret
                    vals[k] = v
            for k in CHECKBOXES:
                if f.get("_has_" + k):
                    vals[k] = "1" if f.get(k) else "0"
            try:
                for k in ("default_tax_rate", "service_charge_rate"):
                    if k in vals and not (0 <= float(vals[k] or 0) <= 100):
                        raise ValueError
            except ValueError:
                flash("Rates must be numbers between 0 and 100", "error")
                return redirect(url_for("admin.settings", tab=f.get("tab")))
            if vals.get("mpesa_callback_base") and not vals["mpesa_callback_base"].startswith("https://"):
                flash("The callback base URL must start with https://", "error")
                return redirect(url_for("admin.settings", tab="mpesa"))
            set_many(db, vals)
            audit(db, g.user["id"], "settings", ",".join(sorted(vals)))
            flash("Settings saved", "ok")
        return redirect(url_for("admin.settings", tab=f.get("tab", "business")))
    s = get_all(db)
    masked = {k: ("********" if s.get(k) else "") for k in SECRET_KEYS}
    client = MpesaClient(s)
    return render_template("admin/settings.html", tab=request.args.get("tab", "business"), industries=INDUSTRIES,
                           features_meta=FEATURES, feat=features(s), masked=masked, callback_url=client.callback_url,
                           host_url=request.host_url)


@bp.post("/settings/test-mpesa")
@login_required("admin")
def test_mpesa():
    client = MpesaClient(get_all(get_db()))
    try:
        if client.simulated:
            flash("M-Pesa is in SIMULATE mode — no connection needed. Payments complete automatically after ~4 s.",
                  "ok")
        else:
            client.check_config()
            client._token()
            flash(f"Connected to Safaricom {client.mode} successfully. Your keys work.", "ok")
    except MpesaError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.settings", tab="mpesa"))


@bp.post("/settings/test-print")
@login_required("admin")
def test_print():
    target = request.form.get("target") or get_all(get_db())["printer_target"]
    if target == "browser":
        flash("Browser printing: make a sale and press Print — your browser's print dialog opens.", "ok")
    else:
        try:
            send(target, b"\x1b@\x1ba\x01MultiPOS test print\nPrinter OK\n\n\n\x1dVB\x00")
            flash(f"Test page sent to {target}", "ok")
        except PrintError as e:
            flash(str(e), "error")
    return redirect(url_for("admin.settings", tab="printing"))


# ---------- products ----------

@bp.get("/products")
@login_required("manager")
def products():
    db = get_db()
    q = (request.args.get("q") or "").strip()
    cat = request.args.get("category", "")
    where, args = ["1=1"], []
    if q:
        where.append("(p.name LIKE ? OR p.sku LIKE ? OR p.barcode = ?)"); args += [f"%{q}%", f"%{q}%", q]
    if cat:
        where.append("p.category_id = ?"); args.append(cat)
    rows = db.execute(f"""SELECT p.*, c.name cat FROM products p LEFT JOIN categories c ON c.id = p.category_id
        WHERE {' AND '.join(where)} ORDER BY p.active DESC, c.sort_order, p.name""", args).fetchall()
    cats = db.execute("SELECT * FROM categories ORDER BY sort_order, name").fetchall()
    return render_template("admin/products.html", rows=rows, cats=cats, q=q, cat=cat)


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:pid>", methods=["GET", "POST"])
@login_required("manager")
def product_edit(pid=None):
    db = get_db()
    s = get_all(db)
    p = db.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone() if pid else None
    if pid and not p:
        abort(404)
    if request.method == "POST":
        f = request.form
        try:
            name = f.get("name", "").strip()
            if not name:
                raise ValueError("Name is required")
            barcode = f.get("barcode", "").strip() or None
            if barcode and db.execute("SELECT 1 FROM products WHERE barcode = ? AND id != ?",
                                      (barcode, pid or 0)).fetchone():
                raise ValueError("Another product already has that barcode")
            tax = float(f.get("tax_rate") or 0)
            if not 0 <= tax <= 100:
                raise ValueError("Tax rate must be 0-100")
            vals = dict(
                name=name, sku=f.get("sku", "").strip() or None, barcode=barcode,
                category_id=f.get("category_id") or None, price=to_cents(f.get("price")),
                cost=to_cents(f.get("cost")), tax_rate=tax, is_service=int(bool(f.get("is_service"))),
                track_stock=int(bool(f.get("track_stock")) and not f.get("is_service")),
                reorder_level=float(f.get("reorder_level") or 0), unit=(f.get("unit") or "pc").strip()[:10],
                allow_decimal=int(bool(f.get("allow_decimal"))), expiry_date=f.get("expiry_date") or None,
                batch_no=f.get("batch_no", "").strip() or None,
                requires_prescription=int(bool(f.get("requires_prescription"))),
                send_to_kitchen=int(bool(f.get("send_to_kitchen"))), active=int(bool(f.get("active"))),
            )
        except ValueError as e:
            flash(str(e), "error")
            return render_template("admin/product_form.html", p=request.form, pid=pid, cats=_cats(db))
        with transaction(db):
            if pid:
                db.execute(f"UPDATE products SET {', '.join(k + ' = ?' for k in vals)} WHERE id = ?",
                           (*vals.values(), pid))
            else:
                opening = float(f.get("opening_stock") or 0)
                cur = db.execute(f"INSERT INTO products ({', '.join(vals)}, stock_qty, created_at) VALUES"
                                 f" ({', '.join('?' * len(vals))}, 0, ?)", (*vals.values(), now()))
                pid = cur.lastrowid
        if not p and float(request.form.get("opening_stock") or 0) > 0 and vals["track_stock"]:
            # Opening stock is capitalised against owner's capital.
            try:
                svc.receive_stock(db, pid, float(request.form["opening_stock"]), vals["cost"], "capital",
                                  g.user["id"], "Opening stock")
            except svc.SaleError as e:
                flash(str(e), "error")
        flash("Saved", "ok")
        return redirect(url_for("admin.products"))
    default = {"tax_rate": s["default_tax_rate"], "unit": "pc", "track_stock": 1, "active": 1, "send_to_kitchen": 1}
    return render_template("admin/product_form.html", p=p or default, pid=pid, cats=_cats(db))


def _cats(db):
    return db.execute("SELECT * FROM categories WHERE active = 1 ORDER BY sort_order, name").fetchall()


# ---------- simple lists: categories, tables, attendants ----------

LISTS = {
    "categories": ("Categories", ["name", "sort_order"]),
    "tables": ("Tables", ["name", "seats"]),
    "attendants": ("Attendants", ["name", "commission_rate"]),
}
TABLE_NAMES = {"categories": "categories", "tables": "dining_tables", "attendants": "attendants"}


@bp.route("/list/<kind>", methods=["GET", "POST"])
@login_required("manager")
def simple_list(kind):
    if kind not in LISTS:
        abort(404)
    db = get_db()
    table = TABLE_NAMES[kind]
    title, cols = LISTS[kind]
    if request.method == "POST":
        f = request.form
        if f.get("toggle"):
            db.execute(f"UPDATE {table} SET active = 1 - active WHERE id = ?", (f["toggle"],))
        else:
            name = f.get("name", "").strip()
            if name:
                try:
                    extra = float(f.get(cols[1]) or 0)
                except ValueError:
                    extra = 0
                if f.get("id"):
                    db.execute(f"UPDATE {table} SET name = ?, {cols[1]} = ? WHERE id = ?", (name, extra, f["id"]))
                else:
                    db.execute(f"INSERT INTO {table} (name, {cols[1]}) VALUES (?, ?)", (name, extra))
        return redirect(url_for("admin.simple_list", kind=kind))
    rows = db.execute(f"SELECT * FROM {table} ORDER BY active DESC, {cols[1] if kind == 'categories' else 'id'}, name"
                      ).fetchall()
    return render_template("admin/simple_list.html", kind=kind, title=title, cols=cols, rows=rows)


# ---------- stock ----------

@bp.route("/stock", methods=["GET", "POST"])
@login_required("manager")
def stock():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            pid = int(f.get("product_id") or 0)
            if f.get("action") == "receive":
                svc.receive_stock(db, pid, float(f.get("qty") or 0), to_cents(f.get("unit_cost")),
                                  f.get("paid_from"), g.user["id"], f.get("note", "").strip())
                flash("Stock received and posted to the accounts", "ok")
            else:
                svc.adjust_stock(db, pid, float(f.get("new_qty")), g.user["id"],
                                 f.get("note", "").strip() or "Stock count")
                flash("Stock adjusted", "ok")
        except (ValueError, TypeError, svc.SaleError) as e:
            flash(str(e) or "Check the numbers", "error")
        return redirect(url_for("admin.stock"))
    prods = db.execute("SELECT id, name, stock_qty, unit, cost FROM products WHERE active = 1 AND track_stock = 1"
                       " AND is_service = 0 ORDER BY name").fetchall()
    moves = db.execute("""SELECT m.*, p.name, p.unit, u.full_name who FROM stock_movements m
        JOIN products p ON p.id = m.product_id LEFT JOIN users u ON u.id = m.created_by
        ORDER BY m.id DESC LIMIT 200""").fetchall()
    return render_template("admin/stock.html", prods=prods, moves=moves)


# ---------- users ----------

@bp.route("/users", methods=["GET", "POST"])
@login_required("admin")
def users():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            if f.get("action") == "create":
                create_user(db, f.get("username"), f.get("full_name"), f.get("password"), f.get("role"))
                flash("User added", "ok")
            elif f.get("action") == "toggle":
                if int(f["id"]) == g.user["id"]:
                    raise ValueError("You cannot deactivate yourself")
                db.execute("UPDATE users SET active = 1 - active WHERE id = ?", (f["id"],))
            elif f.get("action") == "reset":
                if len(f.get("password", "")) < 4:
                    raise ValueError("Password must be at least 4 characters")
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (generate_password_hash(f["password"]), f["id"]))
                flash("Password changed", "ok")
            elif f.get("action") == "role":
                if f.get("role") not in ROLES or int(f["id"]) == g.user["id"]:
                    raise ValueError("Cannot change that role")
                db.execute("UPDATE users SET role = ? WHERE id = ?", (f["role"], f["id"]))
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:  # unique username
            flash("That username is taken" if "UNIQUE" in str(e) else str(e), "error")
        return redirect(url_for("admin.users"))
    rows = db.execute("SELECT * FROM users ORDER BY active DESC, role, username").fetchall()
    return render_template("admin/users.html", rows=rows, roles=list(ROLES))


@bp.get("/audit")
@login_required("admin")
def audit_log():
    rows = get_db().execute("""SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
        ORDER BY a.id DESC LIMIT 300""").fetchall()
    return render_template("admin/audit.html", rows=rows)
