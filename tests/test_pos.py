"""Run with:  python -m pytest -q"""
import json
import re
import time

import pytest

from pos import create_app
from pos import accounting as acc
from pos import sales as svc
from pos.db import connect
from pos.money import compute_totals, fmt, to_cents
from pos.mpesa import MpesaError, normalize_phone, parse_callback
from pos.printing import receipt_data, receipt_escpos, receipt_text_lines
from pos.settings import get_all


# ---------- pure functions ----------

def test_to_cents_and_fmt():
    assert to_cents("1,250.50") == 125050
    assert to_cents(0.1) == 10
    assert fmt(123456789) == "1,234,567.89"
    with pytest.raises(ValueError):
        to_cents("-5")


def test_totals_vat_inclusive_and_discount_allocation():
    lines = [{"qty": 1, "unit_price": 11600, "tax_rate": 16}, {"qty": 3, "unit_price": 3333, "tax_rate": 0}]
    res, t = compute_totals(lines, 1001, True, 10)
    assert t["gross"] == 11600 + 9999
    assert sum(r["line_discount"] for r in res) == 1001
    assert t["net"] + t["tax"] == t["gross"] - t["discount"]
    assert t["total"] == t["net"] + t["tax"] + t["service_charge"]
    assert res[1]["line_tax"] == 0


def test_totals_vat_exclusive():
    _, t = compute_totals([{"qty": 2, "unit_price": 10000, "tax_rate": 16}], 0, False, 0)
    assert (t["net"], t["tax"], t["total"]) == (20000, 3200, 23200)


def test_phone_normalisation():
    assert normalize_phone("0712 345 678") == "254712345678"
    assert normalize_phone("+254110000000") == "254110000000"
    assert normalize_phone("712345678") == "254712345678"
    with pytest.raises(MpesaError):
        normalize_phone("12345")


def test_parse_callback():
    body = {"Body": {"stkCallback": {"MerchantRequestID": "1", "CheckoutRequestID": "ws_X", "ResultCode": 0,
                                     "ResultDesc": "ok", "CallbackMetadata": {"Item": [
                                         {"Name": "Amount", "Value": 100}, {"Name": "MpesaReceiptNumber", "Value": "QAB1CD2EF3"},
                                         {"Name": "PhoneNumber", "Value": 254712345678}]}}}}
    cb = parse_callback(body)
    assert cb["receipt"] == "QAB1CD2EF3" and cb["amount"] == 100 and cb["phone"] == "254712345678"


# ---------- app fixtures ----------

@pytest.fixture
def app(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "t.db"), "TESTING": True})
    return app


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html) or re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1)


@pytest.fixture
def client(app):
    c = app.test_client()
    tok = csrf(c, "/setup")
    r = c.post("/setup", data={"csrf_token": tok, "business_name": "Test Hotel", "industry": "restaurant",
                               "samples": "1", "full_name": "Isaac Admin", "username": "admin", "password": "1234"})
    assert r.status_code == 302
    tok = csrf(c, "/login")
    r = c.post("/login", data={"csrf_token": tok, "username": "admin", "password": "1234"})
    assert r.status_code == 302
    c.token = csrf(c, "/pos")
    return c


def jpost(c, url, body=None, method="post"):
    return getattr(c, method)(url, data=json.dumps(body or {}), content_type="application/json",
                              headers={"X-CSRF-Token": c.token})


def db_of(app):
    return connect(app.config["DATABASE"])


def assert_books_balance(app):
    db = db_of(app)
    _, dr, cr = acc.trial_balance(db)
    assert dr == cr
    assert acc.balance_sheet(db, "2999-12-31")["balanced"]


# ---------- flows ----------

