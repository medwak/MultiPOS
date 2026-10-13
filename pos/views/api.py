"""JSON API used by the till screen."""
import secrets
import string
from datetime import datetime, timedelta

from flask import Blueprint, g, jsonify, request

from .. import sales as svc
from ..auth import can, login_required
from ..db import get_db, now
from ..money import to_cents
from ..mpesa import MpesaClient, MpesaError
from ..printing import PrintError, kitchen_escpos, receipt_data, receipt_escpos, send
from ..settings import features, get_all

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.errorhandler(svc.SaleError)
@bp.errorhandler(MpesaError)
@bp.errorhandler(PrintError)
def _handled(e):
    return jsonify(error=str(e)), 400


def _order_json(db, sale_id):
    s = db.execute("SELECT s.*, t.name AS table_name FROM sales s LEFT JOIN dining_tables t ON t.id = s.table_id"
                   " WHERE s.id = ?", (sale_id,)).fetchone()
    if not s:
        raise svc.SaleError("Order not found")
    items = db.execute("SELECT id, product_id, name, qty, unit, unit_price, line_gross, note, attendant_id,"
                       " kitchen_sent FROM sale_items WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall()
    pays = db.execute("SELECT id, method, amount, tendered, change_given, reference, phone, status, result_desc"
                      " FROM payments WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall()
    d = dict(s)
    d["items"] = [dict(i) for i in items]
    d["payments"] = [dict(p) for p in pays]
    d["balance"] = s["total"] - s["paid"]
    return d


@bp.get("/catalog")
@login_required()
def catalog():
    db = get_db()
    cats = db.execute("SELECT id, name FROM categories WHERE active = 1 ORDER BY sort_order, name").fetchall()
    prods = db.execute("SELECT id, name, sku, barcode, category_id, price, tax_rate, is_service, track_stock,"
                       " stock_qty, unit, allow_decimal, requires_prescription FROM products WHERE active = 1"
                       " ORDER BY name").fetchall()
    tables = db.execute("SELECT id, name, seats FROM dining_tables WHERE active = 1 ORDER BY id").fetchall()
    atts = db.execute("SELECT id, name FROM attendants WHERE active = 1 ORDER BY name").fetchall()
    busy = {r["table_id"]: r["id"] for r in db.execute(
        "SELECT id, table_id FROM sales WHERE status IN ('open','pending_payment') AND table_id IS NOT NULL")}
    return jsonify(categories=[dict(c) for c in cats], products=[dict(p) for p in prods],
                   tables=[{**dict(t), "order_id": busy.get(t["id"])} for t in tables],
                   attendants=[dict(a) for a in atts])


@bp.get("/orders")
@login_required()
def open_orders():
    db = get_db()
    rows = db.execute("""SELECT s.id, s.receipt_no, s.status, s.total, s.paid, s.order_label, s.customer_name,
        s.created_at, t.name AS table_name, u.full_name AS cashier FROM sales s
        LEFT JOIN dining_tables t ON t.id = s.table_id JOIN users u ON u.id = s.cashier_id
        WHERE s.status IN ('open','pending_payment') ORDER BY s.id DESC""").fetchall()
    return jsonify(orders=[dict(r) for r in rows])


def _payload():
    data = request.get_json(force=True, silent=True) or {}
    try:
        data["discount"] = to_cents(data.get("discount"))
    except ValueError as e:
        raise svc.SaleError(str(e))
    return data


@bp.post("/orders")
@login_required()
def create_order():
    db = get_db()
    data = _payload()
    _limit_discount(db, data)
    sale_id = svc.save_order(db, g.user["id"], data)
    return jsonify(_order_json(db, sale_id))


@bp.put("/orders/<int:sale_id>")
@login_required()
def update_order(sale_id):
    db = get_db()
    data = _payload()
    _limit_discount(db, data)
    svc.save_order(db, g.user["id"], data, sale_id)
    return jsonify(_order_json(db, sale_id))


def _limit_discount(db, data):
    """Cashiers may discount at most 10% of the bill; managers and admins have no limit."""
    if can("manager") or not data.get("discount"):
        return
    gross = 0
    for it in data.get("items", []):
        p = db.execute("SELECT price FROM products WHERE id = ?", (it.get("product_id"),)).fetchone()
        if p:
            gross += p["price"] * float(it.get("qty") or 0)
    if data["discount"] > gross * 0.10 + 0.5:
        raise svc.SaleError("Cashiers can give at most 10% discount. Ask a manager.")


@bp.get("/orders/<int:sale_id>")
@login_required()
def get_order(sale_id):
    return jsonify(_order_json(get_db(), sale_id))


@bp.post("/orders/<int:sale_id>/pay/cash")
@login_required()
def pay_cash(sale_id):
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    svc.pay_cash(db, sale_id, _cents(data.get("tendered")), g.user["id"])
    return jsonify(_order_json(db, sale_id))


@bp.post("/orders/<int:sale_id>/pay/mpesa-code")
@login_required()
def pay_mpesa_code(sale_id):
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    svc.pay_mpesa_manual(db, sale_id, _cents(data.get("amount")), data.get("code"), g.user["id"])
    return jsonify(_order_json(db, sale_id))


@bp.post("/orders/<int:sale_id>/pay/mpesa")
@login_required()
def pay_mpesa(sale_id):
    db = get_db()
    data = request.get_json(force=True, silent=True) or {}
    client = MpesaClient(get_all(db))
    svc.start_mpesa(db, sale_id, data.get("phone"), _cents(data.get("amount")), g.user["id"], client)
    out = _order_json(db, sale_id)
    out["message"] = "Simulated STK push sent" if client.simulated else "Ask the customer to enter their M-Pesa PIN"
    return jsonify(out)


@bp.get("/orders/<int:sale_id>/status")
@login_required()
def status(sale_id):
    """Polled by the till while an STK push is waiting."""
    db = get_db()
    s = get_all(db)
    client = MpesaClient(s)
    for p in db.execute("SELECT * FROM payments WHERE sale_id = ? AND method = 'mpesa' AND status = 'pending'",
                        (sale_id,)).fetchall():
        age = (datetime.now() - datetime.strptime(p["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
        if client.simulated:
            if age >= 4:
                code = "SIM" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(7))
                svc.complete_mpesa(db, p["checkout_request_id"], code, p["amount"] / 100, p["phone"],
                                   {"simulated": True})
            continue
        last = p["last_query_at"]
        due = age > 12 and (not last or datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                            > timedelta(seconds=8))
        if not due:
            continue
        db.execute("UPDATE payments SET last_query_at = ? WHERE id = ?", (now(), p["id"]))
        try:
            state, desc = client.stk_query(p["checkout_request_id"])
        except MpesaError:
            continue  # network hiccup: keep waiting for the callback
        if state == "completed":
            svc.complete_mpesa(db, p["checkout_request_id"], None, None, None, {"confirmed_by": "stk_query",
                                                                                   "desc": desc})
        elif state in ("failed", "cancelled"):
            svc.fail_mpesa(db, p["checkout_request_id"], desc, status=state)
        elif age > 180:
            svc.fail_mpesa(db, p["checkout_request_id"], "No response from M-Pesa (timed out)")
    return jsonify(_order_json(db, sale_id))


@bp.post("/orders/<int:sale_id>/cancel-mpesa")
@login_required()
def cancel_mpesa(sale_id):
    db = get_db()
    svc.cancel_pending(db, sale_id)
    return jsonify(_order_json(db, sale_id))


@bp.post("/orders/<int:sale_id>/cancel")
@login_required()
def cancel_order(sale_id):
    """Cancel an unpaid open order (no money received)."""
    db = get_db()
    sale = db.execute("SELECT status, paid FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not sale or sale["status"] not in ("open", "pending_payment"):
        raise svc.SaleError("Only open orders can be cancelled here")
    if sale["paid"] and not can("manager"):
        raise svc.SaleError("This order has part payments. A manager must void it.")
    reason = (request.get_json(silent=True) or {}).get("reason") or "Cancelled at till"
    svc.void_sale(db, sale_id, g.user["id"], reason)
    return jsonify(ok=True)


@bp.post("/orders/<int:sale_id>/kitchen")
@login_required()
def send_kitchen(sale_id):
    db = get_db()
    s = get_all(db)
    if not features(s)["kitchen_tickets"]:
        raise svc.SaleError("Kitchen tickets are turned off")
    lines = svc.kitchen_pending(db, sale_id)
    if not lines:
        return jsonify(ok=True, printed=0, browser=False)
    target = s["kitchen_printer_target"]
    if target != "browser":
        sale = db.execute("SELECT s.*, t.name tn FROM sales s LEFT JOIN dining_tables t ON t.id = s.table_id"
                          " WHERE s.id = ?", (sale_id,)).fetchone()
        send(target, kitchen_escpos(sale, sale["tn"], lines, g.user["full_name"]))
        svc.mark_kitchen_sent(db, sale_id)
        return jsonify(ok=True, printed=len(lines), browser=False)
    # Browser printing: the page /kitchen/<id> renders the pending lines, then marks them sent.
    return jsonify(ok=True, printed=len(lines), browser=True)


@bp.post("/orders/<int:sale_id>/print")
@login_required()
def print_receipt(sale_id):
    db = get_db()
    s = get_all(db)
    if s["printer_target"] == "browser":
        return jsonify(ok=True, browser=True)
    data = receipt_data(db, sale_id, s)
    open_drawer = any(p["method"] == "cash" for p in data["payments"])
    send(s["printer_target"], receipt_escpos(data, open_drawer=open_drawer))
    return jsonify(ok=True, browser=False)


def _cents(v):
    try:
        return to_cents(v)
    except ValueError as e:
        raise svc.SaleError(str(e))
