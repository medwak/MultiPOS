"""Industry profiles.

An industry is just a preset: a set of feature switches, wording ("Menu" vs "Products"),
starter categories and a few defaults. The admin picks an industry in Settings, the
preset is applied, and every switch can then be adjusted one by one.
"""

FEATURES = {
    "tables":            ("Tables / seating", "Assign orders to tables (restaurant floor)."),
    "open_orders":       ("Open orders / tabs", "Hold an order and add to it later before payment."),
    "kitchen_tickets":   ("Kitchen order tickets", "Print new items to the kitchen when an order is sent."),
    "item_notes":        ("Item notes / modifiers", "Free-text notes per line, e.g. 'no onions'."),
    "service_charge":    ("Service charge", "Add a percentage service charge to each bill."),
    "barcode":           ("Barcode scanning", "Scan a barcode into the search box to add the item."),
    "track_stock":       ("Inventory tracking", "Deduct stock on sale, low-stock alerts, stock purchases."),
    "expiry_tracking":   ("Expiry & batch numbers", "Record batch and expiry per product; expiring report."),
    "prescriptions":     ("Prescription-only items", "Require a prescription reference before selling flagged items."),
    "attendants":        ("Attendants & commissions", "Assign a staff member to each service line; commission report."),
    "decimal_qty":       ("Weighed / measured items", "Allow quantities like 1.25 kg or 3.5 m."),
    "customer_details":  ("Customer name & phone", "Capture customer details on the sale."),
}

_ALL_OFF = {k: False for k in FEATURES}


def _f(**on):
    d = dict(_ALL_OFF)
    d.update({k: True for k, v in on.items() if v})
    return d


INDUSTRIES = {
    "restaurant": {
        "label": "Restaurant / Café",
        "features": _f(tables=1, open_orders=1, kitchen_tickets=1, item_notes=1, service_charge=1, track_stock=1),
        "terms": {"product": "Menu item", "products": "Menu", "category": "Menu section"},
        "categories": ["Breakfast", "Starters", "Mains", "Sides", "Drinks", "Desserts"],
        "service_charge_rate": 10,
        "sample": [("Chapati", 3000, "Sides"), ("Beef Stew", 35000, "Mains"), ("Pilau", 30000, "Mains"),
                   ("Chips", 15000, "Sides"), ("Kenyan Tea", 8000, "Drinks"), ("Soda 500ml", 10000, "Drinks"),
                   ("Fruit Salad", 20000, "Desserts"), ("Samosa", 5000, "Starters")],
    },
    "bar": {
        "label": "Bar / Lounge",
        "features": _f(tables=1, open_orders=1, item_notes=1, track_stock=1, barcode=1),
        "terms": {"product": "Drink", "products": "Drinks", "category": "Section"},
        "categories": ["Beers", "Spirits", "Wines", "Soft drinks", "Bites"],
        "service_charge_rate": 0,
        "sample": [("Tusker 500ml", 30000, "Beers"), ("White Cap 500ml", 30000, "Beers"),
                   ("Soda 300ml", 8000, "Soft drinks"), ("Water 500ml", 7000, "Soft drinks"),
                   ("Smokie", 5000, "Bites")],
    },
    "retail": {
        "label": "Retail shop",
        "features": _f(barcode=1, track_stock=1, open_orders=1),
        "terms": {"product": "Product", "products": "Products", "category": "Category"},
        "categories": ["General", "Electronics", "Clothing", "Accessories"],
        "service_charge_rate": 0,
        "sample": [("Phone charger", 50000, "Electronics"), ("Earphones", 80000, "Electronics"),
                   ("T-shirt", 60000, "Clothing"), ("Umbrella", 45000, "Accessories")],
    },
    "supermarket": {
        "label": "Supermarket / Minimart",
        "features": _f(barcode=1, track_stock=1, expiry_tracking=1, decimal_qty=1, open_orders=1),
        "terms": {"product": "Product", "products": "Products", "category": "Department"},
        "categories": ["Groceries", "Fresh produce", "Dairy", "Household", "Beverages"],
        "service_charge_rate": 0,
        "sample": [("Maize flour 2kg", 18000, "Groceries"), ("Sugar 1kg", 16000, "Groceries"),
                   ("Milk 500ml", 6000, "Dairy"), ("Tomatoes (kg)", 12000, "Fresh produce"),
                   ("Bar soap", 9000, "Household")],
    },
    "pharmacy": {
        "label": "Pharmacy / Chemist",
        "features": _f(barcode=1, track_stock=1, expiry_tracking=1, prescriptions=1, customer_details=1),
        "terms": {"product": "Medicine / item", "products": "Medicines & items", "category": "Category"},
        "categories": ["OTC", "Prescription", "First aid", "Personal care", "Supplements"],
        "service_charge_rate": 0,
        "sample": [("Paracetamol 500mg x10", 5000, "OTC"), ("ORS sachet", 3000, "OTC"),
                   ("Amoxicillin 500mg x21", 45000, "Prescription"), ("Bandage roll", 10000, "First aid")],
    },
    "salon": {
        "label": "Salon / Barber / Spa",
        "features": _f(attendants=1, customer_details=1, open_orders=1, item_notes=1, track_stock=1),
        "terms": {"product": "Service", "products": "Services & products", "category": "Category"},
        "categories": ["Hair", "Nails", "Beauty", "Barber", "Retail products"],
        "service_charge_rate": 0,
        "sample": [("Haircut", 30000, "Barber"), ("Shave", 20000, "Barber"), ("Manicure", 50000, "Nails"),
                   ("Braiding", 150000, "Hair"), ("Hair oil", 40000, "Retail products")],
    },
    "hardware": {
        "label": "Hardware store",
        "features": _f(barcode=1, track_stock=1, decimal_qty=1, customer_details=1, open_orders=1),
        "terms": {"product": "Item", "products": "Items", "category": "Category"},
        "categories": ["Building", "Plumbing", "Electrical", "Paint", "Tools"],
        "service_charge_rate": 0,
        "sample": [("Cement 50kg", 75000, "Building"), ("PVC pipe (m)", 25000, "Plumbing"),
                   ("Cable 2.5mm (m)", 6000, "Electrical"), ("Hammer", 60000, "Tools")],
    },
    "general": {
        "label": "General business",
        "features": _f(track_stock=1, open_orders=1, barcode=1),
        "terms": {"product": "Item", "products": "Items", "category": "Category"},
        "categories": ["General"],
        "service_charge_rate": 0,
        "sample": [],
    },
}

# Items sold by measure in these industries get decimal quantities in the sample data.
MEASURED = ("(kg)", "(m)")
SERVICE_CATEGORIES = {"Hair", "Nails", "Beauty", "Barber"}