def test_cash_sale_posts_stock_and_ledger(app, client):
    cat = client.get("/api/catalog").get_json()
    prods = {p["name"]: p for p in cat["products"]}
    stew, tea = prods["Beef Stew"], prods["Kenyan Tea"]
    table = cat["tables"][0]["id"]
    o = jpost(client, "/api/orders", {"items": [{"product_id": stew["id"], "qty": 2, "note": "extra hot"},
                                                {"product_id": tea["id"], "qty": 1}], "table_id": table}).get_json()
    assert o["status"] == "open" and o["table_id"] == table
    # service charge 10% on restaurant
    assert o["service_charge"] == round((2 * 35000 + 8000) * 0.10)
    assert o["total"] == 78000 + o["service_charge"]

    # kitchen
    r = jpost(client, f"/api/orders/{o['id']}/kitchen").get_json()
    assert r["printed"] == 2
    assert client.get(f"/kitchen/{o['id']}").status_code == 200
    assert svc.kitchen_pending(db_of(app), o["id"]) == []

    # cashier pays with more than due
    r = jpost(client, f"/api/orders/{o['id']}/pay/cash", {"tendered": "1000"}).get_json()
    assert r["status"] == "completed"
    assert r["change_due"] == 100000 - o["total"]

    db = db_of(app)
    stock = db.execute("SELECT stock_qty FROM products WHERE id = ?", (stew["id"],)).fetchone()[0]
    assert stock == stew["stock_qty"] - 2
    assert_books_balance(app)
    cash = [a for a in acc.account_balances(db) if a["code"] == "1000"][0]["balance"]
    assert cash == o["total"]
    inventory = [a for a in acc.account_balances(db) if a["code"] == "1200"][0]["balance"]
    assert inventory > 0  # opening sample stock was capitalised, sale took cost out

    # receipt renders in both formats
    html = client.get(f"/receipt/{o['id']}").get_data(as_text=True)
    assert "SALES RECEIPT" in html and "Beef Stew" in html
    data = receipt_data(db, o["id"], get_all(db))
    assert receipt_escpos(data).endswith(b"\x1dVB\x00")
    assert any("TOTAL" in t for _, t in receipt_text_lines(data))


def test_simulated_mpesa_sale(app, client, monkeypatch):
    cat = client.get("/api/catalog").get_json()
    p = cat["products"][0]
    o = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}]}).get_json()
    r = jpost(client, f"/api/orders/{o['id']}/pay/mpesa", {"phone": "0712345678"}).get_json()
    assert r["status"] == "pending_payment"
    # fast-forward: make the pending payment look 5 seconds old
    db = db_of(app)
    db.execute("UPDATE payments SET created_at = datetime('now','localtime','-5 seconds') WHERE sale_id = ?", (o["id"],))
    r = client.get(f"/api/orders/{o['id']}/status").get_json()
    assert r["status"] == "completed"
    assert r["payments"][0]["reference"].startswith("SIM")
    assert_books_balance(app)
    mp = [a for a in acc.account_balances(db) if a["code"] == "1010"][0]["balance"]
    assert mp >= o["total"]


def test_real_callback_flow_and_split_payment(app, client):
    """Simulate a sandbox STK push then Safaricom's callback hitting our endpoint."""
    db = db_of(app)
    cat = client.get("/api/catalog").get_json()
    p = cat["products"][0]
    o = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 3}]}).get_json()
    # part paid in cash first
    jpost(client, f"/api/orders/{o['id']}/pay/cash", {"tendered": "20"})

    class FakeClient:
        simulated = False
        def stk_push(self, phone, amount, ref, desc):
            return {"CheckoutRequestID": "ws_CO_TEST1", "MerchantRequestID": "m1", "phone": normalize_phone(phone)}
    svc.start_mpesa(db, o["id"], "0712345678", 0, 1, FakeClient())
    token = get_all(db)["mpesa_callback_token"]
    due_kes = (o["total"] - 2000) / 100
    body = {"Body": {"stkCallback": {"MerchantRequestID": "m1", "CheckoutRequestID": "ws_CO_TEST1", "ResultCode": 0,
                                     "ResultDesc": "The service request is processed successfully.",
                                     "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": due_kes},
                                                                   {"Name": "MpesaReceiptNumber", "Value": "TJ9ABCDE12"},
                                                                   {"Name": "PhoneNumber", "Value": 254712345678}]}}}}
    assert client.post("/mpesa/callback/wrong", json=body).status_code == 403
    r = client.post(f"/mpesa/callback/{token}", json=body)
    assert r.status_code == 200
    sale = db.execute("SELECT * FROM sales WHERE id = ?", (o["id"],)).fetchone()
    assert sale["status"] == "completed" and sale["paid"] == sale["total"]
    # idempotent: a duplicate callback changes nothing
    client.post(f"/mpesa/callback/{token}", json=body)
    assert db.execute("SELECT COUNT(*) FROM journal_entries WHERE ref_type='sale' AND ref_id=?", (o["id"],)).fetchone()[0] == 1
    assert_books_balance(app)


