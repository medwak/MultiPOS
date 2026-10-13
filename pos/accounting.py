"""Double-entry bookkeeping.

Every sale, void, stock purchase and expense posts a balanced journal entry, so the
trial balance, profit & loss and balance sheet are always derived from the ledger.
"""
from .db import now

PAYMENT_ACCOUNTS = {"cash": "1000", "mpesa": "1010", "mpesa_manual": "1010", "bank": "1020", "payable": "2000",
                    "capital": "3000"}  # 'capital' = owner brought it in (e.g. opening stock)
NORMAL_DEBIT = {"asset", "expense"}


class AccountingError(ValueError):
    pass


def account_id(db, code):
    row = db.execute("SELECT id FROM accounts WHERE code = ?", (code,)).fetchone()
    if not row:
        raise AccountingError(f"Account {code} does not exist")
    return row["id"]


def post_entry(db, description, lines, ref_type=None, ref_id=None, user_id=None, date=None, reversal_of=None):
    """lines: iterable of (account_code, debit_cents, credit_cents, memo). Must balance.
    Call inside a transaction."""
    clean = [(c, int(d or 0), int(cr or 0), m) for c, d, cr, m in lines if int(d or 0) or int(cr or 0)]
    if not clean:
        raise AccountingError("A journal entry needs at least one non-zero line")
    for code, d, cr, _ in clean:
        if d < 0 or cr < 0 or (d and cr):
            raise AccountingError(f"Line on {code} must be a positive debit or credit, not both")
    dr_total = sum(l[1] for l in clean)
    cr_total = sum(l[2] for l in clean)
    if dr_total != cr_total:
        raise AccountingError(f"Entry does not balance: debits {dr_total} != credits {cr_total}")
    ts = now()
    cur = db.execute(
        "INSERT INTO journal_entries (date, description, ref_type, ref_id, reversal_of, created_by, created_at)"
        " VALUES (?,?,?,?,?,?,?)", (date or ts[:10], description, ref_type, ref_id, reversal_of, user_id, ts))
    entry_id = cur.lastrowid
    for code, d, cr, memo in clean:
        db.execute("INSERT INTO journal_lines (entry_id, account_id, debit, credit, memo) VALUES (?,?,?,?,?)",
                   (entry_id, account_id(db, code), d, cr, memo))
    return entry_id


def post_sale(db, sale_id, user_id=None):
    sale = db.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
    pays = db.execute("SELECT method, SUM(amount) AS amt FROM payments WHERE sale_id = ? AND status = 'completed'"
                      " GROUP BY method", (sale_id,)).fetchall()
    lines = [(PAYMENT_ACCOUNTS[p["method"]], p["amt"], 0, f"Received ({p['method']})") for p in pays]
    lines += [
        ("4000", 0, sale["net"], "Sales (net of VAT and discount)"),
        ("2100", 0, sale["tax"], "Output VAT"),
        ("4100", 0, sale["service_charge"], "Service charge"),
    ]
    cogs = db.execute("SELECT COALESCE(SUM(ROUND(i.qty * i.unit_cost)), 0) AS c FROM sale_items i "
                      "JOIN products p ON p.id = i.product_id WHERE i.sale_id = ? AND p.track_stock = 1",
                      (sale_id,)).fetchone()["c"]
    if cogs:
        lines += [("5000", int(cogs), 0, "Cost of goods sold"), ("1200", 0, int(cogs), "Inventory out")]
    return post_entry(db, f"Sale {sale['receipt_no']}", lines, "sale", sale_id, user_id,
                      date=(sale["completed_at"] or now())[:10])


def reverse_ref(db, ref_type, ref_id, user_id, reason):
    entries = db.execute("SELECT * FROM journal_entries WHERE ref_type = ? AND ref_id = ? AND reversal_of IS NULL"
                         " AND id NOT IN (SELECT reversal_of FROM journal_entries WHERE reversal_of IS NOT NULL)",
                         (ref_type, ref_id)).fetchall()
    for e in entries:
        lines = db.execute("SELECT a.code, l.debit, l.credit, l.memo FROM journal_lines l "
                           "JOIN accounts a ON a.id = l.account_id WHERE l.entry_id = ?", (e["id"],)).fetchall()
        post_entry(db, f"REVERSAL: {e['description']} — {reason}",
                   [(l["code"], l["credit"], l["debit"], l["memo"]) for l in lines],
                   ref_type, ref_id, user_id, reversal_of=e["id"])


