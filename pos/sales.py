"""Sale lifecycle: open order -> payments -> completed (stock + ledger) -> optional void."""
import json
import math
import re

from . import accounting
from .db import audit, now, transaction
from .money import compute_totals, fmt
from .settings import features, get_all


class SaleError(ValueError):
    pass


MPESA_CODE_RE = re.compile(r"^[A-Z0-9]{10}$")


def _sale(db, sale_id):
    sale = db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    if not sale:
        raise SaleError("Sale not found")
    return sale


# ---------- building an order ----------

def save_order(db, user_id, payload, sale_id=None):
    """Create a new order or replace the lines of an open one. Returns sale id."""
    s = get_all(db)
    feat = features(s)
    items = payload.get("items") or []
    if not items:
        raise SaleError("The order has no items")

    with transaction(db):
        if sale_id:
            sale = _sale(db, sale_id)
            if sale["status"] != "open":
                raise SaleError("Only open orders can be changed")
            existing = {r["id"]: r for r in db.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,))}
        else:
            existing = {}

        lines, needs_rx = [], False
        for raw in items:
            product = db.execute("SELECT * FROM products WHERE id = ? AND active = 1",
                                 (raw.get("product_id"),)).fetchone()
            if not product:
                raise SaleError("A product in the order no longer exists")
            try:
                qty = float(raw.get("qty", 1))
            except (TypeError, ValueError):
                raise SaleError(f"Invalid quantity for {product['name']}")
            if qty <= 0 or not math.isfinite(qty):
                raise SaleError(f"Quantity for {product['name']} must be more than zero")
            if qty != int(qty) and not (feat["decimal_qty"] and product["allow_decimal"]):
                raise SaleError(f"{product['name']} must be sold in whole units")
            if feat["prescriptions"] and product["requires_prescription"]:
                needs_rx = True
            attendant = raw.get("attendant_id") or None
            if feat["attendants"] and product["is_service"] and not attendant:
                raise SaleError(f"Choose an attendant for {product['name']}")
            lines.append({"product": product, "qty": qty, "unit_price": product["price"],
                          "tax_rate": product["tax_rate"], "note": (raw.get("note") or "")[:200] if feat["item_notes"] else "",
                          "attendant_id": attendant if feat["attendants"] else None, "item_id": raw.get("item_id")})

        if needs_rx and not (payload.get("prescription_ref") or "").strip():
            raise SaleError("This order has prescription-only items: enter the prescription reference")

        if feat["track_stock"] and s["allow_negative_stock"] != "1":
            wanted = {}
            for l in lines:
                if l["product"]["track_stock"] and not l["product"]["is_service"]:
                    wanted[l["product"]["id"]] = wanted.get(l["product"]["id"], 0) + l["qty"]
            for pid, q in wanted.items():
                p = db.execute("SELECT name, stock_qty, unit FROM products WHERE id = ?", (pid,)).fetchone()
                if q > p["stock_qty"] + 1e-9:
                    raise SaleError(f"Not enough stock for {p['name']}: {p['stock_qty']:g} {p['unit']} left")

        discount = int(payload.get("discount") or 0)
        service_rate = float(s["service_charge_rate"]) if feat["service_charge"] else 0
        results, t = compute_totals(lines, discount, s["prices_include_tax"] == "1", service_rate)

        fields = dict(
            table_id=(payload.get("table_id") or None) if feat["tables"] else None,
            order_label=(payload.get("order_label") or "")[:60],
            customer_name=(payload.get("customer_name") or "")[:100],
            customer_phone=(payload.get("customer_phone") or "")[:20],
            prescription_ref=(payload.get("prescription_ref") or "")[:60],
            notes=(payload.get("notes") or "")[:300],
            **t,
        )
        if sale_id:
            sets = ", ".join(f"{k} = ?" for k in fields)
            db.execute(f"UPDATE sales SET {sets} WHERE id = ?", (*fields.values(), sale_id))
        else:
            cols = ", ".join(fields)
            cur = db.execute(f"INSERT INTO sales (status, cashier_id, created_at, {cols}) VALUES "
                             f"('open', ?, ?, {', '.join('?' * len(fields))})", (user_id, now(), *fields.values()))
            sale_id = cur.lastrowid
            db.execute("UPDATE sales SET receipt_no = ? WHERE id = ?", (f"{s['receipt_prefix']}{sale_id:06d}", sale_id))

        keep = set()
        for l, r in zip(lines, results):
            p = l["product"]
            vals = (p["id"], p["name"], l["qty"], p["unit"], l["unit_price"], p["cost"], l["tax_rate"],
                    r["line_gross"], r["line_discount"], r["line_net"], r["line_tax"], l["attendant_id"], l["note"])
            if l["item_id"] and int(l["item_id"]) in existing:
                keep.add(int(l["item_id"]))
                db.execute("UPDATE sale_items SET product_id=?, name=?, qty=?, unit=?, unit_price=?, unit_cost=?,"
                           " tax_rate=?, line_gross=?, line_discount=?, line_net=?, line_tax=?, attendant_id=?, note=?"
                           " WHERE id = ?", (*vals, int(l["item_id"])))
            else:
                db.execute("INSERT INTO sale_items (product_id, name, qty, unit, unit_price, unit_cost, tax_rate,"
                           " line_gross, line_discount, line_net, line_tax, attendant_id, note, sale_id)"
                           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*vals, sale_id))
        for item_id in set(existing) - keep:
            db.execute("DELETE FROM sale_items WHERE id = ?", (item_id,))
    return sale_id