def test_failed_mpesa_reopens_order(app, client):
    db = db_of(app)
    p = client.get("/api/catalog").get_json()["products"][0]
    o = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}]}).get_json()

    class FakeClient:
        def stk_push(self, *a):
            return {"CheckoutRequestID": "ws_CO_FAIL", "phone": "254712345678"}
    svc.start_mpesa(db, o["id"], "0712345678", 0, 1, FakeClient())
    token = get_all(db)["mpesa_callback_token"]
    client.post(f"/mpesa/callback/{token}", json={"Body": {"stkCallback": {
        "CheckoutRequestID": "ws_CO_FAIL", "ResultCode": 1032, "ResultDesc": "Request cancelled by user"}}})
    st = client.get(f"/api/orders/{o['id']}").get_json()
    assert st["status"] == "open"
    assert st["payments"][0]["status"] == "cancelled"


def test_manual_mpesa_code_and_duplicate(app, client):
    p = client.get("/api/catalog").get_json()["products"][0]
    o1 = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}]}).get_json()
    r = jpost(client, f"/api/orders/{o1['id']}/pay/mpesa-code", {"code": "tj7k2abc9d", "amount": o1["total"] / 100})
    assert r.get_json()["status"] == "completed"
    o2 = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}]}).get_json()
    r = jpost(client, f"/api/orders/{o2['id']}/pay/mpesa-code", {"code": "TJ7K2ABC9D", "amount": o2["total"] / 100})
    assert r.status_code == 400 and "already been used" in r.get_json()["error"]


def test_void_reverses_everything(app, client):
    db = db_of(app)
    p = client.get("/api/catalog").get_json()["products"][1]
    before = db.execute("SELECT stock_qty FROM products WHERE id = ?", (p["id"],)).fetchone()[0]
    o = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 2}]}).get_json()
    jpost(client, f"/api/orders/{o['id']}/pay/cash", {"tendered": str(o["total"] / 100)})
    r = client.post(f"/sales/{o['id']}/void", data={"csrf_token": client.token, "reason": "Wrong order"})
    assert r.status_code == 302
    after = db.execute("SELECT stock_qty FROM products WHERE id = ?", (p["id"],)).fetchone()[0]
    assert after == before
    pl = acc.profit_and_loss(db, "2000-01-01", "2999-12-31")
    assert pl["total_income"] == 0 and pl["total_cogs"] == 0
    assert_books_balance(app)


def test_stock_guard_and_expense_and_purchase(app, client):
    db = db_of(app)
    p = client.get("/api/catalog").get_json()["products"][0]
    r = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 9999}]})
    assert r.status_code == 400 and "Not enough stock" in r.get_json()["error"]
    r = client.post("/admin/stock", data={"csrf_token": client.token, "action": "receive", "product_id": p["id"],
                                          "qty": "10", "unit_cost": "50", "paid_from": "mpesa", "note": "Inv 1"})
    assert r.status_code == 302
    r = client.post("/accounting/expense", data={"csrf_token": client.token, "account": "6200", "amount": "1500",
                                                 "paid_from": "cash", "description": "KPLC tokens", "date": "2026-10-09"})
    assert r.status_code == 302
    pl = acc.profit_and_loss(db, "2026-10-01", "2026-10-31")
    assert pl["total_expenses"] == 150000
    assert_books_balance(app)


def test_cashier_limits(app, client):
    client.post("/admin/users", data={"csrf_token": client.token, "action": "create", "full_name": "Cash Ier",
                                      "username": "cashier", "password": "1111", "role": "cashier"})
    c2 = app.test_client()
    tok = csrf(c2, "/login")
    c2.post("/login", data={"csrf_token": tok, "username": "cashier", "password": "1111"})
    c2.token = csrf(c2, "/pos")
    assert c2.get("/admin/settings").status_code == 403
    assert c2.get("/reports/").status_code == 403
    p = c2.get("/api/catalog").get_json()["products"][1]
    r = jpost(c2, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}], "discount": p["price"] / 100 * 0.5})
    assert r.status_code == 400 and "10%" in r.get_json()["error"]


