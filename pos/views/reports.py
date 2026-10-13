import csv
import io
from datetime import date, timedelta

from flask import Blueprint, Response, render_template, request

from .. import reports as r
from ..auth import login_required
from ..db import get_db
from ..money import fmt, fmt_qty

bp = Blueprint("reports", __name__, url_prefix="/reports")

PRESETS = {"today": 0, "7d": 6, "30d": 29}


def _range():
    preset = request.args.get("preset")
    today = date.today()
    if preset in PRESETS:
        return (today - timedelta(days=PRESETS[preset])).isoformat(), today.isoformat()
    if preset == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    start = request.args.get("start") or today.isoformat()
    end = request.args.get("end") or start
    return (start, end) if start <= end else (end, start)


@bp.get("/")
@login_required("manager")
def index():
    db = get_db()
    start, end = _range()
    days = r.by_day(db, start, end)
    return render_template(
        "reports.html", start=start, end=end, sm=r.summary(db, start, end), days=days,
        day_max=max([d["total"] for d in days] or [1]), hours=r.by_hour(db, start, end),
        products=r.by_product(db, start, end, 50), categories=r.by_category(db, start, end),
        payments=r.by_payment(db, start, end), cashiers=r.by_cashier(db, start, end),
        attendants=r.by_attendant(db, start, end), tables=r.by_table(db, start, end),
        low=r.low_stock(db), expiring=r.expiring(db), valuation=r.stock_valuation(db))


@bp.get("/z-report")
@login_required("manager")
def z_report():
    db = get_db()
    day = request.args.get("day") or date.today().isoformat()
    return render_template("z_report.html", day=day, sm=r.summary(db, day, day), payments=r.by_payment(db, day, day),
                           cashiers=r.by_cashier(db, day, day), categories=r.by_category(db, day, day))


@bp.get("/export/<kind>.csv")
@login_required("manager")
def export(kind):
    db = get_db()
    start, end = _range()
    out = io.StringIO()
    w = csv.writer(out)
    if kind == "sales":
        w.writerow(["Receipt", "Completed", "Cashier", "Gross", "Discount", "Net", "VAT", "Service", "Total",
                    "Payments"])
        for s in db.execute("""SELECT s.*, u.full_name cashier, (SELECT GROUP_CONCAT(p.method || ':' ||
            COALESCE(p.reference,'') || ':' || p.amount, ' | ') FROM payments p WHERE p.sale_id = s.id AND
            p.status='completed') pays FROM sales s JOIN users u ON u.id = s.cashier_id
            WHERE s.status='completed' AND date(s.completed_at) BETWEEN ? AND ? ORDER BY s.id""", (start, end)):
            w.writerow([s["receipt_no"], s["completed_at"], s["cashier"], fmt(s["gross"]), fmt(s["discount"]),
                        fmt(s["net"]), fmt(s["tax"]), fmt(s["service_charge"]), fmt(s["total"]), s["pays"]])
    elif kind == "products":
        w.writerow(["Item", "Qty", "Sales", "Cost", "Margin"])
        for p in r.by_product(db, start, end):
            w.writerow([p["name"], fmt_qty(p["qty"]), fmt(p["sales"]), fmt(p["cost"]), fmt(p["sales"] - p["cost"])])
    elif kind == "items":
        w.writerow(["Receipt", "Date", "Item", "Qty", "Unit price", "Discount", "Net", "VAT", "VAT rate"])
        for i in db.execute("""SELECT s.receipt_no, s.completed_at, i.* FROM sale_items i JOIN sales s ON
            s.id = i.sale_id WHERE s.status='completed' AND date(s.completed_at) BETWEEN ? AND ? ORDER BY s.id""",
                            (start, end)):
            w.writerow([i["receipt_no"], i["completed_at"], i["name"], fmt_qty(i["qty"]), fmt(i["unit_price"]),
                        fmt(i["line_discount"]), fmt(i["line_net"]), fmt(i["line_tax"]), i["tax_rate"]])
    else:
        return "Unknown export", 404
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={kind}_{start}_{end}.csv"})