def kitchen_pending(db, sale_id):
    """Lines (or extra quantity) not yet sent to the kitchen."""
    rows = db.execute("SELECT i.*, p.send_to_kitchen FROM sale_items i LEFT JOIN products p ON p.id = i.product_id"
                      " WHERE i.sale_id = ?", (sale_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "qty": r["qty"] - r["kitchen_sent"], "note": r["note"]}
            for r in rows if r["send_to_kitchen"] and r["qty"] - r["kitchen_sent"] > 1e-9]


def mark_kitchen_sent(db, sale_id):
    db.execute("UPDATE sale_items SET kitchen_sent = qty WHERE sale_id = ?", (sale_id,))


# ---------- payments ----------

def balance_due(db, sale_id):
    sale = _sale(db, sale_id)
    paid = db.execute("SELECT COALESCE(SUM(amount),0) a FROM payments WHERE sale_id = ? AND status='completed'",
                      (sale_id,)).fetchone()["a"]
    return sale["total"] - paid


def _check_payable(sale):
    if sale["status"] in ("completed", "voided"):
        raise SaleError(f"This sale is already {sale['status']}")


def pay_cash(db, sale_id, tendered, user_id):
    with transaction(db):
        sale = _sale(db, sale_id)
        _check_payable(sale)
        due = balance_due(db, sale_id)
        if tendered <= 0:
            raise SaleError("Enter the cash amount received")
        applied = min(tendered, due)
        change = tendered - applied
        db.execute("INSERT INTO payments (sale_id, method, amount, tendered, change_given, status, created_by,"
                   " created_at, completed_at) VALUES (?, 'cash', ?, ?, ?, 'completed', ?, ?, ?)",
                   (sale_id, applied, tendered, change, user_id, now(), now()))
        db.execute("UPDATE sales SET change_due = change_due + ? WHERE id = ?", (change, sale_id))
        _try_finalize(db, sale_id, user_id)


def pay_mpesa_manual(db, sale_id, amount, code, user_id):
    """Customer paid to the till/paybill directly and the cashier types the confirmation code."""
    code = (code or "").strip().upper()
    if not MPESA_CODE_RE.match(code):
        raise SaleError("M-Pesa codes are 10 letters/digits, e.g. TJ7K2ABC9D")
    with transaction(db):
        sale = _sale(db, sale_id)
        _check_payable(sale)
        if db.execute("SELECT 1 FROM payments WHERE reference = ? AND status = 'completed'", (code,)).fetchone():
            raise SaleError(f"M-Pesa code {code} has already been used on another sale")
        due = balance_due(db, sale_id)
        if amount <= 0:
            raise SaleError("Enter the amount received")
        applied = min(amount, due)
        db.execute("INSERT INTO payments (sale_id, method, amount, tendered, change_given, reference, status,"
                   " created_by, created_at, completed_at) VALUES (?, 'mpesa_manual', ?, ?, ?, ?, 'completed', ?, ?, ?)",
                   (sale_id, applied, amount, amount - applied, code, user_id, now(), now()))
        db.execute("UPDATE sales SET change_due = change_due + ? WHERE id = ?", (amount - applied, sale_id))
        _try_finalize(db, sale_id, user_id)


def start_mpesa(db, sale_id, phone, amount, user_id, client):
    """Send an STK push. amount in cents (rounded UP to whole shillings, as Daraja requires)."""
    sale = _sale(db, sale_id)
    _check_payable(sale)
    due = balance_due(db, sale_id)
    amount = min(amount or due, due)
    if amount <= 0:
        raise SaleError("Nothing left to pay")
    if db.execute("SELECT 1 FROM payments WHERE sale_id = ? AND status = 'pending'", (sale_id,)).fetchone():
        raise SaleError("An M-Pesa request is already waiting on this sale; cancel it first")
    shillings = math.ceil(amount / 100)
    resp = client.stk_push(phone, shillings, sale["receipt_no"], f"Payment {sale['receipt_no']}")
    with transaction(db):
        cur = db.execute(
            "INSERT INTO payments (sale_id, method, amount, phone, status, checkout_request_id, merchant_request_id,"
            " result_desc, raw, created_by, created_at) VALUES (?, 'mpesa', ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (sale_id, shillings * 100, resp["phone"], resp["CheckoutRequestID"], resp.get("MerchantRequestID"),
             resp.get("CustomerMessage"), json.dumps(resp), user_id, now()))
        db.execute("UPDATE sales SET status = 'pending_payment' WHERE id = ? AND status = 'open'", (sale_id,))
    return cur.lastrowid


