"""Receipts and kitchen tickets.

Two ways to print:
  * browser  - an 80mm/58mm HTML receipt opened and printed by the browser (works with any thermal
               printer installed in Windows/macOS/Linux; set its paper size to 80mm/58mm roll).
  * ESC/POS  - raw bytes sent straight to the printer:
                 network:192.168.1.50:9100   (Ethernet/Wi-Fi thermal printers)
                 device:/dev/usb/lp0         (USB printer on Linux)
                 file:/path/to/out.bin       (for testing)
"""
import socket

from .money import fmt, fmt_qty

ESC, GS = b"\x1b", b"\x1d"
INIT = ESC + b"@"
ALIGN_L, ALIGN_C, ALIGN_R = ESC + b"a\x00", ESC + b"a\x01", ESC + b"a\x02"
BOLD_ON, BOLD_OFF = ESC + b"E\x01", ESC + b"E\x00"
BIG, NORMAL = GS + b"!\x11", GS + b"!\x00"
CUT = GS + b"V\x42\x00"
DRAWER = ESC + b"p\x00\x19\xfa"  # kick cash drawer on pin 2


def receipt_data(db, sale_id, settings):
    sale = db.execute("""SELECT s.*, u.full_name AS cashier, t.name AS table_name FROM sales s
        JOIN users u ON u.id = s.cashier_id LEFT JOIN dining_tables t ON t.id = s.table_id WHERE s.id = ?""",
                      (sale_id,)).fetchone()
    items = db.execute("""SELECT i.*, a.name AS attendant FROM sale_items i
        LEFT JOIN attendants a ON a.id = i.attendant_id WHERE i.sale_id = ? ORDER BY i.id""", (sale_id,)).fetchall()
    payments = db.execute("SELECT * FROM payments WHERE sale_id = ? AND status = 'completed' ORDER BY id",
                          (sale_id,)).fetchall()
    vat = db.execute("""SELECT tax_rate, SUM(line_net) AS net, SUM(line_tax) AS tax FROM sale_items
        WHERE sale_id = ? GROUP BY tax_rate ORDER BY tax_rate DESC""", (sale_id,)).fetchall()
    return {"s": settings, "sale": sale, "items": items, "payments": payments, "vat": vat}


def _cols(left, right, width):
    left = str(left)
    space = width - len(right) - 1
    if len(left) > space:
        left = left[:space]
    return f"{left}{' ' * (width - len(left) - len(right))}{right}"


PAY_LABEL = {"cash": "Cash", "mpesa": "M-Pesa", "mpesa_manual": "M-Pesa"}