# ---------- statements ----------

def account_balances(db, start=None, end=None):
    """Per-account debit/credit totals for entries dated within [start, end] (inclusive, YYYY-MM-DD)."""
    where, args = [], []
    if start:
        where.append("e.date >= ?"); args.append(start)
    if end:
        where.append("e.date <= ?"); args.append(end)
    cond = ("AND " + " AND ".join(where)) if where else ""
    rows = db.execute(f"""
        SELECT a.id, a.code, a.name, a.type,
               COALESCE(SUM(l.debit), 0) AS debit, COALESCE(SUM(l.credit), 0) AS credit
        FROM accounts a
        LEFT JOIN journal_lines l ON l.account_id = a.id
             AND l.entry_id IN (SELECT e.id FROM journal_entries e WHERE 1=1 {cond})
        GROUP BY a.id ORDER BY a.code""", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["balance"] = (r["debit"] - r["credit"]) if r["type"] in NORMAL_DEBIT else (r["credit"] - r["debit"])
        out.append(d)
    return out


def trial_balance(db, end=None):
    rows = account_balances(db, end=end)
    tb = []
    for r in rows:
        net = r["debit"] - r["credit"]
        if r["debit"] or r["credit"]:
            tb.append({**r, "dr": max(net, 0), "cr": max(-net, 0)})
    return tb, sum(r["dr"] for r in tb), sum(r["cr"] for r in tb)


def profit_and_loss(db, start, end):
    rows = account_balances(db, start, end)
    income = [r for r in rows if r["type"] == "income" and r["balance"]]
    cogs = [r for r in rows if r["type"] == "expense" and r["code"].startswith("5") and r["balance"]]
    expenses = [r for r in rows if r["type"] == "expense" and not r["code"].startswith("5") and r["balance"]]
    ti, tc, te = (sum(r["balance"] for r in x) for x in (income, cogs, expenses))
    return {"income": income, "cogs": cogs, "expenses": expenses, "total_income": ti, "total_cogs": tc,
            "gross_profit": ti - tc, "total_expenses": te, "net_profit": ti - tc - te}


def balance_sheet(db, end):
    rows = account_balances(db, end=end)
    assets = [r for r in rows if r["type"] == "asset" and r["balance"]]
    liabilities = [r for r in rows if r["type"] == "liability" and r["balance"]]
    equity = [r for r in rows if r["type"] == "equity" and r["balance"]]
    earnings = sum(r["balance"] for r in rows if r["type"] == "income") - \
        sum(r["balance"] for r in rows if r["type"] == "expense")
    ta = sum(r["balance"] for r in assets)
    tl = sum(r["balance"] for r in liabilities)
    teq = sum(r["balance"] for r in equity) + earnings
    return {"assets": assets, "liabilities": liabilities, "equity": equity, "earnings": earnings,
            "total_assets": ta, "total_liabilities": tl, "total_equity": teq, "balanced": ta == tl + teq}


def ledger(db, account_code, start=None, end=None):
    acc = db.execute("SELECT * FROM accounts WHERE code = ?", (account_code,)).fetchone()
    where, args = ["l.account_id = ?"], [acc["id"]]
    if start:
        where.append("e.date >= ?"); args.append(start)
    if end:
        where.append("e.date <= ?"); args.append(end)
    rows = db.execute(f"""SELECT e.id, e.date, e.description, l.debit, l.credit, l.memo
        FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
        WHERE {' AND '.join(where)} ORDER BY e.date, e.id""", args).fetchall()
    opening = 0
    if start:
        ob = db.execute("""SELECT COALESCE(SUM(l.debit),0) d, COALESCE(SUM(l.credit),0) c FROM journal_lines l
            JOIN journal_entries e ON e.id = l.entry_id WHERE l.account_id = ? AND e.date < ?""",
                        (acc["id"], start)).fetchone()
        opening = (ob["d"] - ob["c"]) if acc["type"] in NORMAL_DEBIT else (ob["c"] - ob["d"])
    bal, out = opening, []
    for r in rows:
        bal += (r["debit"] - r["credit"]) if acc["type"] in NORMAL_DEBIT else (r["credit"] - r["debit"])
        out.append({**dict(r), "balance": bal})
    return acc, opening, out
