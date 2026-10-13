from datetime import date

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import accounting as acc
from .. import sales as svc
from ..auth import login_required
from ..db import get_db, transaction
from ..money import to_cents

bp = Blueprint("accounting", __name__, url_prefix="/accounting")


def _dates():
    today = date.today()
    start = request.args.get("start") or today.replace(day=1).isoformat()
    end = request.args.get("end") or today.isoformat()
    return start, end


@bp.get("/")
@login_required("manager")
def overview():
    db = get_db()
    start, end = _dates()
    return render_template("accounting/overview.html", start=start, end=end, pl=acc.profit_and_loss(db, start, end),
                           bs=acc.balance_sheet(db, end))


@bp.get("/trial-balance")
@login_required("manager")
def trial_balance():
    end = request.args.get("end") or date.today().isoformat()
    rows, dr, cr = acc.trial_balance(get_db(), end)
    return render_template("accounting/trial_balance.html", rows=rows, dr=dr, cr=cr, end=end)


@bp.get("/journal")
@login_required("manager")
def journal():
    db = get_db()
    start, end = _dates()
    entries = db.execute("""SELECT e.*, u.full_name who FROM journal_entries e LEFT JOIN users u ON u.id = e.created_by
        WHERE e.date BETWEEN ? AND ? ORDER BY e.id DESC LIMIT 300""", (start, end)).fetchall()
    lines = {}
    if entries:
        ids = [e["id"] for e in entries]
        for l in db.execute(f"""SELECT l.*, a.code, a.name FROM journal_lines l JOIN accounts a ON a.id = l.account_id
            WHERE l.entry_id IN ({','.join('?' * len(ids))}) ORDER BY l.id""", ids):
            lines.setdefault(l["entry_id"], []).append(l)
    return render_template("accounting/journal.html", entries=entries, lines=lines, start=start, end=end)


@bp.get("/ledger/<code>")
@login_required("manager")
def ledger(code):
    db = get_db()
    if not db.execute("SELECT 1 FROM accounts WHERE code = ?", (code,)).fetchone():
        abort(404)
    start, end = _dates()
    account, opening, rows = acc.ledger(db, code, start, end)
    return render_template("accounting/ledger.html", a=account, opening=opening, rows=rows, start=start, end=end)


@bp.route("/accounts", methods=["GET", "POST"])
@login_required("manager")
def accounts():
    db = get_db()
    if request.method == "POST":
        f = request.form
        code, name, typ = f.get("code", "").strip(), f.get("name", "").strip(), f.get("type")
        if not code.isdigit() or not name or typ not in ("asset", "liability", "equity", "income", "expense"):
            flash("Enter a numeric code, a name and a type", "error")
        elif db.execute("SELECT 1 FROM accounts WHERE code = ?", (code,)).fetchone():
            flash("That code is already used", "error")
        else:
            db.execute("INSERT INTO accounts (code, name, type) VALUES (?,?,?)", (code, name, typ))
            flash("Account added", "ok")
        return redirect(url_for("accounting.accounts"))
    rows = acc.account_balances(db)
    return render_template("accounting/accounts.html", rows=rows)


@bp.route("/expense", methods=["GET", "POST"])
@login_required("manager")
def expense():
    db = get_db()
    if request.method == "POST":
        f = request.form
        try:
            svc.record_expense(db, f.get("account"), to_cents(f.get("amount")), f.get("paid_from"),
                               f.get("description", "").strip(), f.get("date") or date.today().isoformat(),
                               g.user["id"])
            flash("Expense recorded", "ok")
            return redirect(url_for("accounting.expense"))
        except (ValueError, svc.SaleError) as e:
            flash(str(e), "error")
    exp_accounts = db.execute("SELECT * FROM accounts WHERE type = 'expense' AND active = 1 AND code NOT IN"
                              " ('5000','5100') ORDER BY code").fetchall()
    recent = db.execute("""SELECT e.date, e.description, l.debit, a.name FROM journal_entries e
        JOIN journal_lines l ON l.entry_id = e.id AND l.debit > 0 JOIN accounts a ON a.id = l.account_id
        WHERE e.ref_type = 'expense' ORDER BY e.id DESC LIMIT 30""").fetchall()
    return render_template("accounting/expense.html", accounts=exp_accounts, recent=recent,
                           today=date.today().isoformat())


@bp.route("/manual", methods=["GET", "POST"])
@login_required("admin")
def manual():
    """General journal entry (opening balances, capital injected, drawings, loans...)."""
    db = get_db()
    accounts = db.execute("SELECT * FROM accounts WHERE active = 1 ORDER BY code").fetchall()
    if request.method == "POST":
        f = request.form
        try:
            lines = []
            for code, d, c, memo in zip(f.getlist("account"), f.getlist("debit"), f.getlist("credit"),
                                        f.getlist("memo")):
                if code and (d or c):
                    lines.append((code, to_cents(d), to_cents(c), memo))
            with transaction(db):
                acc.post_entry(db, f.get("description", "").strip() or "Journal entry", lines, "manual", None,
                               g.user["id"], date=f.get("date") or date.today().isoformat())
            flash("Journal entry posted", "ok")
            return redirect(url_for("accounting.journal"))
        except (ValueError, acc.AccountingError) as e:
            flash(str(e), "error")
    return render_template("accounting/manual.html", accounts=accounts, today=date.today().isoformat())