def test_csrf_required(client):
    r = client.post("/api/orders", json={"items": []})
    assert r.status_code == 400


def test_industry_switch_and_pharmacy_rules(app, client):
    r = client.post("/admin/settings", data={"csrf_token": client.token, "action": "industry", "industry": "pharmacy",
                                             "seed_categories": "1", "seed_samples": "1", "tab": "industry"})
    assert r.status_code == 302
    cat = client.get("/api/catalog").get_json()
    amox = [p for p in cat["products"] if p["name"].startswith("Amoxicillin")][0]
    r = jpost(client, "/api/orders", {"items": [{"product_id": amox["id"], "qty": 1}]})
    assert r.status_code == 400 and "prescription" in r.get_json()["error"].lower()
    r = jpost(client, "/api/orders", {"items": [{"product_id": amox["id"], "qty": 1}], "prescription_ref": "RX-001"})
    assert r.status_code == 200
    db = db_of(app)
    assert get_all(db)["feature.tables"] == "0" and get_all(db)["feature.expiry_tracking"] == "1"


def test_salon_attendants_and_commission(app, client):
    client.post("/admin/settings", data={"csrf_token": client.token, "action": "industry", "industry": "salon",
                                         "seed_categories": "1", "seed_samples": "1", "tab": "industry"})
    cat = client.get("/api/catalog").get_json()
    cut = [p for p in cat["products"] if p["name"] == "Haircut"][0]
    r = jpost(client, "/api/orders", {"items": [{"product_id": cut["id"], "qty": 1}]})
    assert r.status_code == 400 and "attendant" in r.get_json()["error"]
    att = cat["attendants"][0]["id"]
    o = jpost(client, "/api/orders", {"items": [{"product_id": cut["id"], "qty": 1, "attendant_id": att}]}).get_json()
    jpost(client, f"/api/orders/{o['id']}/pay/cash", {"tendered": "300"})
    html = client.get("/reports/?preset=today").get_data(as_text=True)
    assert "Attendant commissions" in html and "90.00" in html  # 30% of 300


def test_every_page_renders(app, client):
    p = client.get("/api/catalog").get_json()["products"][0]
    o = jpost(client, "/api/orders", {"items": [{"product_id": p["id"], "qty": 1}]}).get_json()
    jpost(client, f"/api/orders/{o['id']}/pay/cash", {"tendered": "1000"})
    pages = ["/pos", "/sales", f"/sales/{o['id']}", f"/receipt/{o['id']}", "/reports/", "/reports/?preset=30d",
             "/reports/z-report", "/reports/export/sales.csv", "/reports/export/products.csv",
             "/reports/export/items.csv", "/accounting/", "/accounting/trial-balance", "/accounting/journal",
             "/accounting/ledger/1000", "/accounting/accounts", "/accounting/expense", "/accounting/manual",
             "/admin/products", "/admin/products/new", f"/admin/products/{p['id']}", "/admin/list/categories",
             "/admin/list/tables", "/admin/list/attendants", "/admin/stock", "/admin/users", "/admin/audit"]
    pages += [f"/admin/settings?tab={t}" for t in ("business", "industry", "tax", "printing", "mpesa")]
    for url in pages:
        r = client.get(url)
        assert r.status_code == 200, (url, r.status_code)


def test_manual_journal_must_balance(app, client):
    r = client.post("/accounting/manual", data={"csrf_token": client.token, "description": "Capital",
                                                "date": "2026-10-01", "account": ["1010", "3000"],
                                                "debit": ["50000", ""], "credit": ["", "40000"], "memo": ["", ""]})
    assert "does not balance" in r.get_data(as_text=True)
    r = client.post("/accounting/manual", data={"csrf_token": client.token, "description": "Capital",
                                                "date": "2026-10-01", "account": ["1010", "3000"],
                                                "debit": ["50000", ""], "credit": ["", "50000"], "memo": ["", ""]})
    assert r.status_code == 302
    assert_books_balance(app)
