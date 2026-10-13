# MultiPOS — multi-industry point of sale (Python / Flask)

A browser-based POS that runs on one computer and is used from any device on the same network: a till PC, a tablet or a phone. One admin setting turns it into a restaurant, bar, retail shop, supermarket, pharmacy, salon or hardware store POS.

**Built in:** M-Pesa STK push (Daraja), receipt printing (browser and ESC/POS thermal printers), reports, and full double-entry accounting.

---

## 1. Install and run

Requires **Python 3.10+**.

```bash
cd multipos
python -m venv venv
# Windows: venv\Scripts\activate      macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open **http://localhost:5000**. The first visit opens a setup screen with these steps:

1. Enter the business name.
2. Pick an industry.
3. Choose whether to load sample items.
4. Create the admin account.

To use other devices, open `http://<this-computer's-IP>:5000` on the tablet or phone. Data is stored in `instance/pos.db` (SQLite). **Back this file up daily.**

Run the tests with `pip install pytest` followed by `python -m pytest -q`.

## 2. Industry settings

Go to **Settings → Industry & features**. Choosing an industry applies a preset, and each feature can then be switched on or off individually.

| Feature | Restaurant | Bar | Retail | Supermarket | Pharmacy | Salon | Hardware |
|---|---|---|---|---|---|---|---|
| Tables / floor | ✓ | ✓ | | | | | |
| Open orders / tabs | ✓ | ✓ | ✓ | ✓ | | ✓ | ✓ |
| Kitchen tickets | ✓ | | | | | | |
| Item notes ("no onions") | ✓ | ✓ | | | | ✓ | |
| Service charge | ✓ (10%) | | | | | | |
| Barcode scanning | | ✓ | ✓ | ✓ | ✓ | | ✓ |
| Inventory | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Expiry & batch | | | | ✓ | ✓ | | |
| Prescription-only items | | | | | ✓ | | |
| Attendants & commissions | | | | | | ✓ | |
| Weighed items (1.25 kg) | | | | ✓ | | | ✓ |
| Customer name/phone | | | | | ✓ | ✓ | ✓ |

The wording also changes with the industry, for example "Menu" versus "Products" versus "Services". Presets live in `pos/industries.py`; to add a new industry, add a new entry there.

## 3. M-Pesa

Go to **Settings → M-Pesa**. There are three modes:

- **Simulate** (default): no real money. Payments confirm about 4 seconds after the request. Receipts are stamped *SIMULATED*. Use this mode for training.
- **Sandbox**: create an app at https://developer.safaricom.co.ke and enter its Consumer Key and Secret. Shortcode `174379` is the sandbox Paybill, and the passkey is on the portal's test credentials page.
- **Production**: use your Go-Live keys, Paybill or Till, and passkey. For a Till, choose *Till (Buy Goods)*, put the store number in Shortcode and the till number in "Till number".

**How a payment is confirmed.** The cashier types the customer's number and the customer enters their PIN on their phone. The till then confirms the payment in one of two ways:

- **Callback (best).** Set *Public callback base URL* to an https address that reaches this computer, for example with `ngrok http 5000`. Safaricom then posts the result to `/mpesa/callback/<secret-token>` and the M-Pesa receipt code is stored on the sale.
- **Polling (works offline-LAN).** With no public URL, the server asks Safaricom's STK Query API after about 12 seconds. This is slower, and Safaricom does not return the receipt code this way.

**Customer paid by SMS instead?** Cashiers can use the *M-Pesa code* tab to type the confirmation code. Duplicate codes are rejected.

Split payments are supported, for example part cash and part M-Pesa.

Keys can also be set as environment variables in a `.env` file instead of the database. See `.env.example`.

## 4. Printing

Go to **Settings → Printing**. The receipt printer and the kitchen printer are set separately.

- `browser` opens an 80 mm or 58 mm receipt in the print dialog. It works with any printer installed on the computer. To print without the dialog, start Chrome with `--kiosk-printing`.
- `network:192.168.1.50:9100` sends raw ESC/POS to a LAN or Wi-Fi thermal printer and opens the cash drawer on cash sales.
- `device:/dev/usb/lp0` sends to a USB thermal printer on Linux.

## 5. Accounting (double entry)

Every event posts a balanced journal entry automatically:

| Event | Debit | Credit |
|---|---|---|
| Sale | Cash / M-Pesa | Sales (net), VAT Payable, Service Charge |
| …and for stocked items | Cost of Goods Sold | Inventory |
| Stock purchase | Inventory | Cash / M-Pesa / Bank / Accounts Payable |
| Expense | Expense account (rent, power…) | Cash / M-Pesa / Bank |
| Stock count difference | Shrinkage (or Inventory) | Inventory (or Shrinkage) |
| Void | Exact reversal of the sale entry | |

The **Accounts** menu shows:

- Profit & Loss and Balance Sheet for any dates
- Trial Balance
- General Journal
- Ledger for each account
- Chart of Accounts, where you can add accounts
- Expense entry
- Manual journal entries, for opening balances, owner's capital, drawings and loans

All money is stored as integer cents, so totals never drift.

## 6. Reports

The **Reports** menu covers:

- Sales summary: totals, VAT, discounts, gross profit and voids
- Sales by day and by hour
- Breakdowns by item, category, payment method, cashier, table, and attendant commission
- Low stock, expiring stock and stock valuation
- A printable **Z-report** (end of day) showing expected cash
- CSV exports of sales, items and line items

## 7. Users and roles

- **Cashier:** the till and their own sales. Discounts are capped at 10%.
- **Manager:** everything a cashier can do, plus reports, accounts, products, stock and voids.
- **Admin:** everything, including settings, users and the audit log.

## 8. Going live checklist

- [ ] Set `SECRET_KEY` in `.env`. Otherwise a random one is created in `instance/`.
- [ ] Run on a dedicated PC on the shop's network. `run.py` uses Waitress if it is installed, which is in requirements.
- [ ] Set up daily backups of `instance/pos.db`.
- [ ] Switch M-Pesa from *Simulate* to *Production* and test with a small amount.
- [ ] Remove or edit the sample items.
- [ ] **KRA eTIMS:** VAT-registered businesses must transmit invoices through KRA eTIMS. This system prints standard receipts; eTIMS integration is **not** included yet.
- [ ] **Refunds:** voiding a sale reverses stock and accounts, but M-Pesa money must be reversed from your M-Pesa business portal.

## Project layout

```
run.py                  start the server
pos/__init__.py         app factory
pos/industries.py       industry presets & feature list
pos/settings.py         settings, chart of accounts, sample data
pos/money.py            cents, VAT, discounts, service charge
pos/sales.py            orders, payments, stock, voids, purchases, expenses
pos/accounting.py       double-entry ledger & statements
pos/mpesa.py            Daraja STK push, query, callback parsing
pos/printing.py         receipts (HTML + ESC/POS), kitchen tickets
pos/reports.py          report queries
pos/views/              web pages and JSON API
pos/templates/, static/ UI (till screen is static/pos.js)
tests/test_pos.py       automated tests
```