def receipt_text_lines(data):
    """Plain-text receipt layout used for ESC/POS (and handy for tests)."""
    s, sale = data["s"], data["sale"]
    w = 32 if s.get("receipt_width") == "58" else 48
    c = s.get("currency", "KES")
    out = []
    out.append(("center_big", s["business_name"]))
    for extra in (s.get("business_address"), s.get("business_phone") and f"Tel: {s['business_phone']}",
                  s.get("kra_pin") and f"PIN: {s['kra_pin']}"):
        if extra:
            out.append(("center", extra))
    out.append(("line", "-" * w))
    title = "SALES RECEIPT" if sale["status"] == "completed" else \
        "*** VOIDED ***" if sale["status"] == "voided" else "BILL (NOT PAID)"
    out.append(("center_bold", title))
    out.append(("line", _cols(f"No: {sale['receipt_no']}", (sale["completed_at"] or sale["created_at"])[:16], w)))
    out.append(("line", f"Served by: {sale['cashier']}"))
    if sale["table_name"]:
        out.append(("line", f"Table: {sale['table_name']}"))
    if sale["customer_name"]:
        out.append(("line", f"Customer: {sale['customer_name']} {sale['customer_phone'] or ''}".strip()))
    out.append(("line", "-" * w))
    for it in data["items"]:
        out.append(("line", it["name"][:w]))
        qty = f"  {fmt_qty(it['qty'])} {it['unit'] if it['unit'] not in ('pc', None) else ''} x {fmt(it['unit_price'])}"
        out.append(("line", _cols(qty.replace("  x", " x"), fmt(it["line_gross"]), w)))
        if it["attendant"]:
            out.append(("line", f"  by {it['attendant']}"))
        if it["note"]:
            out.append(("line", f"  * {it['note']}"[:w]))
    out.append(("line", "-" * w))
    out.append(("line", _cols("Subtotal", fmt(sale["gross"]), w)))
    if sale["discount"]:
        out.append(("line", _cols("Discount", "-" + fmt(sale["discount"]), w)))
    if sale["service_charge"]:
        out.append(("line", _cols(f"Service charge ({s['service_charge_rate']}%)", fmt(sale["service_charge"]), w)))
    if s.get("prices_include_tax") != "1" and sale["tax"]:
        out.append(("line", _cols("VAT", fmt(sale["tax"]), w)))
    out.append(("bold", _cols(f"TOTAL {c}", fmt(sale["total"]), w)))
    for p in data["payments"]:
        label = PAY_LABEL[p["method"]] + (f" {p['reference']}" if p["reference"] else "")
        out.append(("line", _cols(label, fmt(p["tendered"] or p["amount"]), w)))
    if sale["change_due"]:
        out.append(("bold", _cols("CHANGE", fmt(sale["change_due"]), w)))
    if sale["status"] == "completed" and sale["paid"] < sale["total"]:
        out.append(("bold", _cols("BALANCE", fmt(sale["total"] - sale["paid"]), w)))
    out.append(("line", "-" * w))
    out.append(("line", _cols("VAT rate", "Net        VAT", w)))
    for v in data["vat"]:
        out.append(("line", _cols(f"{v['tax_rate']:g}%", f"{fmt(v['net'])}  {fmt(v['tax']):>9}", w)))
    if any(p["reference"] and p["reference"].startswith("SIM") for p in data["payments"]):
        out.append(("center_bold", "SIMULATED M-PESA - NOT REAL MONEY"))
    out.append(("line", ""))
    if s.get("receipt_footer"):
        out.append(("center", s["receipt_footer"]))
    return out


def receipt_escpos(data, open_drawer=False):
    buf = bytearray(INIT)
    if open_drawer:
        buf += DRAWER
    for kind, text in receipt_text_lines(data):
        enc = text.encode("cp437", errors="replace") + b"\n"
        if kind == "center_big":
            buf += ALIGN_C + BIG + BOLD_ON + enc + NORMAL + BOLD_OFF + ALIGN_L
        elif kind == "center":
            buf += ALIGN_C + enc + ALIGN_L
        elif kind == "center_bold":
            buf += ALIGN_C + BOLD_ON + enc + BOLD_OFF + ALIGN_L
        elif kind == "bold":
            buf += BOLD_ON + enc + BOLD_OFF
        else:
            buf += enc
    buf += b"\n\n\n" + CUT
    return bytes(buf)


def kitchen_escpos(sale, table_name, lines, cashier):
    buf = bytearray(INIT + ALIGN_C + BIG + b"KITCHEN\n" + NORMAL + ALIGN_L)
    head = f"{table_name or sale['order_label'] or 'Order'}  #{sale['receipt_no']}\n{sale['created_at'][11:16]}  {cashier}\n"
    buf += BOLD_ON + head.encode("cp437", "replace") + BOLD_OFF + b"-" * 32 + b"\n"
    for l in lines:
        buf += BIG + f"{fmt_qty(l['qty'])} x {l['name']}\n".encode("cp437", "replace") + NORMAL
        if l["note"]:
            buf += f"   ** {l['note']}\n".encode("cp437", "replace")
    buf += b"\n\n\n" + CUT
    return bytes(buf)


class PrintError(Exception):
    pass


def send(target, payload):
    """Send raw bytes to a printer target. 'browser' is handled by the front end."""
    try:
        if target.startswith("network:"):
            _, host, port = (target.split(":") + ["9100"])[:3]
            with socket.create_connection((host, int(port or 9100)), timeout=5) as sock:
                sock.sendall(payload)
        elif target.startswith("device:") or target.startswith("file:"):
            with open(target.split(":", 1)[1], "ab" if target.startswith("file:") else "wb") as f:
                f.write(payload)
        else:
            raise PrintError(f"Unknown printer target '{target}'")
    except (OSError, ValueError) as e:
        raise PrintError(f"Printer error ({target}): {e}")
