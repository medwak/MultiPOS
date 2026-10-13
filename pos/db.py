"""SQLite storage. All money is stored as integer cents to avoid rounding errors."""
import sqlite3
from datetime import datetime

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','manager','cashier')),
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    sku                   TEXT,
    barcode               TEXT,
    category_id           INTEGER REFERENCES categories(id),
    price                 INTEGER NOT NULL DEFAULT 0,
    cost                  INTEGER NOT NULL DEFAULT 0,
    tax_rate              REAL NOT NULL DEFAULT 16,
    is_service            INTEGER NOT NULL DEFAULT 0,
    track_stock           INTEGER NOT NULL DEFAULT 1,
    stock_qty             REAL NOT NULL DEFAULT 0,
    reorder_level         REAL NOT NULL DEFAULT 0,
    unit                  TEXT NOT NULL DEFAULT 'pc',
    allow_decimal         INTEGER NOT NULL DEFAULT 0,
    expiry_date           TEXT,
    batch_no              TEXT,
    requires_prescription INTEGER NOT NULL DEFAULT 0,
    send_to_kitchen       INTEGER NOT NULL DEFAULT 1,
    active                INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_products_barcode ON products(barcode);

CREATE TABLE IF NOT EXISTS dining_tables (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    seats  INTEGER NOT NULL DEFAULT 4,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS attendants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    commission_rate REAL NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sales (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no       TEXT UNIQUE,
    status           TEXT NOT NULL CHECK (status IN ('open','pending_payment','completed','voided')),
    table_id         INTEGER REFERENCES dining_tables(id),
    order_label      TEXT,
    cashier_id       INTEGER NOT NULL REFERENCES users(id),
    customer_name    TEXT,
    customer_phone   TEXT,
    prescription_ref TEXT,
    gross            INTEGER NOT NULL DEFAULT 0,
    discount         INTEGER NOT NULL DEFAULT 0,
    net              INTEGER NOT NULL DEFAULT 0,
    tax              INTEGER NOT NULL DEFAULT 0,
    service_charge   INTEGER NOT NULL DEFAULT 0,
    total            INTEGER NOT NULL DEFAULT 0,
    paid             INTEGER NOT NULL DEFAULT 0,
    change_due       INTEGER NOT NULL DEFAULT 0,
    notes            TEXT,
    created_at       TEXT NOT NULL,
    completed_at     TEXT,
    voided_at        TEXT,
    voided_by        INTEGER REFERENCES users(id),
    void_reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_sales_completed ON sales(status, completed_at);

CREATE TABLE IF NOT EXISTS sale_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id       INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
    product_id    INTEGER REFERENCES products(id),
    name          TEXT NOT NULL,
    qty           REAL NOT NULL,
    unit          TEXT,
    unit_price    INTEGER NOT NULL,
    unit_cost     INTEGER NOT NULL DEFAULT 0,
    tax_rate      REAL NOT NULL DEFAULT 0,
    line_gross    INTEGER NOT NULL DEFAULT 0,
    line_discount INTEGER NOT NULL DEFAULT 0,
    line_net      INTEGER NOT NULL DEFAULT 0,
    line_tax      INTEGER NOT NULL DEFAULT 0,
    attendant_id  INTEGER REFERENCES attendants(id),
    note          TEXT,
    kitchen_sent  REAL NOT NULL DEFAULT 0   -- quantity already sent to the kitchen
);

CREATE TABLE IF NOT EXISTS payments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id             INTEGER NOT NULL REFERENCES sales(id),
    method              TEXT NOT NULL CHECK (method IN ('cash','mpesa','mpesa_manual')),
    amount              INTEGER NOT NULL,
    tendered            INTEGER NOT NULL DEFAULT 0,
    change_given        INTEGER NOT NULL DEFAULT 0,
    reference           TEXT,
    phone               TEXT,
    status              TEXT NOT NULL CHECK (status IN ('pending','completed','failed','cancelled')),
    checkout_request_id TEXT UNIQUE,
    merchant_request_id TEXT,
    result_desc         TEXT,
    raw                 TEXT,
    last_query_at       TEXT,
    created_by          INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code   TEXT UNIQUE NOT NULL,
    name   TEXT NOT NULL,
    type   TEXT NOT NULL CHECK (type IN ('asset','liability','equity','income','expense')),
    system INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,
    description TEXT NOT NULL,
    ref_type    TEXT,
    ref_id      INTEGER,
    reversal_of INTEGER REFERENCES journal_entries(id),
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_je_ref ON journal_entries(ref_type, ref_id);

CREATE TABLE IF NOT EXISTS journal_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   INTEGER NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    debit      INTEGER NOT NULL DEFAULT 0,
    credit     INTEGER NOT NULL DEFAULT 0,
    memo       TEXT
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty_change REAL NOT NULL,
    reason     TEXT NOT NULL,
    ref_id     INTEGER,
    unit_cost  INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path):
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)  # autocommit; we use explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


class transaction:
    """`with transaction(db):` — BEGIN IMMEDIATE ... COMMIT / ROLLBACK."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_):
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def init_schema(db):
    db.executescript(SCHEMA)


def audit(db, user_id, action, detail=""):
    db.execute("INSERT INTO audit_log (user_id, action, detail, created_at) VALUES (?,?,?,?)",
               (user_id, action, detail, now()))
