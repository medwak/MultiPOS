"""Business settings stored in the `settings` table, with defaults."""
import os

from .db import now
from .industries import FEATURES, INDUSTRIES, MEASURED, SERVICE_CATEGORIES

DEFAULTS = {
    "setup_done": "0",
    "industry": "general",
    "business_name": "My Business",
    "business_address": "",
    "business_phone": "",
    "business_email": "",
    "kra_pin": "",
    "currency": "KES",
    "default_tax_rate": "16",
    "prices_include_tax": "1",
    "service_charge_rate": "0",
    "receipt_prefix": "R",
    "receipt_footer": "Thank you for your business!",
    "receipt_width": "80",            # mm: 58 or 80
    "printer_target": "browser",      # browser | network:IP:PORT | device:/dev/usb/lp0 | file:/path
    "kitchen_printer_target": "browser",
    "auto_print": "1",
    "allow_negative_stock": "0",
    # M-Pesa (Daraja) — env vars with the same name in upper case take priority
    "mpesa_mode": "simulate",         # simulate | sandbox | production
    "mpesa_consumer_key": "",
    "mpesa_consumer_secret": "",
    "mpesa_shortcode": "174379",
    "mpesa_passkey": "",
    "mpesa_transaction_type": "CustomerPayBillOnline",  # or CustomerBuyGoodsOnline for a Till
    "mpesa_party_b": "",              # Till number when using Buy Goods; blank = shortcode
    "mpesa_callback_base": "",        # public https base URL, e.g. https://abc.ngrok-free.app
    "mpesa_callback_token": "",       # random secret appended to the callback path
}

SECRET_KEYS = {"mpesa_consumer_secret", "mpesa_passkey", "mpesa_callback_token"}

DEFAULT_ACCOUNTS = [
    ("1000", "Cash on Hand", "asset"),
    ("1010", "M-Pesa", "asset"),
    ("1020", "Bank", "asset"),
    ("1200", "Inventory", "asset"),
    ("2000", "Accounts Payable", "liability"),
    ("2100", "VAT Payable", "liability"),
    ("3000", "Owner's Capital", "equity"),
    ("3100", "Owner's Drawings", "equity"),
    ("4000", "Sales Revenue", "income"),
    ("4100", "Service Charge Income", "income"),
    ("4900", "Other Income", "income"),
    ("5000", "Cost of Goods Sold", "expense"),
    ("5100", "Stock Adjustments / Shrinkage", "expense"),
    ("6000", "Rent", "expense"),
    ("6100", "Salaries & Wages", "expense"),
    ("6150", "Commissions", "expense"),
    ("6200", "Utilities (Power, Water, Internet)", "expense"),
    ("6300", "Transport", "expense"),
    ("6400", "M-Pesa & Bank Charges", "expense"),
    ("6500", "Repairs & Maintenance", "expense"),
    ("6900", "Other Expenses", "expense"),
]


def get_all(db):
    data = dict(DEFAULTS)
    for row in db.execute("SELECT key, value FROM settings"):
        data[row["key"]] = row["value"]
    for key in DEFAULTS:
        if key.startswith("mpesa_") and os.environ.get(key.upper()):
            data[key] = os.environ[key.upper()]
    return data


def set_many(db, values):
    for k, v in values.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, str(v)))


def features(settings):
    return {k: settings.get(f"feature.{k}", "0") == "1" for k in FEATURES}


def terms(settings):
    return INDUSTRIES.get(settings.get("industry"), INDUSTRIES["general"])["terms"]


def apply_industry(db, industry, seed_categories=True, seed_samples=False):
    profile = INDUSTRIES[industry]
    values = {"industry": industry, "service_charge_rate": profile["service_charge_rate"]}
    values.update({f"feature.{k}": "1" if v else "0" for k, v in profile["features"].items()})
    set_many(db, values)
    if seed_categories:
        existing = {r["name"].lower() for r in db.execute("SELECT name FROM categories")}
        for i, name in enumerate(profile["categories"]):
            if name.lower() not in existing:
                db.execute("INSERT INTO categories (name, sort_order) VALUES (?, ?)", (name, i))
    if seed_samples:
        tax = float(get_all(db)["default_tax_rate"])
        for name, price, cat in profile["sample"]:
            if db.execute("SELECT 1 FROM products WHERE name = ?", (name,)).fetchone():
                continue
            cat_id = db.execute("SELECT id FROM categories WHERE name = ?", (cat,)).fetchone()
            is_service = cat in SERVICE_CATEGORIES
            measured = any(m in name for m in MEASURED)
            db.execute(
                "INSERT INTO products (name, category_id, price, cost, tax_rate, is_service, track_stock, stock_qty,"
                " reorder_level, unit, allow_decimal, requires_prescription, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, cat_id["id"] if cat_id else None, price, int(price * 0.6), tax, int(is_service),
                 0 if is_service else 1, 0 if is_service else 50, 0 if is_service else 10,
                 "kg" if "(kg)" in name else "m" if "(m)" in name else "pc", int(measured),
                 int(cat == "Prescription"), now()))
            if not is_service:
                # Opening stock: record the movement and capitalise it so the books stay right.
                from .accounting import post_entry
                pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                cost = int(price * 0.6)
                db.execute("INSERT INTO stock_movements (product_id, qty_change, reason, unit_cost, note, created_at)"
                           " VALUES (?, 50, 'purchase', ?, 'Opening stock (sample)', ?)", (pid, cost, now()))
                post_entry(db, f"Opening stock: 50 x {name}", [("1200", cost * 50, 0, name),
                                                               ("3000", 0, cost * 50, "Opening stock")], "stock", None)
        if profile["features"].get("tables") and not db.execute("SELECT 1 FROM dining_tables").fetchone():
            for n in range(1, 9):
                db.execute("INSERT INTO dining_tables (name, seats) VALUES (?, 4)", (f"Table {n}",))
        if profile["features"].get("attendants") and not db.execute("SELECT 1 FROM attendants").fetchone():
            for n, rate in (("Jane", 30), ("Otieno", 30)):
                db.execute("INSERT INTO attendants (name, commission_rate) VALUES (?, ?)", (n, rate))


def ensure_defaults(db):
    for code, name, typ in DEFAULT_ACCOUNTS:
        db.execute("INSERT OR IGNORE INTO accounts (code, name, type, system) VALUES (?,?,?,1)", (code, name, typ))
    if not db.execute("SELECT 1 FROM settings WHERE key LIKE 'feature.%'").fetchone():
        apply_industry(db, "general", seed_categories=True)
    if not db.execute("SELECT 1 FROM settings WHERE key = 'mpesa_callback_token'").fetchone():
        import secrets
        set_many(db, {"mpesa_callback_token": secrets.token_urlsafe(16)})