def complete_mpesa(db, checkout_request_id, receipt_no, amount_kes, phone, raw):
    """Called from the Daraja callback (or STK query). Idempotent."""
    with transaction(db):
        p = db.execute("SELECT * FROM payments WHERE checkout_request_id = ?", (checkout_request_id,)).fetchone()
        if not p or p["status"] == "completed":
            return p
        received = int(round(float(amount_kes) * 100)) if amount_kes is not None else p["amount"]
        sale = _sale(db, p["sale_id"])
        due = sale["total"] - db.execute("SELECT COALESCE(SUM(amount),0) a FROM payments WHERE sale_id = ? AND"
                                         " status='completed'", (sale["id"],)).fetchone()["a"]
        applied = min(received, max(due, 0))
        db.execute("UPDATE payments SET status='completed', amount=?, tendered=?, change_given=?, reference=?,"
                   " phone=COALESCE(?, phone), raw=?, result_desc='Paid', completed_at=? WHERE id = ?",
                   (applied, received, received - applied, receipt_no, phone, json.dumps(raw), now(), p["id"]))
        db.execute("UPDATE sales SET change_due = change_due + ? WHERE id = ?", (received - applied, sale["id"]))
        if sale["status"] == "voided":
            # Money arrived after the sale was voided: keep the record so it can be refunded.
            audit(db, None, "mpesa_after_void", f"{receipt_no} on {sale['receipt_no']}")
        else:
            _try_finalize(db, sale["id"], p["created_by"])
        return p


def fail_mpesa(db, checkout_request_id, desc, raw=None, status="failed"):
    with transaction(db):
        p = db.execute("SELECT * FROM payments WHERE checkout_request_id = ?", (checkout_request_id,)).fetchone()
        if not p or p["status"] != "pending":
            return
        db.execute("UPDATE payments SET status=?, result_desc=?, raw=COALESCE(?, raw) WHERE id = ?",
                   (status, desc, json.dumps(raw) if raw else None, p["id"]))
        _reopen_if_unpaid(db, p["sale_id"])


def cancel_pending(db, sale_id):
    with transaction(db):
        db.execute("UPDATE payments SET status='cancelled', result_desc='Cancelled at till' "
                   "WHERE sale_id = ? AND status='pending'", (sale_id,))
        _reopen_if_unpaid(db, sale_id)


def _reopen_if_unpaid(db, sale_id):
    pending = db.execute("SELECT 1 FROM payments WHERE sale_id = ? AND status='pending'", (sale_id,)).fetchone()
    if not pending:
        db.execute("UPDATE sales SET status='open' WHERE id = ? AND status='pending_payment'", (sale_id,))


def _try_finalize(db, sale_id, user_id):
    sale = _sale(db, sale_id)
    paid = db.execute("SELECT COALESCE(SUM(amount),0) a FROM payments WHERE sale_id = ? AND status='completed'",
                      (sale_id,)).fetchone()["a"]
    db.execute("UPDATE sales SET paid = ? WHERE id = ?", (paid, sale_id))
    if paid < sale["total"] or sale["status"] == "completed":
        if sale["status"] == "pending_payment":
            _reopen_if_unpaid(db, sale_id)
        return False
    ts = now()
    db.execute("UPDATE sales SET status='completed', completed_at=? WHERE id = ?", (ts, sale_id))
    db.execute("UPDATE payments SET status='cancelled', result_desc='Sale already paid' "
               "WHERE sale_id = ? AND status='pending'", (sale_id,))
    for it in db.execute("SELECT i.product_id, i.qty, i.unit_cost FROM sale_items i JOIN products p ON p.id = i.product_id"
                         " WHERE i.sale_id = ? AND p.track_stock = 1 AND p.is_service = 0", (sale_id,)).fetchall():
        db.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?", (it["qty"], it["product_id"]))
        db.execute("INSERT INTO stock_movements (product_id, qty_change, reason, ref_id, unit_cost, created_by,"
                   " created_at) VALUES (?, ?, 'sale', ?, ?, ?, ?)",
                   (it["product_id"], -it["qty"], sale_id, it["unit_cost"], user_id, ts))
    accounting.post_sale(db, sale_id, user_id)
    return True


