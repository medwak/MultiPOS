from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import sales as svc
from ..auth import can, login_required
from ..db import get_db
from ..printing import receipt_data
from ..settings import get_all

bp = Blueprint("main", __name__)


@bp.get("/")
@login_required()
def index():
    return redirect(url_for("main.pos"))


@bp.get("/pos")
@login_required()
def pos():
    return render_template("pos.html")


@bp.get("/sales")
@login_required()
def sales_list():
    db = get_db()
    start = request.args.get("start") or date.today().isoformat()
    end = request.args.get("end") or start
    status = request.args.get("status", "")
    q = (request.args.get("q") or "").strip()
    where, args = ["date(s.created_at) BETWEEN ? AND ?"], [start, end]
    if status:
        where.append("s.status = ?"); args.append(status)
    if q:
        where.append("(s.receipt_no LIKE ? OR s.customer_name LIKE ? OR s.customer_phone LIKE ? OR EXISTS"
                     " (SELECT 1 FROM payments p WHERE p.sale_id = s.id AND p.reference LIKE ?))")
        args += [f"%{q}%"] * 4
    if not can("manager"):
        where.append("s.cashier_id = ?"); args.append(g.user["id"])
    rows = db.execute(f"""SELECT s.*, u.full_name cashier, t.name table_name,
        (SELECT GROUP_CONCAT(DISTINCT CASE method WHEN 'cash' THEN 'Cash' ELSE 'M-Pesa' END) FROM payments p
         WHERE p.sale_id = s.id AND p.status='completed') methods
        FROM sales s JOIN users u ON u.id = s.cashier_id LEFT JOIN dining_tables t ON t.id = s.table_id
        WHERE {' AND '.join(where)} ORDER BY s.id DESC LIMIT 500""", args).fetchall()
    return render_template("sales.html", rows=rows, start=start, end=end, status=status, q=q)


@bp.get("/sales/<int:sale_id>")
@login_required()
def sale_detail(sale_id):
    db = get_db()
    data = receipt_data(db, sale_id, get_all(db))
    if not data["sale"]:
        abort(404)
    if not can("manager") and data["sale"]["cashier_id"] != g.user["id"]:
        abort(403)
    all_pays = db.execute("SELECT * FROM payments WHERE sale_id = ? ORDER BY id", (sale_id,)).fetchall()
    journal = db.execute("""SELECT e.id, e.date, e.description, a.code, a.name, l.debit, l.credit
        FROM journal_entries e JOIN journal_lines l ON l.entry_id = e.id JOIN accounts a ON a.id = l.account_id
        WHERE e.ref_type = 'sale' AND e.ref_id = ? ORDER BY e.id, l.id""", (sale_id,)).fetchall()
    return render_template("sale_detail.html", d=data, all_pays=all_pays, journal=journal)


@bp.post("/sales/<int:sale_id>/void")
@login_required("manager")
def void(sale_id):
    try:
        svc.void_sale(get_db(), sale_id, g.user["id"], request.form.get("reason"))
        flash("Sale voided. Stock returned and accounts reversed. Refund any M-Pesa payment from your M-Pesa portal.",
              "ok")
    except svc.SaleError as e:
        flash(str(e), "error")
    return redirect(url_for("main.sale_detail", sale_id=sale_id))


@bp.get("/receipt/<int:sale_id>")
@login_required()
def receipt(sale_id):
    db = get_db()
    data = receipt_data(db, sale_id, get_all(db))
    if not data["sale"]:
        abort(404)
    return render_template("receipt.html", d=data, autoprint=request.args.get("print") == "1")


@bp.get("/kitchen/<int:sale_id>")
@login_required()
def kitchen(sale_id):
    db = get_db()
    sale = db.execute("SELECT s.*, t.name table_name, u.full_name cashier FROM sales s LEFT JOIN dining_tables t"
                      " ON t.id = s.table_id JOIN users u ON u.id = s.cashier_id WHERE s.id = ?",
                      (sale_id,)).fetchone()
    if not sale:
        abort(404)
    lines = svc.kitchen_pending(db, sale_id)
    reprint = False
    if not lines:  # nothing new: show the whole order as a reprint
        reprint = True
        lines = [{"name": r["name"], "qty": r["qty"], "note": r["note"]} for r in
                 db.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))]
    else:
        svc.mark_kitchen_sent(db, sale_id)
    return render_template("kitchen.html", sale=sale, lines=lines, reprint=reprint)
