# seed_dev_data.py
#
# One-time convenience script for LOCAL DEVELOPMENT ONLY.
# Populates the database with sample users, products, restock history,
# and sales so the app is fully exercised without manual data entry.
#
# Usage:
#   python seed_dev_data.py            # seed if empty (safe to re-run)
#   python seed_dev_data.py --reset    # wipe all tables first, then seed
#
# Do NOT run this against a production database.

import os
import sys
import json
import datetime
import random

from app import app, db, User, Product, Sale, StockLog

SAMPLE_PRODUCTS = [
    # name, price (KSh), quantity, reorder_level
    ("2kg Maize Flour", 180.00, 40, 10),
    ("1kg Sugar", 150.00, 35, 10),
    ("500ml Cooking Oil", 220.00, 8, 10),   # low stock
    ("1L Fresh Milk", 70.00, 3, 5),          # low stock
    ("Bread Loaf", 65.00, 25, 8),
    ("Bar Soap", 45.00, 50, 15),
    ("2kg Rice", 260.00, 4, 10),             # low stock
    ("Toothpaste 100ml", 120.00, 20, 6),
    ("Matchbox", 10.00, 100, 20),
    ("1kg Salt", 40.00, 30, 10),
]


def seed():
    with app.app_context():
        if '--reset' in sys.argv:
            print("Dropping all tables...")
            db.drop_all()
        db.create_all()

        # --- Users ---
        admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', password=admin_password, role='admin')
            db.session.add(admin)
            print(f"Created admin user (password: {admin_password})")

        if not User.query.filter_by(username='cashier').first():
            cashier = User(username='cashier', password='cashier123', role='cashier')
            db.session.add(cashier)
            print("Created cashier user (password: cashier123)")

        db.session.commit()

        admin = User.query.filter_by(username='admin').first()
        cashier = User.query.filter_by(username='cashier').first()

        # --- Products ---
        if Product.query.count() == 0:
            for name, price, quantity, reorder_level in SAMPLE_PRODUCTS:
                db.session.add(Product(
                    name=name,
                    price=price,
                    quantity=quantity,
                    reorder_level=reorder_level
                ))
            db.session.commit()
            print(f"Created {len(SAMPLE_PRODUCTS)} sample products.")
        else:
            print("Products already exist — skipping product seed.")

        products = Product.query.all()

        # --- Restock history (a couple of log entries per product) ---
        if StockLog.query.count() == 0 and products:
            now = datetime.datetime.utcnow()
            for product in products:
                for days_ago in (14, 5):
                    added = random.randint(10, 30)
                    prev_qty = max(product.quantity - added, 0)
                    db.session.add(StockLog(
                        product_id=product.id,
                        quantity_added=added,
                        previous_quantity=prev_qty,
                        new_quantity=prev_qty + added,
                        note="Supplier delivery",
                        restocked_by=admin.username,
                        timestamp=now - datetime.timedelta(days=days_ago)
                    ))
            db.session.commit()
            print("Created sample restock history.")
        else:
            print("Stock logs already exist — skipping restock history seed.")

        # --- Sample completed sales ---
        if Sale.query.count() == 0 and products:
            now = datetime.datetime.utcnow()
            clerks = [admin, cashier]
            for i in range(8):
                sale_products = random.sample(products, k=random.randint(1, 3))
                items = []
                total = 0.0
                for p in sale_products:
                    qty = random.randint(1, 3)
                    subtotal = round(p.price * qty, 2)
                    items.append({
                        'id': p.id,
                        'name': p.name,
                        'price': p.price,
                        'quantity': qty,
                        'subtotal': subtotal
                    })
                    total += subtotal

                clerk = clerks[i % len(clerks)]
                sale_time = now - datetime.timedelta(days=random.randint(0, 10), hours=random.randint(0, 12))
                invoice_id = f"KRA-INV-{sale_time.strftime('%Y%m%d%H%M%S')}-{i}"

                db.session.add(Sale(
                    timestamp=sale_time,
                    total_amount=round(total, 2),
                    items=json.dumps(items),
                    user_id=clerk.id,
                    kra_invoice_id=invoice_id,
                    kra_qr_code_data=f"https://etims.kra.go.ke/verify?id={invoice_id}"
                ))
            db.session.commit()
            print("Created 8 sample completed sales.")
        else:
            print("Sales already exist — skipping sales seed.")

        print("\nDone. Log in with:")
        print(f"  admin    / {admin_password}")
        print("  cashier  / cashier123")


if __name__ == '__main__':
    seed()
