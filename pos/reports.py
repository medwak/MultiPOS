"""Sales and stock reports. Dates are YYYY-MM-DD, inclusive."""

DONE = "s.status = 'completed' AND date(s.completed_at) BETWEEN ? AND ?"


def summary(db, start, end):
    r = db.execute(f"""SELECT COUNT(*) n, COALESCE(SUM(gross),0) gross, COALESCE(SUM(discount),0) discount,
        COALESCE(SUM(net),0) net, COALESCE(SUM(tax),0) tax, COALESCE(SUM(service_charge),0) service,
        COALESCE(SUM(total),0) total FROM sales s WHERE {DONE}""", (start, end)).fetchone()
    d = dict(r)
    d["avg"] = d["total"] // d["n"] if d["n"] else 0
    d["voids"] = db.execute("SELECT COUNT(*) n, COALESCE(SUM(total),0) t FROM sales WHERE status='voided'"
                            " AND date(voided_at) BETWEEN ? AND ?", (start, end)).fetchone()
    d["cogs"] = db.execute(f"""SELECT COALESCE(SUM(ROUND(i.qty*i.unit_cost)),0) c FROM sale_items i
        JOIN sales s ON s.id = i.sale_id JOIN products p ON p.id = i.product_id
        WHERE {DONE} AND p.track_stock = 1""", (start, end)).fetchone()["c"]
    d["gross_profit"] = d["net"] + d["service"] - d["cogs"]
    return d


def by_day(db, start, end):
    return db.execute(f"""SELECT date(completed_at) day, COUNT(*) n, SUM(total) total, SUM(tax) tax
        FROM sales s WHERE {DONE} GROUP BY day ORDER BY day""", (start, end)).fetchall()


def by_hour(db, start, end):
    return db.execute(f"""SELECT CAST(strftime('%H', completed_at) AS INTEGER) hour, COUNT(*) n, SUM(total) total
        FROM sales s WHERE {DONE} GROUP BY hour ORDER BY hour""", (start, end)).fetchall()


def by_product(db, start, end, limit=None):
    q = f"""SELECT i.name, SUM(i.qty) qty, SUM(i.line_gross - i.line_discount) sales,
        SUM(ROUND(i.qty * i.unit_cost)) cost FROM sale_items i JOIN sales s ON s.id = i.sale_id
        WHERE {DONE} GROUP BY i.product_id, i.name ORDER BY sales DESC"""
    return db.execute(q + (f" LIMIT {int(limit)}" if limit else ""), (start, end)).fetchall()


def by_category(db, start, end):
    return db.execute(f"""SELECT COALESCE(c.name, 'Uncategorised') name, SUM(i.qty) qty,
        SUM(i.line_gross - i.line_discount) sales FROM sale_items i JOIN sales s ON s.id = i.sale_id
        LEFT JOIN products p ON p.id = i.product_id LEFT JOIN categories c ON c.id = p.category_id
        WHERE {DONE} GROUP BY c.id ORDER BY sales DESC""", (start, end)).fetchall()


def by_payment(db, start, end):
    return db.execute(f"""SELECT CASE p.method WHEN 'cash' THEN 'Cash' WHEN 'mpesa' THEN 'M-Pesa (STK push)'
        ELSE 'M-Pesa (code entered)' END method, COUNT(*) n, SUM(p.amount) amount
        FROM payments p JOIN sales s ON s.id = p.sale_id
        WHERE p.status = 'completed' AND {DONE} GROUP BY p.method ORDER BY amount DESC""", (start, end)).fetchall()


def by_cashier(db, start, end):
    return db.execute(f"""SELECT u.full_name name, COUNT(*) n, SUM(s.total) total, SUM(s.discount) discount
        FROM sales s JOIN users u ON u.id = s.cashier_id WHERE {DONE} GROUP BY u.id ORDER BY total DESC""",
                      (start, end)).fetchall()


def by_attendant(db, start, end):
    return db.execute(f"""SELECT a.name, a.commission_rate rate, COUNT(*) services,
        SUM(i.line_gross - i.line_discount) sales,
        ROUND(SUM(i.line_gross - i.line_discount) * a.commission_rate / 100) commission
        FROM sale_items i JOIN sales s ON s.id = i.sale_id JOIN attendants a ON a.id = i.attendant_id
        WHERE {DONE} GROUP BY a.id ORDER BY sales DESC""", (start, end)).fetchall()


def by_table(db, start, end):
    return db.execute(f"""SELECT t.name, COUNT(*) n, SUM(s.total) total FROM sales s
        JOIN dining_tables t ON t.id = s.table_id WHERE {DONE} GROUP BY t.id ORDER BY total DESC""",
                      (start, end)).fetchall()


def low_stock(db):
    return db.execute("""SELECT name, stock_qty, reorder_level, unit FROM products
        WHERE active = 1 AND track_stock = 1 AND is_service = 0 AND stock_qty <= reorder_level
        ORDER BY stock_qty - reorder_level""").fetchall()


def expiring(db, within_days=60):
    return db.execute("""SELECT name, batch_no, expiry_date, stock_qty, unit,
        CAST(julianday(expiry_date) - julianday('now', 'localtime') AS INTEGER) days_left
        FROM products WHERE active = 1 AND expiry_date IS NOT NULL AND expiry_date != ''
        AND julianday(expiry_date) - julianday('now', 'localtime') <= ? ORDER BY expiry_date""",
                      (within_days,)).fetchall()


def stock_valuation(db):
    return db.execute("""SELECT COALESCE(SUM(ROUND(stock_qty * cost)),0) cost_value,
        COALESCE(SUM(ROUND(stock_qty * price)),0) retail_value, COUNT(*) n FROM products
        WHERE active = 1 AND track_stock = 1 AND is_service = 0 AND stock_qty > 0""").fetchone()
