"""Money helpers. Amounts are integer cents; quantities may be decimal."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_cents(value):
    """'1,250.50' / 1250.5 / '1250' -> 125050. Raises ValueError on garbage or negatives."""
    if value is None or value == "":
        return 0
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {value!r}")
    if d < 0:
        raise ValueError("Amount cannot be negative")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def rnd(x):
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(cents, currency=""):
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    s = f"{sign}{cents // 100:,}.{cents % 100:02d}"
    return f"{currency} {s}".strip()


def fmt_qty(q):
    q = float(q)
    return str(int(q)) if q == int(q) else f"{q:.3f}".rstrip("0").rstrip(".")


def compute_totals(lines, discount, prices_include_tax=True, service_rate=0.0):
    """Price a basket.

    lines: list of dicts with qty, unit_price (cents), tax_rate (%).
    discount: whole-bill discount in cents, spread across lines in proportion to value.
    Returns (line_results, totals) where every figure is integer cents and
    totals['total'] == net + tax + service_charge exactly.
    """
    gross_list = [rnd(Decimal(str(l["unit_price"])) * Decimal(str(l["qty"]))) for l in lines]
    gross = sum(gross_list)
    discount = max(0, min(int(discount or 0), gross))

    # Allocate the discount; the last non-zero line takes the rounding remainder.
    allocs, remaining = [], discount
    last = max((i for i, g in enumerate(gross_list) if g > 0), default=-1)
    for i, g in enumerate(gross_list):
        if i == last:
            d = remaining
        else:
            d = (discount * g) // gross if gross else 0
        allocs.append(d)
        remaining -= d

    results, net_sum, tax_sum = [], 0, 0
    for line, g, d in zip(lines, gross_list, allocs):
        share = g - d
        rate = Decimal(str(line.get("tax_rate") or 0))
        if prices_include_tax:
            tax = rnd(Decimal(share) * rate / (100 + rate)) if rate else 0
            net = share - tax
        else:
            tax = rnd(Decimal(share) * rate / 100)
            net = share
        results.append({"line_gross": g, "line_discount": d, "line_net": net, "line_tax": tax})
        net_sum += net
        tax_sum += tax

    service = rnd(Decimal(gross - discount) * Decimal(str(service_rate or 0)) / 100)
    totals = {"gross": gross, "discount": discount, "net": net_sum, "tax": tax_sum,
              "service_charge": service, "total": net_sum + tax_sum + service}
    return results, totals