# ---------- void ----------

def void_sale(db, sale_id, user_id, reason):
    reason = (reason or "").strip()
    if not reason:
        raise SaleError("Give a reason for the void")
    with transaction(db):
        sale = _sale(db, sale_id)
        if sale["status"] == "voided":
            raise SaleError("Already voided")
        if sale["status"] == "completed":
            accounting.reverse_ref(db, "sale", sale_id, user_id, reason)
            for m in db.execute("SELECT product_id, qty_change, unit_cost FROM stock_movements"
                                " WHERE reason='sale' AND ref_id = ?", (sale_id,)).fetchall():
                db.execute("UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                           (m["qty_change"], m["product_id"]))
                db.execute("INSERT INTO stock_movements (product_id, qty_change, reason, ref_id, unit_cost,"
                           " created_by, created_at) VALUES (?, ?, 'void', ?, ?, ?, ?)",
                           (m["product_id"], -m["qty_change"], sale_id, m["unit_cost"], user_id, now()))
        db.execute("UPDATE payments SET status='cancelled', result_desc='Sale voided' "
                   "WHERE sale_id = ? AND status='pending'", (sale_id,))
        db.execute("UPDATE sales SET status='voided', voided_at=?, voided_by=?, void_reason=? WHERE id = ?",
                   (now(), user_id, reason, sale_id))
        audit(db, user_id, "void_sale", f"{sale['receipt_no']} {fmt(sale['total'])}: {reason}")


# ---------- stock & expenses (they also post to the ledger) ----------

def receive_stock(db, product_id, qty, unit_cost, paid_from, user_id, note=""):
    if qty <= 0:
        raise SaleError("Quantity must be more than zero")
    if paid_from not in accounting.PAYMENT_ACCOUNTS:
        raise SaleError("Choose how the stock was paid for")
    with transaction(db):
        p = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not p:
            raise SaleError("Product not found")
        old_q = max(p["stock_qty"], 0)
        new_cost = round((old_q * p["cost"] + qty * unit_cost) / (old_q + qty)) if old_q + qty else unit_cost
        db.execute("UPDATE products SET stock_qty = stock_qty + ?, cost = ? WHERE id = ?", (qty, new_cost, product_id))
        cur = db.execute("INSERT INTO stock_movements (product_id, qty_change, reason, unit_cost, note, created_by,"
                         " created_at) VALUES (?, ?, 'purchase', ?, ?, ?, ?)",
                         (product_id, qty, unit_cost, note, user_id, now()))
        total = round(qty * unit_cost)
        accounting.post_entry(db, f"Stock purchase: {qty:g} x {p['name']}" + (f" ({note})" if note else ""),
                              [("1200", total, 0, p["name"]),
                               (accounting.PAYMENT_ACCOUNTS[paid_from], 0, total, paid_from)],
                              "stock", cur.lastrowid, user_id)


def adjust_stock(db, product_id, new_qty, user_id, note):
    """Stock count correction. Loss/gain is booked against 5100 at current cost."""
    with transaction(db):
        p = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        diff = new_qty - p["stock_qty"]
        if abs(diff) < 1e-9:
            return
        db.execute("UPDATE products SET stock_qty = ? WHERE id = ?", (new_qty, product_id))
        cur = db.execute("INSERT INTO stock_movements (product_id, qty_change, reason, unit_cost, note, created_by,"
                         " created_at) VALUES (?, ?, 'adjustment', ?, ?, ?, ?)",
                         (product_id, diff, p["cost"], note, user_id, now()))
        value = round(abs(diff) * p["cost"])
        if value:
            lines = ([("1200", value, 0, "Stock gain"), ("5100", 0, value, note)] if diff > 0 else
                     [("5100", value, 0, note), ("1200", 0, value, "Stock loss")])
            accounting.post_entry(db, f"Stock count adjustment: {p['name']} ({diff:+g})", lines,
                                  "stock", cur.lastrowid, user_id)


def record_expense(db, account_code, amount, paid_from, description, date, user_id):
    acc = db.execute("SELECT type FROM accounts WHERE code = ?", (account_code,)).fetchone()
    if not acc or acc["type"] != "expense":
        raise SaleError("Choose an expense account")
    if amount <= 0:
        raise SaleError("Amount must be more than zero")
    if paid_from not in accounting.PAYMENT_ACCOUNTS:
        raise SaleError("Choose how it was paid")
    with transaction(db):
        accounting.post_entry(db, description or "Expense",
                              [(account_code, amount, 0, description),
                               (accounting.PAYMENT_ACCOUNTS[paid_from], 0, amount, paid_from)],
                              "expense", None, user_id, date=date)
