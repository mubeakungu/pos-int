import os
import io
import re
import hmac
import csv
import datetime
import json
import requests
from collections import defaultdict
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, Response, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
# New imports for QR Code generation
import qrcode
import base64
from io import BytesIO
# Barcode label generation
import barcode as barcode_lib
from barcode.writer import ImageWriter

import integrations

# --- ADD THESE TWO LINES ---
from dotenv import load_dotenv
load_dotenv()
# --- END OF ADDITION ---

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your_very_secret_key')
if app.config['SECRET_KEY'] == 'your_very_secret_key':
    print("⚠️  WARNING: SECRET_KEY env var not set — using an insecure default. Set SECRET_KEY before deploying to production.")
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')

if not app.config['SQLALCHEMY_DATABASE_URI']:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. On Render: open this service's "
        "Environment tab and set DATABASE_URL to your Postgres instance's Internal Database URL "
        "(create a Postgres database first if you haven't). Locally: set it in your .env file "
        "(see .env.example)."
    )

# Handle PostgreSQL URL format and add SSL for Render
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgresql") and 'sslmode' not in app.config['SQLALCHEMY_DATABASE_URI']:
    sep = '&' if '?' in app.config['SQLALCHEMY_DATABASE_URI'] else '?'
    app.config['SQLALCHEMY_DATABASE_URI'] += f'{sep}sslmode=require'


app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if app.config['SQLALCHEMY_DATABASE_URI'].startswith("sqlite"):
    # SQLite (handy for local dev and tests) doesn't support the Postgres pool options below.
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
        'pool_timeout': 20,
        'max_overflow': 0
    }

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
# Initialize Flask-Migrate with the app and db
migrate = Migrate(app, db)
# Initialize CSRF protection on all forms
csrf = CSRFProtect(app)

# Cloudinary configuration
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET')
)

# --- Database Models ---
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='cashier')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    @property
    def password(self):
        raise AttributeError('password is not a readable attribute')

    @password.setter
    def password(self, password):
        self.password_hash = generate_password_hash(password)

    def verify_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    quantity = db.Column(db.Integer, default=0)
    reorder_level = db.Column(db.Integer, default=5, nullable=False)
    category = db.Column(db.String(60), nullable=True)
    tax_type = db.Column(db.String(1), nullable=True, default='B')  # eTIMS tax type: A exempt, B 16%, C zero, D non-VAT, E 8%
    cost_price = db.Column(db.Float, nullable=True)
    barcode = db.Column(db.String(64), unique=True, nullable=True)
    image_url = db.Column(db.String(255), nullable=True)
    public_id = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    @property
    def is_low_stock(self):
        return self.quantity <= self.reorder_level

class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    total_amount = db.Column(db.Float, nullable=False)
    items = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    # New columns to store KRA-related data
    kra_invoice_id = db.Column(db.String(255), nullable=True)
    kra_qr_code_data = db.Column(db.String(512), nullable=True)

    # Customer contact (optional, for emailing/texting the receipt)
    customer_name = db.Column(db.String(100), nullable=True)
    customer_phone = db.Column(db.String(30), nullable=True)
    customer_email = db.Column(db.String(120), nullable=True)

    # Discount applied at checkout
    discount_amount = db.Column(db.Float, nullable=False, default=0.0)
    discount_reason = db.Column(db.String(255), nullable=True)

    # Refund tracking
    refunded = db.Column(db.Boolean, nullable=False, default=False)
    refunded_at = db.Column(db.DateTime, nullable=True)
    refunded_by = db.Column(db.String(80), nullable=True)
    refund_reason = db.Column(db.String(255), nullable=True)

    clerk = db.relationship('User', backref=db.backref('sales', lazy=True))

    @property
    def subtotal(self):
        return self.total_amount + self.discount_amount

class StockLog(db.Model):
    __tablename__ = 'stock_logs'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity_added = db.Column(db.Integer, nullable=False)
    previous_quantity = db.Column(db.Integer, nullable=False)
    new_quantity = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(255), nullable=True)
    restocked_by = db.Column(db.String(80), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    product = db.relationship('Product', backref=db.backref('stock_logs', lazy=True, order_by='StockLog.timestamp.desc()'))

class BusinessSettings(db.Model):
    __tablename__ = 'business_settings'
    id = db.Column(db.Integer, primary_key=True)
    business_name = db.Column(db.String(150), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    mpesa_till_number = db.Column(db.String(30), nullable=True)
    mpesa_paybill_number = db.Column(db.String(30), nullable=True)
    mpesa_paybill_account = db.Column(db.String(60), nullable=True)
    currency_code = db.Column(db.String(10), nullable=False, default='KES')
    currency_symbol = db.Column(db.String(10), nullable=False, default='KSh')
    secondary_currency_code = db.Column(db.String(10), nullable=True)
    secondary_currency_symbol = db.Column(db.String(10), nullable=True)
    secondary_exchange_rate = db.Column(db.Float, nullable=True)  # units of primary currency per 1 unit of secondary
    secondary_rate_auto = db.Column(db.Boolean, nullable=False, default=False)
    secondary_rate_updated_at = db.Column(db.DateTime, nullable=True)
    # Shown on quotations / invoices
    kra_pin = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    bank_details = db.Column(db.Text, nullable=True)
    invoice_terms = db.Column(db.Text, nullable=True)
    quotation_valid_days = db.Column(db.Integer, nullable=True)
    invoice_due_days = db.Column(db.Integer, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    @classmethod
    def get(cls):
        """Return the single settings row, creating a blank one if it doesn't exist yet."""
        settings = cls.query.first()
        if not settings:
            settings = cls()
            db.session.add(settings)
            db.session.commit()
        return settings

    def convert_to_secondary(self, primary_amount):
        """Convert a primary-currency amount into the configured secondary currency, if set."""
        if not self.secondary_exchange_rate or not self.secondary_currency_symbol:
            return None
        try:
            return primary_amount / self.secondary_exchange_rate
        except (TypeError, ZeroDivisionError):
            return None

    def fetch_live_secondary_rate(self):
        """
        Look up how many units of the primary currency equal 1 unit of the secondary
        currency, using the free open.er-api.com exchange-rate API (no key required).
        Returns (rate, error_message) — exactly one of which will be None.
        """
        if not self.secondary_currency_code:
            return None, "No secondary currency configured."
        primary = (self.currency_code or 'KES').upper()
        secondary = self.secondary_currency_code.upper()
        try:
            resp = requests.get(f"https://open.er-api.com/v6/latest/{secondary}", timeout=6)
            resp.raise_for_status()
            data = resp.json()
            if data.get('result') != 'success':
                return None, data.get('error-type', 'The exchange rate service returned an error.')
            rate = data.get('rates', {}).get(primary)
            if rate is None:
                return None, f"No rate found for {primary} in the API response."
            return float(rate), None
        except requests.RequestException as e:
            return None, f"Could not reach the exchange rate service ({e})."
        except (ValueError, KeyError) as e:
            return None, f"Unexpected response from the exchange rate service ({e})."

    def is_secondary_rate_stale(self, hours=1):
        if not self.secondary_rate_updated_at:
            return True
        return (datetime.datetime.utcnow() - self.secondary_rate_updated_at) > datetime.timedelta(hours=hours)

    def refresh_secondary_rate_if_stale(self, hours=1):
        """Silently refresh the live rate if it's due; swallow errors and keep the last known rate."""
        if not (self.secondary_rate_auto and self.secondary_currency_code):
            return
        if not self.is_secondary_rate_stale(hours=hours):
            return
        rate, error = self.fetch_live_secondary_rate()
        if rate is not None:
            self.secondary_exchange_rate = rate
            self.secondary_rate_updated_at = datetime.datetime.utcnow()
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Error saving auto-refreshed exchange rate: {e}")
        else:
            print(f"Live exchange rate auto-refresh skipped: {error}")

    @property
    def payment_qr_text(self):
        """Text encoded into the payment QR code shown on receipts."""
        lines = []
        if self.business_name:
            lines.append(self.business_name)
        if self.mpesa_till_number:
            lines.append(f"M-Pesa Buy Goods Till: {self.mpesa_till_number}")
        elif self.mpesa_paybill_number:
            lines.append(f"M-Pesa Paybill: {self.mpesa_paybill_number}")
            if self.mpesa_paybill_account:
                lines.append(f"Account: {self.mpesa_paybill_account}")
        return "\n".join(lines) if lines else None

# --- Integration Models ---
# These live in their own tables (rather than new columns on `sales`) because
# the startup schema init only creates missing tables, so existing databases
# pick them up on the next boot without a migration.
class IntegrationConfig(db.Model):
    """One configured connection to an external API (an instance of an adapter)."""
    __tablename__ = 'integration_configs'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), unique=True, nullable=False)
    adapter = db.Column(db.String(60), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    settings_json = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    @property
    def settings(self):
        try:
            return json.loads(self.settings_json or '{}')
        except ValueError:
            return {}

    @settings.setter
    def settings(self, value):
        self.settings_json = json.dumps(value or {})

    @property
    def adapter_class(self):
        return integrations.get_adapter(self.adapter)

    def instance(self):
        cls = self.adapter_class
        return cls(self.key, self.settings) if cls else None


class IntegrationLog(db.Model):
    """Audit trail of every call to an external API, with enough detail to retry it."""
    __tablename__ = 'integration_logs'
    id = db.Column(db.Integer, primary_key=True)
    integration_key = db.Column(db.String(60), nullable=False, index=True)
    event = db.Column(db.String(60), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # success | failed | skipped | retried
    sale_id = db.Column(db.Integer, nullable=True, index=True)
    payload_json = db.Column(db.Text, nullable=True)   # the POS event payload (used for retries)
    request_json = db.Column(db.Text, nullable=True)   # what was sent (secrets redacted)
    response_json = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Payment(db.Model):
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=False, index=True)
    method = db.Column(db.String(30), nullable=False, default='cash')  # cash | mpesa | card | ...
    provider = db.Column(db.String(60), nullable=True)  # integration key that processed it
    amount = db.Column(db.Float, nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending | paid | failed | cancelled
    provider_ref = db.Column(db.String(120), nullable=True, index=True)
    receipt_number = db.Column(db.String(60), nullable=True)
    message = db.Column(db.String(255), nullable=True)
    raw_callback = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    sale = db.relationship('Sale', backref=db.backref('payments', lazy=True, order_by='Payment.id'))


class SaleMeta(db.Model):
    """Extra per-sale data used by integrations (customer KRA PIN, payment method, fiscal data)."""
    __tablename__ = 'sale_meta'
    sale_id = db.Column(db.Integer, db.ForeignKey('sales.id'), primary_key=True)
    customer_pin = db.Column(db.String(20), nullable=True)
    payment_method = db.Column(db.String(30), nullable=True)
    fiscal_provider = db.Column(db.String(60), nullable=True)
    fiscal_signature = db.Column(db.String(255), nullable=True)
    fiscal_json = db.Column(db.Text, nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    shift_id = db.Column(db.Integer, db.ForeignKey('shifts.id'), nullable=True)
    document_id = db.Column(db.Integer, nullable=True)  # set when the sale fulfils an invoice

    sale = db.relationship('Sale', backref=db.backref('meta', uselist=False))
    customer = db.relationship('Customer')


# --- Business Models: customers, shifts, documents, suppliers ---
TAX_TYPES = [('B', 'VAT 16%'), ('E', 'VAT 8%'), ('A', 'Exempt'), ('C', 'Zero-rated'), ('D', 'Non-VAT')]
TAX_RATES = {'A': 0.0, 'B': 16.0, 'C': 0.0, 'D': 0.0, 'E': 8.0}


class Customer(db.Model):
    __tablename__ = 'customers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30), nullable=True, index=True)
    email = db.Column(db.String(120), nullable=True)
    kra_pin = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    credit_limit = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Shift(db.Model):
    """A cashier's session at the till, from opening float to cash-up."""
    __tablename__ = 'shifts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    opened_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    opening_float = db.Column(db.Float, nullable=False, default=0.0)
    closed_at = db.Column(db.DateTime, nullable=True)
    counted_cash = db.Column(db.Float, nullable=True)
    expected_cash = db.Column(db.Float, nullable=True)
    notes = db.Column(db.String(255), nullable=True)

    user = db.relationship('User')

    @property
    def is_open(self):
        return self.closed_at is None


class Document(db.Model):
    """Quotations, proforma invoices and invoices share one table and numbering per type."""
    __tablename__ = 'documents'
    id = db.Column(db.Integer, primary_key=True)
    doc_type = db.Column(db.String(20), nullable=False, index=True)  # quotation | proforma | invoice
    number = db.Column(db.String(30), nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default='draft')
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    issue_date = db.Column(db.Date, nullable=False, default=datetime.date.today)
    valid_until = db.Column(db.Date, nullable=True)  # quotations / proformas
    due_date = db.Column(db.Date, nullable=True)     # invoices
    lines_json = db.Column(db.Text, nullable=False, default='[]')
    discount_amount = db.Column(db.Float, nullable=False, default=0.0)
    notes = db.Column(db.Text, nullable=True)
    terms = db.Column(db.Text, nullable=True)
    source_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=True)  # converted from
    sale_id = db.Column(db.Integer, db.ForeignKey('sales.id'), nullable=True)  # stock/eTIMS record for an issued invoice
    created_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    customer = db.relationship('Customer', backref=db.backref('documents', lazy=True))
    source = db.relationship('Document', remote_side=[id])
    sale = db.relationship('Sale')

    LABELS = {'quotation': 'Quotation', 'proforma': 'Proforma Invoice', 'invoice': 'Invoice'}
    PREFIXES = {'quotation': 'QUO', 'proforma': 'PRO', 'invoice': 'INV'}

    @property
    def label(self):
        return self.LABELS.get(self.doc_type, self.doc_type.title())

    @property
    def lines(self):
        try:
            return json.loads(self.lines_json or '[]')
        except ValueError:
            return []

    @lines.setter
    def lines(self, value):
        self.lines_json = json.dumps(value or [])

    @property
    def subtotal(self):
        return round(sum(float(l.get('quantity', 0)) * float(l.get('price', 0)) for l in self.lines), 2)

    @property
    def total(self):
        return round(max(self.subtotal - (self.discount_amount or 0), 0), 2)

    @property
    def tax_breakdown(self):
        """VAT included in the total, per tax type, after spreading the discount across lines."""
        subtotal = self.subtotal
        ratio = (self.total / subtotal) if subtotal else 0
        out = {}
        for line in self.lines:
            amount = float(line.get('quantity', 0)) * float(line.get('price', 0)) * ratio
            tax_type = line.get('tax_type') or 'B'
            rate = TAX_RATES.get(tax_type, 0.0)
            entry = out.setdefault(tax_type, {'taxable': 0.0, 'tax': 0.0, 'rate': rate})
            entry['taxable'] += amount
            entry['tax'] += amount * rate / (100 + rate) if rate else 0.0
        return {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in out.items()}

    @property
    def total_tax(self):
        return round(sum(v['tax'] for v in self.tax_breakdown.values()), 2)

    @property
    def amount_paid(self):
        return round(sum(p.amount for p in self.payments), 2)

    @property
    def balance(self):
        return round(self.total - self.amount_paid, 2) if self.doc_type == 'invoice' else 0.0

    @property
    def is_overdue(self):
        return (self.doc_type == 'invoice' and self.status in ('unpaid', 'partial')
                and self.due_date is not None and self.due_date < datetime.date.today())

    @property
    def display_status(self):
        return 'overdue' if self.is_overdue else self.status

    @property
    def is_editable(self):
        return self.status == 'draft'

    def refresh_payment_status(self):
        if self.doc_type != 'invoice' or self.status in ('draft', 'void'):
            return
        if self.amount_paid >= self.total - 0.005:
            self.status = 'paid'
        elif self.amount_paid > 0:
            self.status = 'partial'
        else:
            self.status = 'unpaid'


class DocumentPayment(db.Model):
    __tablename__ = 'document_payments'
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey('documents.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    method = db.Column(db.String(30), nullable=False, default='cash')  # cash | mpesa | bank | cheque | card
    reference = db.Column(db.String(80), nullable=True)
    paid_on = db.Column(db.Date, nullable=False, default=datetime.date.today)
    recorded_by = db.Column(db.String(80), nullable=True)
    shift_id = db.Column(db.Integer, db.ForeignKey('shifts.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    document = db.relationship('Document', backref=db.backref('payments', lazy=True, order_by='DocumentPayment.id'))


class Supplier(db.Model):
    __tablename__ = 'suppliers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    kra_pin = db.Column(db.String(20), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class PurchaseOrder(db.Model):
    __tablename__ = 'purchase_orders'
    id = db.Column(db.Integer, primary_key=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey('suppliers.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='ordered')  # ordered | received | cancelled
    lines_json = db.Column(db.Text, nullable=False, default='[]')  # [{product_id, name, quantity, unit_cost}]
    supplier_invoice_no = db.Column(db.String(60), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    expected_on = db.Column(db.Date, nullable=True)
    received_at = db.Column(db.DateTime, nullable=True)
    received_by = db.Column(db.String(80), nullable=True)

    supplier = db.relationship('Supplier', backref=db.backref('purchase_orders', lazy=True))

    @property
    def number(self):
        return f"PO-{self.id:04d}"

    @property
    def lines(self):
        try:
            return json.loads(self.lines_json or '[]')
        except ValueError:
            return []

    @lines.setter
    def lines(self, value):
        self.lines_json = json.dumps(value or [])

    @property
    def total(self):
        return round(sum(float(l.get('quantity', 0)) * float(l.get('unit_cost', 0)) for l in self.lines), 2)


def ensure_columns():
    """
    Add columns introduced after a table was first created. There's no Alembic
    migrations folder (and no shell on Render's free tier), so this keeps
    existing databases in step on boot. It only ever ADDs nullable columns.
    """
    wanted = {
        'products': {'category': 'VARCHAR(60)', 'tax_type': 'VARCHAR(1)', 'cost_price': 'FLOAT'},
        'business_settings': {'kra_pin': 'VARCHAR(20)', 'email': 'VARCHAR(120)', 'bank_details': 'TEXT',
                              'invoice_terms': 'TEXT', 'quotation_valid_days': 'INTEGER', 'invoice_due_days': 'INTEGER'},
        'sale_meta': {'customer_id': 'INTEGER', 'shift_id': 'INTEGER', 'document_id': 'INTEGER'},
    }
    inspector = db.inspect(db.engine)
    tables = set(inspector.get_table_names())
    for table, columns in wanted.items():
        if table not in tables:
            continue
        existing = {c['name'] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                db.session.execute(db.text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
                print(f"✅ Added column {table}.{name}")
    db.session.commit()


# --- User Loader ---
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Access Control ---
from functools import wraps

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Admin access required for that action.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# --- Template Globals ---
@app.context_processor
def inject_currency():
    try:
        settings = BusinessSettings.get()
        return dict(
            currency_symbol=settings.currency_symbol or 'KSh',
            currency_code=settings.currency_code or 'KES'
        )
    except Exception as e:
        print(f"Error loading currency settings: {e}")
        return dict(currency_symbol='KSh', currency_code='KES')

def seed_default_integrations():
    """Create the built-in M-Pesa and eTIMS configs (in mock mode) the first time the app boots."""
    defaults = [
        # eTIMS starts enabled in mock mode, matching the old placeholder KRA invoice behaviour.
        ('kra_etims', 'kra_etims', 'KRA eTIMS', True),
        ('mpesa', 'mpesa_daraja', 'M-Pesa (Daraja)', False),
    ]
    for key, adapter, name, enabled in defaults:
        if not IntegrationConfig.query.filter_by(key=key).first():
            db.session.add(IntegrationConfig(key=key, adapter=adapter, name=name, enabled=enabled,
                                             settings={'mode': 'mock'}))
    db.session.commit()

# --- Startup: self-healing schema init ---
# No Shell access on Render's free tier and no Alembic migrations folder yet,
# so create any tables that don't exist and seed a default admin. This only
# creates MISSING tables — it never touches or alters tables that already
# exist, so it's safe to run on every boot and won't overwrite real data.
with app.app_context():
    try:
        db.create_all()
        ensure_columns()
        if not User.query.filter_by(username='admin').first():
            admin_password = os.environ.get('ADMIN_PASSWORD')
            if not admin_password:
                admin_password = 'admin123'
                print("⚠️  ADMIN_PASSWORD env var not set — using insecure default 'admin123'. Set ADMIN_PASSWORD and change the password after first login.")
            admin_user = User(username='admin', password=admin_password, role='admin')
            db.session.add(admin_user)
            db.session.commit()
            print("✅ Default admin user created.")
        seed_default_integrations()
    except Exception as e:
        print(f"⚠️  Startup schema initialization failed: {e}")

# --- Utility Functions ---
def generate_qr_code_b64(data):
    """
    Generates a QR code for the given data and returns it as a base64 encoded string.
    """
    if not data:
        return None
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save QR code to a BytesIO object
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    
    # Encode to base64
    qr_code_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return qr_code_b64

# --- Integration Services ---
# The POS talks to external APIs only through these helpers: it emits an
# event, and every enabled integration subscribed to that event handles it.

def _to_json(value):
    if value is None:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def get_integration(key):
    """Return (config, adapter instance) for a configured integration, or (None, None)."""
    config = IntegrationConfig.query.filter_by(key=key).first()
    if not config:
        return None, None
    return config, config.instance()


def enabled_integrations():
    out = []
    for config in IntegrationConfig.query.filter_by(enabled=True).order_by(IntegrationConfig.id).all():
        instance = config.instance()
        if instance is not None:
            out.append((config, instance))
    return out


def payment_integrations():
    """Enabled integrations that can take payments at checkout."""
    return [(c, i) for c, i in enabled_integrations() if i.category == 'payments']


def sale_payload(sale):
    """Everything an integration may need to know about a sale, as plain JSON."""
    settings = BusinessSettings.get()
    try:
        items = json.loads(sale.items)
    except (ValueError, TypeError):
        items = []
    products = {p.id: p for p in Product.query.filter(Product.id.in_([i.get('id') for i in items])).all()} if items else {}
    for item in items:
        product = products.get(item.get('id'))
        if product is not None and product.barcode:
            item.setdefault('barcode', product.barcode)
        if product is not None and not item.get('tax_type'):
            item['tax_type'] = product.tax_type or 'B'
    meta = sale.meta
    return {
        'id': sale.id,
        'timestamp': sale.timestamp.isoformat() if sale.timestamp else None,
        'timestamp_eat': (sale.timestamp + datetime.timedelta(hours=3)) if sale.timestamp else None,
        'items': items,
        'subtotal': round(sale.subtotal, 2),
        'discount': round(sale.discount_amount or 0, 2),
        'total': round(sale.total_amount, 2),
        'currency': settings.currency_code or 'KES',
        'customer_name': sale.customer_name,
        'customer_phone': sale.customer_phone,
        'customer_email': sale.customer_email,
        'customer_pin': meta.customer_pin if meta else None,
        'payment_method': meta.payment_method if meta else None,
        'cashier': sale.clerk.username if sale.clerk else None,
        'business_name': settings.business_name,
        'business_address': settings.address,
        'refunded': sale.refunded,
        'refund_reason': sale.refund_reason,
        'kra_invoice_id': sale.kra_invoice_id,
    }


def payment_payload(payment):
    return {
        'id': payment.id,
        'sale_id': payment.sale_id,
        'method': payment.method,
        'amount': round(payment.amount, 2),
        'phone': payment.phone,
        'status': payment.status,
        'provider_ref': payment.provider_ref,
        'receipt_number': payment.receipt_number,
        'reference': f"SALE{payment.sale_id}",
    }


def payment_method_name(config):
    """Short method name stored on the payment, e.g. 'mpesa' (falls back to the integration key)."""
    cls = config.adapter_class
    return getattr(cls, 'payment_method_code', None) or config.key


def request_payment(payment):
    """Ask the payment's provider to collect it (e.g. send an M-Pesa STK push)."""
    results = emit_event(integrations.PAYMENT_REQUESTED, sale=payment.sale, payment=payment, only=payment.provider)
    result = results.get(payment.provider)
    if result is None:
        payment.status = 'failed'
        payment.message = 'Payment provider is not enabled.'
    elif result.ok:
        payment.status = 'pending'
        payment.provider_ref = result.data.get('provider_ref')
        payment.message = result.data.get('message')
    else:
        payment.status = 'failed'
        payment.message = (result.error or 'Payment request failed')[:255]
    db.session.commit()
    return result


def apply_fiscal(sale, provider_key, fiscal):
    """Put a tax integration's signed invoice number + QR data on the sale."""
    if not fiscal or not fiscal.get('invoice_number'):
        return
    sale.kra_invoice_id = str(fiscal['invoice_number'])[:255]
    if fiscal.get('qr_data'):
        sale.kra_qr_code_data = str(fiscal['qr_data'])[:512]
    meta = sale.meta or SaleMeta(sale_id=sale.id)
    meta.fiscal_provider = provider_key
    meta.fiscal_signature = (fiscal.get('signature') or '')[:255] or None
    meta.fiscal_json = _to_json(fiscal)
    db.session.add(meta)


def run_integration(config, instance, event, payload, sale=None, log=None):
    """Call one integration, record the outcome, and apply anything it returns. Never raises."""
    try:
        result = instance.handle(event, payload)
    except Exception as e:
        print(f"Integration {config.key} crashed on {event}: {e}")
        result = integrations.Result.failure(f'Unexpected error: {e}')

    status = 'skipped' if result.skipped else ('success' if result.ok else 'failed')
    if log is None:
        log = IntegrationLog(integration_key=config.key, event=event, sale_id=sale.id if sale else None,
                             payload_json=_to_json(payload))
    else:
        log.attempts = (log.attempts or 1) + 1
    log.status = status
    log.request_json = _to_json(result.request)
    log.response_json = _to_json(result.response if result.response is not None else result.data)
    log.error = result.error
    db.session.add(log)

    if result.ok and sale is not None and result.data.get('fiscal') and event in (integrations.SALE_COMPLETED,):
        apply_fiscal(sale, config.key, result.data['fiscal'])
    return result


def emit_event(event, sale=None, payment=None, only=None):
    """
    Send a POS event to every enabled integration that subscribes to it.
    `only` limits delivery to one integration key (used for payment requests).
    Returns {integration_key: Result}. Commits the session.
    """
    payload = {}
    if sale is not None:
        payload['sale'] = sale_payload(sale)
    if payment is not None:
        payload['payment'] = payment_payload(payment)
    results = {}
    for config, instance in enabled_integrations():
        if only and config.key != only:
            continue
        if not instance.handles(event):
            continue
        if event == integrations.PAYMENT_REQUESTED:
            payload['callback_url'] = integration_callback_url(config, instance)
        results[config.key] = run_integration(config, instance, event, payload, sale=sale)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error saving integration results for {event}: {e}")
    return results


def integration_callback_url(config, instance):
    base = (os.environ.get('PUBLIC_BASE_URL') or request.url_root).rstrip('/')
    url = f"{base}{url_for('integration_callback', key=config.key)}"
    token = instance.setting('callback_token') if any(f.name == 'callback_token' for f in instance.fields) else None
    return f"{url}?token={token}" if token else url


def finalize_payment(payment, status, receipt_number=None, message=None, raw=None):
    """Mark a payment paid/failed (idempotent) and fire the follow-up events."""
    if payment.status == 'paid':
        return False
    payment.status = status
    if receipt_number:
        payment.receipt_number = str(receipt_number)[:60]
    if message:
        payment.message = str(message)[:255]
    if raw is not None:
        payment.raw_callback = _to_json(raw)
    db.session.commit()

    sale = payment.sale
    if status == 'paid':
        emit_event(integrations.PAYMENT_COMPLETED, sale=sale, payment=payment)
        if all(p.status == 'paid' for p in sale.payments if p.status != 'cancelled'):
            emit_event(integrations.SALE_COMPLETED, sale=sale, payment=payment)
    elif status == 'failed':
        emit_event(integrations.PAYMENT_FAILED, sale=sale, payment=payment)
    return True


def restock_sale_items(sale, note, username):
    try:
        items_data = json.loads(sale.items)
    except (json.JSONDecodeError, TypeError):
        items_data = []
    for item in items_data:
        product = Product.query.get(item.get('id'))
        if product:
            qty = item.get('quantity', 0)
            previous_quantity = product.quantity
            product.quantity += qty
            db.session.add(StockLog(
                product_id=product.id,
                quantity_added=qty,
                previous_quantity=previous_quantity,
                new_quantity=product.quantity,
                note=note,
                restocked_by=username
            ))

def product_form_context():
    categories = [c for (c,) in db.session.query(Product.category).filter(Product.category.isnot(None)).distinct().order_by(Product.category)]
    return {'categories': categories, 'tax_types': TAX_TYPES}


def apply_product_extras(product, form):
    product.category = (form.get('category') or '').strip() or None
    tax_type = (form.get('tax_type') or 'B').strip().upper()
    product.tax_type = tax_type if tax_type in TAX_RATES else 'B'
    cost = (form.get('cost_price') or '').strip()
    try:
        product.cost_price = float(cost) if cost else None
    except ValueError:
        pass


# --- Routes ---
@app.route('/')
@login_required
def index():
    try:
        all_products = Product.query.all()
        total_sales = db.session.query(db.func.sum(Sale.total_amount)).scalar() or 0
        total_items = 0
        try:
            for sale in Sale.query.all():
                if sale.items:
                    items_data = json.loads(sale.items)
                    total_items += sum(item.get('quantity', 0) for item in items_data)
        except (json.JSONDecodeError, KeyError, TypeError):
            total_items = 0

        low_stock_products = [p for p in all_products if p.is_low_stock]

        today_start = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        today_sales = db.session.query(db.func.sum(Sale.total_amount)).filter(
            Sale.timestamp >= today_start, Sale.refunded.is_(False)).scalar() or 0
        open_invoices = Document.query.filter(Document.doc_type == 'invoice', Document.status.in_(('unpaid', 'partial'))).all()

        return render_template(
            'index.html',
            total_sales=total_sales,
            total_items=total_items,
            products=all_products,
            low_stock_products=low_stock_products,
            today_sales=today_sales,
            outstanding=sum(d.balance for d in open_invoices),
            overdue_count=sum(1 for d in open_invoices if d.is_overdue)
        )
    except Exception as e:
        print(f"Error in index route: {e}")
        flash('Error loading dashboard data', 'error')
        return render_template('index.html', total_sales=0, total_items=0, products=[], low_stock_products=[])

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        try:
            user = User.query.filter_by(username=username).first()
            if user and user.verify_password(password):
                login_user(user)
                flash('Logged in successfully!', 'success')
                return redirect(url_for('index'))
            else:
                flash('Invalid username or password.', 'error')
        except Exception as e:
            print(f"Login error: {e}")
            flash('Login system error. Please try again.', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out successfully.', 'success')
    return redirect(url_for('login'))

@app.route('/products', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_products():
    if request.method == 'POST':
        name = request.form.get('name')
        price_str = request.form.get('price')
        quantity_str = request.form.get('quantity')
        reorder_level_str = request.form.get('reorder_level', '5')
        barcode = request.form.get('barcode', '').strip() or None
        image = request.files.get('image')
        image_url = None
        public_id = None

        if not name or not price_str or not quantity_str:
            flash('Name, price, and quantity are required.', 'error')
            return redirect(url_for('manage_products'))

        if barcode and Product.query.filter_by(barcode=barcode).first():
            flash(f'Barcode "{barcode}" is already assigned to another product.', 'error')
            return redirect(url_for('manage_products'))

        try:
            price = float(price_str)
            quantity = int(quantity_str)
            reorder_level = int(reorder_level_str) if reorder_level_str else 5
        except (ValueError, TypeError):
            flash('Price, quantity, and reorder level must be valid numbers.', 'error')
            return redirect(url_for('manage_products'))

        if image and image.filename and not os.environ.get('CLOUDINARY_CLOUD_NAME'):
            flash('Product saved without its image: set the CLOUDINARY_* environment variables to enable image uploads.', 'info')
            image = None
        if image and image.filename:
            try:
                upload_result = cloudinary.uploader.upload(image)
                image_url = upload_result['secure_url']
                public_id = upload_result['public_id']
            except Exception as e:
                flash(f'Image upload failed: {e}', 'error')
                return redirect(url_for('manage_products'))
            
        try:
            new_product = Product(name=name, price=price, quantity=quantity, reorder_level=reorder_level, barcode=barcode, image_url=image_url, public_id=public_id)
            apply_product_extras(new_product, request.form)
            db.session.add(new_product)
            db.session.commit()
            flash('Product added successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            print(f"Error adding product: {e}")
            flash('Error adding product to database', 'error')
            
        return redirect(url_for('manage_products'))

    try:
        all_products = Product.query.order_by(Product.category, Product.name).all()
    except Exception as e:
        print(f"Error fetching products: {e}")
        all_products = []
        flash('Error loading products', 'error')
        
    return render_template('products.html', products=all_products, **product_form_context())

@app.route('/products/edit/<int:product_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_product(product_id):
    try:
        product = Product.query.get_or_404(product_id)
    except Exception as e:
        print(f"Error fetching product: {e}")
        flash('Product not found', 'error')
        return redirect(url_for('manage_products'))
        
    if request.method == 'POST':
        try:
            new_barcode = request.form.get('barcode', '').strip() or None
            if new_barcode:
                clash = Product.query.filter(Product.barcode == new_barcode, Product.id != product.id).first()
                if clash:
                    flash(f'Barcode "{new_barcode}" is already assigned to {clash.name}.', 'error')
                    return render_template('edit_product.html', product=product, **product_form_context())

            product.name = request.form.get('name')
            product.price = float(request.form.get('price'))
            product.quantity = int(request.form.get('quantity'))
            product.barcode = new_barcode
            apply_product_extras(product, request.form)
            reorder_level_str = request.form.get('reorder_level')
            if reorder_level_str:
                product.reorder_level = int(reorder_level_str)
            product.updated_at = datetime.datetime.utcnow()
            image = request.files.get('image')
            if image and image.filename and not os.environ.get('CLOUDINARY_CLOUD_NAME'):
                flash('Image not changed: set the CLOUDINARY_* environment variables to enable image uploads.', 'info')
                image = None

            if image and image.filename:
                if product.public_id:
                    cloudinary.uploader.destroy(product.public_id)
                upload_result = cloudinary.uploader.upload(image)
                product.image_url = upload_result['secure_url']
                product.public_id = upload_result['public_id']
                
            db.session.commit()
            flash('Product updated successfully!', 'success')
            return redirect(url_for('manage_products'))
        except Exception as e:
            db.session.rollback()
            print(f"Error updating product: {e}")
            flash('Error updating product', 'error')
            
    return render_template('edit_product.html', product=product, **product_form_context())

@app.route('/products/restock/<int:product_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def restock_product(product_id):
    try:
        product = Product.query.get_or_404(product_id)
    except Exception as e:
        print(f"Error fetching product for restock: {e}")
        flash('Product not found', 'error')
        return redirect(url_for('manage_products'))

    if request.method == 'POST':
        quantity_str = request.form.get('quantity_added')
        note = request.form.get('note', '').strip() or None

        try:
            quantity_added = int(quantity_str)
            if quantity_added <= 0:
                raise ValueError
        except (ValueError, TypeError):
            flash('Enter a whole number greater than 0 to restock.', 'error')
            return redirect(url_for('restock_product', product_id=product_id))

        try:
            previous_quantity = product.quantity
            product.quantity = previous_quantity + quantity_added
            product.updated_at = datetime.datetime.utcnow()

            log_entry = StockLog(
                product_id=product.id,
                quantity_added=quantity_added,
                previous_quantity=previous_quantity,
                new_quantity=product.quantity,
                note=note,
                restocked_by=current_user.username if current_user.is_authenticated else None
            )
            db.session.add(log_entry)
            db.session.commit()
            flash(f'Restocked {product.name}: {previous_quantity} → {product.quantity} units.', 'success')
            return redirect(url_for('manage_products'))
        except Exception as e:
            db.session.rollback()
            print(f"Error restocking product: {e}")
            flash('Error restocking product', 'error')

    try:
        recent_logs = StockLog.query.filter_by(product_id=product.id).order_by(StockLog.timestamp.desc()).limit(10).all()
    except Exception as e:
        print(f"Error loading restock history: {e}")
        recent_logs = []

    return render_template('restock_product.html', product=product, recent_logs=recent_logs)

@app.route('/products/delete/<int:product_id>', methods=['POST'])
@login_required
@admin_required
def delete_product(product_id):
    try:
        product = Product.query.get_or_404(product_id)
        if product.public_id:
            cloudinary.uploader.destroy(product.public_id)
        db.session.delete(product)
        db.session.commit()
        flash('Product deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting product: {e}")
        flash('Error deleting product', 'error')
        
    return redirect(url_for('manage_products'))

@app.route('/sales', methods=['GET', 'POST'])
@login_required
def process_sales():
    if request.method == 'POST':
        try:
            product_id = request.form.get('product_id')
            barcode_input = request.form.get('barcode', '').strip()
            quantity = int(request.form.get('quantity', 1))

            product = None
            if barcode_input:
                product = Product.query.filter_by(barcode=barcode_input).first()
                if not product:
                    flash(f'No product found for barcode "{barcode_input}".', 'error')
            elif product_id:
                product = Product.query.get(int(product_id))
            else:
                flash('Select a product or scan a barcode.', 'error')

            if product:
                if product.quantity < quantity:
                    flash(f'Insufficient stock for {product.name}. Only {product.quantity} left.', 'error')
                else:
                    item = {
                        'id': product.id,
                        'name': product.name,
                        'price': product.price,
                        'quantity': quantity,
                        'subtotal': product.price * quantity,
                        'tax_type': product.tax_type or 'B',
                    }
                    active_transaction = session.get('active_transaction', [])
                    active_transaction.append(item)
                    session['active_transaction'] = active_transaction
                    flash(f'{item["name"]} added to cart.', 'success')
            elif not barcode_input and product_id:
                flash('Product not found.', 'error')
        except Exception as e:
            print(f"Error processing sale: {e}")
            flash('Error processing sale', 'error')

    try:
        total = sum(item['subtotal'] for item in session.get('active_transaction', []))
        all_products_list = Product.query.all()
        all_products_dict = {p.id: p for p in all_products_list}
    except Exception as e:
        print(f"Error loading sales data: {e}")
        total = 0
        all_products_dict = {}
        
    try:
        payment_options = [(c.key, i.payment_method_label or c.name) for c, i in payment_integrations()]
    except Exception as e:
        print(f"Error loading payment integrations: {e}")
        payment_options = []

    cart_customer = Customer.query.get(session['cart_customer_id']) if session.get('cart_customer_id') else None
    cart_document = Document.query.get(session['cart_document_id']) if session.get('cart_document_id') else None
    return render_template('sales.html', products=all_products_dict, transaction=session.get('active_transaction', []), total=total,
                           payment_options=payment_options, customers=Customer.query.order_by(Customer.name).all(),
                           cart_customer=cart_customer, cart_document=cart_document)

@app.route('/complete_sale', methods=['POST'])
@login_required
def complete_sale():
    if not session.get('active_transaction'):
        flash('Cannot complete an empty sale.', 'error')
        return redirect(url_for('process_sales'))
    
    try:
        active_transaction = session.get('active_transaction', [])
        subtotal = sum(item['subtotal'] for item in active_transaction)

        # --- Discount ---
        discount_type = request.form.get('discount_type', 'none')
        discount_value_str = request.form.get('discount_value', '').strip()
        discount_amount = 0.0
        discount_reason = request.form.get('discount_reason', '').strip() or None

        if discount_type in ('percent', 'fixed') and discount_value_str:
            try:
                discount_value = float(discount_value_str)
                if discount_value < 0:
                    raise ValueError
                if discount_type == 'percent':
                    discount_amount = subtotal * min(discount_value, 100) / 100
                else:
                    discount_amount = min(discount_value, subtotal)
            except ValueError:
                flash('Invalid discount value — sale completed without a discount.', 'error')
                discount_amount = 0.0

        total_amount = round(subtotal - discount_amount, 2)

        for item in active_transaction:
            product = Product.query.get(item['id'])
            if product:
                product.quantity -= item['quantity']
                db.session.add(product)
        
        items_str = json.dumps(active_transaction)

        payment_method = request.form.get('payment_method', 'cash').strip() or 'cash'
        payment_config = None
        if payment_method != 'cash':
            payment_config = next((c for c, _ in payment_integrations() if c.key == payment_method), None)
            if payment_config is None:
                db.session.rollback()
                flash('That payment method is not available. Pick another one.', 'error')
                return redirect(url_for('process_sales'))

        customer = find_or_create_customer(request.form)
        new_sale = Sale(
            total_amount=total_amount,
            items=items_str,
            user_id=current_user.id if current_user.is_authenticated else None,
            customer_name=request.form.get('customer_name', '').strip() or (customer.name if customer else None),
            customer_phone=request.form.get('customer_phone', '').strip() or (customer.phone if customer else None),
            customer_email=request.form.get('customer_email', '').strip() or (customer.email if customer else None),
            discount_amount=discount_amount,
            discount_reason=discount_reason if discount_amount > 0 else None
        )
        db.session.add(new_sale)
        db.session.flush()

        # Split payment: part cash now, the rest through the chosen integration
        cash_part = 0.0
        if payment_config is not None:
            cash_part = min(max(_parse_float(request.form.get('cash_part'), 0.0) or 0.0, 0.0), total_amount)
            if cash_part >= total_amount:
                payment_config, cash_part = None, 0.0  # all cash after all

        method_name = 'cash' if payment_config is None else payment_method_name(payment_config)
        shift = current_shift()
        db.session.add(SaleMeta(
            sale_id=new_sale.id,
            customer_pin=(request.form.get('customer_pin', '').strip().upper() or (customer.kra_pin if customer else None)),
            payment_method='split' if cash_part else method_name,
            customer_id=customer.id if customer else None,
            shift_id=shift.id if shift else None,
        ))

        cash_due = total_amount if payment_config is None else cash_part
        cash_message = None
        tendered = _parse_float(request.form.get('amount_tendered'))
        if cash_due and tendered:
            if tendered + 0.005 < cash_due:
                db.session.rollback()
                flash(f'Cash tendered ({tendered:.2f}) is less than the cash due ({cash_due:.2f}).', 'error')
                return redirect(url_for('process_sales'))
            cash_message = f'Tendered {tendered:.2f}, change {tendered - cash_due:.2f}'
        if cash_due:
            db.session.add(Payment(sale_id=new_sale.id, method='cash', amount=round(cash_due, 2), status='paid',
                                   message=cash_message))
        payment = None
        if payment_config is not None:
            payment = Payment(
                sale_id=new_sale.id,
                method=method_name,
                provider=payment_config.key,
                amount=round(total_amount - cash_part, 2),
                phone=(request.form.get('payment_phone', '').strip() or new_sale.customer_phone),
                status='pending',
            )
            db.session.add(payment)

        # A quotation/proforma sold over the counter
        cart_doc_id = session.pop('cart_document_id', None)
        session.pop('cart_customer_id', None)
        if cart_doc_id:
            doc = Document.query.get(cart_doc_id)
            if doc is not None and doc.doc_type in ('quotation', 'proforma'):
                doc.status = 'converted'
                doc.sale_id = new_sale.id
        db.session.commit()

        # Store the ID of the new sale in the session to easily redirect to its receipt
        session['last_sale_id'] = new_sale.id
        session.pop('active_transaction', None)

        if payment is None:
            emit_event(integrations.SALE_COMPLETED, sale=new_sale)
            flash('Sale completed successfully!' + (f' {cash_message}.' if cash_message else ''), 'success')
            return redirect(url_for('view_receipt', sale_id=new_sale.id))

        request_payment(payment)
        return redirect(url_for('payment_status_page', payment_id=payment.id))
    except Exception as e:
        db.session.rollback()
        print(f"Error completing sale: {e}")
        flash('Error completing sale', 'error')
        
    return redirect(url_for('process_sales'))

@app.route('/remove_item_from_sale/<int:item_id>', methods=['POST'])
@login_required
def remove_item_from_sale(item_id):
    active_transaction = session.get('active_transaction', [])
    updated_transaction = [item for item in active_transaction if item['id'] != item_id]
    
    if len(active_transaction) > len(updated_transaction):
        flash('Item removed from cart.', 'info')
        session['active_transaction'] = updated_transaction
    else:
        flash('Item not found in cart.', 'error')
    
    return redirect(url_for('process_sales'))

@app.route('/cancel_sale', methods=['POST'])
@login_required
def cancel_sale():
    session['active_transaction'] = []
    session.pop('cart_document_id', None)
    session.pop('cart_customer_id', None)
    flash('Sale cancelled.', 'info')
    return redirect(url_for('process_sales'))

@app.route('/history')
@login_required
def sales_history():
    try:
        completed_sales = []
        for sale in Sale.query.order_by(Sale.timestamp.desc()).all():
            try:
                items_data = json.loads(sale.items)
                item_count = sum(item.get('quantity', 0) for item in items_data)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"Error processing sales history item {sale.id}: {e}")
                item_count = 0
                
            completed_sales.append({
                'id': sale.id,
                'date': sale.timestamp.strftime('%Y-%m-%d %H:%M'),
                'total': sale.total_amount,
                'item_count': item_count,
                'clerk': sale.clerk.username if sale.clerk else 'N/A',
                'refunded': sale.refunded
            })

        ongoing_transactions = [{
            'id': 'current',
            'item_count': sum(item['quantity'] for item in session.get('active_transaction', [])),
            'total': sum(item['subtotal'] for item in session.get('active_transaction', []))
        }]

        remaining_products = [
            {'name': p.name, 'quantity': p.quantity}
            for p in Product.query.order_by(Product.name).all()
        ]

        purchase_counts = defaultdict(int)
        for sale in Sale.query.all():
            try:
                items_data = json.loads(sale.items)
                for item in items_data:
                    purchase_counts[item.get('name', '')] += item.get('quantity', 0)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                print(f"Error calculating purchase counts for sale {sale.id}: {e}")
                continue
            
        most_purchased_products = sorted(
            [{'name': name, 'count': count} for name, count in purchase_counts.items() if name],
            key=lambda x: x['count'],
            reverse=True
        )
        
    except Exception as e:
        print(f"Error loading sales history: {e}")
        completed_sales = []
        ongoing_transactions = []
        remaining_products = []
        most_purchased_products = []
        flash('Error loading sales history', 'error')
    
    return render_template(
        'sales_history.html',
        completed_sales=completed_sales,
        ongoing_transactions=ongoing_transactions,
        remaining_products=remaining_products,
        most_purchased_products=most_purchased_products
    )

@app.route('/view_receipt/<int:sale_id>')
@login_required
def view_receipt(sale_id):
    try:
        sale = Sale.query.get_or_404(sale_id)
        
        # Parse the items JSON
        items = json.loads(sale.items)
        
        # Get KRA-related data from the Sale model
        kra_invoice_id = sale.kra_invoice_id
        qr_code_data = sale.kra_qr_code_data
        
        # Generate the QR code as a base64 string
        qr_code_b64 = generate_qr_code_b64(qr_code_data)

        # Business settings + M-Pesa payment QR code
        settings = BusinessSettings.get()
        settings.refresh_secondary_rate_if_stale(hours=1)
        payment_qr_b64 = generate_qr_code_b64(settings.payment_qr_text) if settings.payment_qr_text else None

        # Get clerk username: prefer the user who made this sale, fall back to viewer
        clerk_username = sale.clerk.username if sale.clerk else (current_user.username if current_user.is_authenticated else "N/A")

        secondary_amount = settings.convert_to_secondary(sale.total_amount)
        
        return render_template(
            'receipt.html',
            sale=sale,
            items=items,
            kra_invoice_id=kra_invoice_id,
            qr_code_b64=qr_code_b64,
            clerk_username=clerk_username,
            settings=settings,
            payment_qr_b64=payment_qr_b64,
            secondary_amount=secondary_amount,
            payments=[p for p in sale.payments if p.status in ('paid', 'pending')],
            meta=sale.meta
        )
    except Exception as e:
        print(f"Error viewing receipt: {e}")
        flash('Error loading receipt.', 'error')
        return redirect(url_for('sales_history'))

@app.route('/receipt/<int:sale_id>/refund', methods=['POST'])
@login_required
@admin_required
def refund_sale(sale_id):
    sale = Sale.query.get_or_404(sale_id)

    if sale.refunded:
        flash('This sale has already been refunded.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale_id))

    if sale.meta and sale.meta.document_id:
        doc = Document.query.get(sale.meta.document_id)
        flash(f'This sale belongs to invoice {doc.number if doc else ""}. Void the invoice instead.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id) if doc else url_for('view_receipt', sale_id=sale_id))

    reason = request.form.get('refund_reason', '').strip() or None

    try:
        # Restock every item from the sale back into inventory, with an audit trail.
        restock_sale_items(sale, f"Refund of Sale #{sale.id}" + (f" ({reason})" if reason else ""), current_user.username)

        sale.refunded = True
        sale.refunded_at = datetime.datetime.utcnow()
        sale.refunded_by = current_user.username
        sale.refund_reason = reason

        db.session.commit()
        emit_event(integrations.SALE_REFUNDED, sale=sale)
        flash(f'Sale #{sale.id} refunded and items restocked.', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Error refunding sale: {e}")
        flash('Error processing refund.', 'error')

    return redirect(url_for('view_receipt', sale_id=sale_id))

@app.route('/receipt/<int:sale_id>/email', methods=['POST'])
@login_required
def email_receipt(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    recipient = request.form.get('email', '').strip() or sale.customer_email

    if not recipient:
        flash('Enter an email address to send the receipt to.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale_id))

    mail_server = os.environ.get('MAIL_SERVER')
    if not mail_server:
        flash('Email is not configured yet. Set MAIL_SERVER, MAIL_USERNAME, MAIL_PASSWORD, and MAIL_DEFAULT_SENDER in your environment to enable this.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale_id))

    try:
        from flask_mail import Mail, Message

        app.config.setdefault('MAIL_SERVER', mail_server)
        app.config.setdefault('MAIL_PORT', int(os.environ.get('MAIL_PORT', 587)))
        app.config.setdefault('MAIL_USE_TLS', os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true')
        app.config.setdefault('MAIL_USERNAME', os.environ.get('MAIL_USERNAME'))
        app.config.setdefault('MAIL_PASSWORD', os.environ.get('MAIL_PASSWORD'))
        app.config.setdefault('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_DEFAULT_SENDER'))

        mail = Mail(app)
        settings = BusinessSettings.get()
        items_data = json.loads(sale.items)

        lines = [f"Receipt from {settings.business_name or 'our store'}", ""]
        for item in items_data:
            lines.append(f"{item.get('name')} x{item.get('quantity')} - {settings.currency_symbol} {item.get('subtotal', 0):.2f}")
        if sale.discount_amount:
            lines.append(f"Discount: -{settings.currency_symbol} {sale.discount_amount:.2f}")
        lines.append(f"Total: {settings.currency_symbol} {sale.total_amount:.2f}")
        lines.append(f"Receipt #: {sale.id}")
        lines.append(f"KRA Invoice ID: {sale.kra_invoice_id}")

        msg = Message(
            subject=f"Your Receipt #{sale.id} from {settings.business_name or 'our store'}",
            recipients=[recipient],
            body="\n".join(lines)
        )
        mail.send(msg)
        flash(f'Receipt emailed to {recipient}.', 'success')
    except Exception as e:
        print(f"Error emailing receipt: {e}")
        flash('Could not send the email. Check your MAIL_* environment variables.', 'error')

    return redirect(url_for('view_receipt', sale_id=sale_id))

@app.route('/receipt/<int:sale_id>/sms', methods=['POST'])
@login_required
def sms_receipt(sale_id):
    sale = Sale.query.get_or_404(sale_id)
    recipient = request.form.get('phone', '').strip() or sale.customer_phone

    if not recipient:
        flash('Enter a phone number to text the receipt to.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale_id))

    at_username = os.environ.get('AFRICASTALKING_USERNAME')
    at_api_key = os.environ.get('AFRICASTALKING_API_KEY')
    if not at_username or not at_api_key:
        flash('SMS is not configured yet. Set AFRICASTALKING_USERNAME and AFRICASTALKING_API_KEY in your environment to enable this.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale_id))

    try:
        import africastalking

        africastalking.initialize(at_username, at_api_key)
        sms = africastalking.SMS
        settings = BusinessSettings.get()

        message = (
            f"{settings.business_name or 'Receipt'}: Total {settings.currency_symbol} "
            f"{sale.total_amount:.2f}. Invoice {sale.kra_invoice_id}. Thank you!"
        )
        sender_id = os.environ.get('AFRICASTALKING_SENDER_ID')
        if sender_id:
            sms.send(message, [recipient], sender_id=sender_id)
        else:
            sms.send(message, [recipient])

        flash(f'Receipt texted to {recipient}.', 'success')
    except Exception as e:
        print(f"Error sending SMS receipt: {e}")
        flash('Could not send the SMS. Check your AFRICASTALKING_* environment variables and account balance.', 'error')

    return redirect(url_for('view_receipt', sale_id=sale_id))


@app.route('/users', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_users():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'cashier')

        if role not in ('admin', 'cashier'):
            role = 'cashier'

        if not username or not password:
            flash('Username and password are required.', 'error')
            return redirect(url_for('manage_users'))

        if User.query.filter_by(username=username).first():
            flash('That username is already taken.', 'error')
            return redirect(url_for('manage_users'))

        try:
            new_user = User(username=username, role=role)
            new_user.password = password
            db.session.add(new_user)
            db.session.commit()
            flash(f'User "{username}" created as {role}.', 'success')
        except Exception as e:
            db.session.rollback()
            print(f"Error creating user: {e}")
            flash('Error creating user', 'error')

        return redirect(url_for('manage_users'))

    try:
        all_users = User.query.order_by(User.username).all()
    except Exception as e:
        print(f"Error fetching users: {e}")
        all_users = []
        flash('Error loading users', 'error')

    return render_template('users.html', users=all_users)

@app.route('/users/delete/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    if user_id == current_user.id:
        flash("You can't delete your own account while logged in.", 'error')
        return redirect(url_for('manage_users'))

    try:
        user = User.query.get_or_404(user_id)
        if user.is_admin and User.query.filter_by(role='admin').count() <= 1:
            flash('Cannot delete the last remaining admin.', 'error')
            return redirect(url_for('manage_users'))

        db.session.delete(user)
        db.session.commit()
        flash('User deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting user: {e}")
        flash('Error deleting user', 'error')

    return redirect(url_for('manage_users'))

@app.route('/products/<int:product_id>/barcode.png')
@login_required
@admin_required
def product_barcode_image(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.barcode:
        flash('This product has no barcode set. Add one first via Edit.', 'error')
        return redirect(url_for('manage_products'))

    try:
        code_class = barcode_lib.get_barcode_class('code128')
        buffer = BytesIO()
        code = code_class(product.barcode, writer=ImageWriter())
        code.write(buffer, options={'write_text': True, 'module_height': 10})
        buffer.seek(0)
        return send_file(buffer, mimetype='image/png')
    except Exception as e:
        print(f"Error generating barcode image: {e}")
        flash('Error generating barcode label.', 'error')
        return redirect(url_for('manage_products'))

def _parse_report_range():
    """Parse ?start=YYYY-MM-DD&end=YYYY-MM-DD query params, defaulting to today."""
    today = datetime.date.today()
    start_str = request.args.get('start', today.isoformat())
    end_str = request.args.get('end', today.isoformat())
    try:
        start_date = datetime.datetime.strptime(start_str, '%Y-%m-%d').date()
        end_date = datetime.datetime.strptime(end_str, '%Y-%m-%d').date()
    except ValueError:
        start_date = end_date = today
        start_str = end_str = today.isoformat()

    if end_date < start_date:
        start_date, end_date = end_date, start_date
        start_str, end_str = end_str, start_str

    start_dt = datetime.datetime.combine(start_date, datetime.time.min)
    end_dt = datetime.datetime.combine(end_date, datetime.time.max)
    return start_str, end_str, start_dt, end_dt

@app.route('/reports')
@login_required
def sales_reports():
    start_str, end_str, start_dt, end_dt = _parse_report_range()

    try:
        sales_in_range = Sale.query.filter(
            Sale.timestamp >= start_dt, Sale.timestamp <= end_dt
        ).order_by(Sale.timestamp).all()

        total_revenue = sum(s.total_amount for s in sales_in_range)
        total_transactions = len(sales_in_range)
        total_items = 0
        product_breakdown = defaultdict(lambda: {'quantity': 0, 'revenue': 0.0})
        clerk_breakdown = defaultdict(lambda: {'transactions': 0, 'revenue': 0.0})

        for sale in sales_in_range:
            clerk_name = sale.clerk.username if sale.clerk else 'N/A'
            clerk_breakdown[clerk_name]['transactions'] += 1
            clerk_breakdown[clerk_name]['revenue'] += sale.total_amount

            try:
                items_data = json.loads(sale.items)
            except (json.JSONDecodeError, TypeError):
                continue
            for item in items_data:
                qty = item.get('quantity', 0)
                total_items += qty
                name = item.get('name', 'Unknown')
                product_breakdown[name]['quantity'] += qty
                product_breakdown[name]['revenue'] += item.get('subtotal', 0)

        breakdown_list = sorted(
            [{'name': name, **data} for name, data in product_breakdown.items()],
            key=lambda x: x['revenue'],
            reverse=True
        )
        clerk_list = sorted(
            [{'name': name, **data} for name, data in clerk_breakdown.items()],
            key=lambda x: x['revenue'],
            reverse=True
        )
    except Exception as e:
        print(f"Error generating report: {e}")
        flash('Error generating report', 'error')
        total_revenue, total_transactions, total_items = 0, 0, 0
        breakdown_list, clerk_list = [], []

    return render_template(
        'reports.html',
        start_date=start_str,
        end_date=end_str,
        total_revenue=total_revenue,
        total_transactions=total_transactions,
        total_items=total_items,
        breakdown_list=breakdown_list,
        clerk_list=clerk_list
    )

@app.route('/reports/export.csv')
@login_required
def export_report_csv():
    start_str, end_str, start_dt, end_dt = _parse_report_range()

    try:
        sales_in_range = Sale.query.filter(
            Sale.timestamp >= start_dt, Sale.timestamp <= end_dt
        ).order_by(Sale.timestamp).all()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Sale ID', 'Date/Time', 'Clerk', 'Total (KSh)', 'KRA Invoice ID', 'Items'])
        for sale in sales_in_range:
            try:
                items_data = json.loads(sale.items)
                items_summary = "; ".join(f"{i.get('name')} x{i.get('quantity')}" for i in items_data)
            except (json.JSONDecodeError, TypeError):
                items_summary = ''
            writer.writerow([
                sale.id,
                sale.timestamp.strftime('%Y-%m-%d %H:%M'),
                sale.clerk.username if sale.clerk else 'N/A',
                f"{sale.total_amount:.2f}",
                sale.kra_invoice_id or '',
                items_summary
            ])

        response = Response(output.getvalue(), mimetype='text/csv')
        response.headers['Content-Disposition'] = f'attachment; filename=sales_report_{start_str}_to_{end_str}.csv'
        return response
    except Exception as e:
        print(f"Error exporting report: {e}")
        flash('Error exporting report', 'error')
        return redirect(url_for('sales_reports'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def business_settings():
    settings = BusinessSettings.get()

    if request.method == 'POST':
        settings.business_name = request.form.get('business_name', '').strip() or None
        settings.address = request.form.get('address', '').strip() or None
        settings.phone = request.form.get('phone', '').strip() or None
        settings.kra_pin = request.form.get('kra_pin', '').strip().upper() or None
        settings.email = request.form.get('email', '').strip() or None
        settings.bank_details = request.form.get('bank_details', '').strip() or None
        settings.invoice_terms = request.form.get('invoice_terms', '').strip() or None
        for field in ('quotation_valid_days', 'invoice_due_days'):
            value = request.form.get(field, '').strip()
            setattr(settings, field, int(value) if value.isdigit() else None)
        settings.mpesa_till_number = request.form.get('mpesa_till_number', '').strip() or None
        settings.mpesa_paybill_number = request.form.get('mpesa_paybill_number', '').strip() or None
        settings.mpesa_paybill_account = request.form.get('mpesa_paybill_account', '').strip() or None

        settings.currency_code = request.form.get('currency_code', '').strip() or 'KES'
        settings.currency_symbol = request.form.get('currency_symbol', '').strip() or 'KSh'
        settings.secondary_currency_code = request.form.get('secondary_currency_code', '').strip() or None
        settings.secondary_currency_symbol = request.form.get('secondary_currency_symbol', '').strip() or None

        auto_mode = request.form.get('secondary_rate_auto') == 'on'
        settings.secondary_rate_auto = auto_mode

        if not auto_mode:
            rate_str = request.form.get('secondary_exchange_rate', '').strip()
            if rate_str:
                try:
                    settings.secondary_exchange_rate = float(rate_str)
                except ValueError:
                    flash('Exchange rate must be a number — left unchanged.', 'error')
            else:
                settings.secondary_exchange_rate = None
        # In auto mode, leave secondary_exchange_rate as-is; it's populated by
        # fetch_exchange_rate() or the silent refresh on receipt view.

        try:
            db.session.commit()
            flash('Business settings updated.', 'success')
        except Exception as e:
            db.session.rollback()
            print(f"Error updating settings: {e}")
            flash('Error updating settings', 'error')
        return redirect(url_for('business_settings'))

    return render_template('settings.html', settings=settings)

@app.route('/settings/fetch-rate', methods=['POST'])
@login_required
@admin_required
def fetch_exchange_rate():
    settings = BusinessSettings.get()
    rate, error = settings.fetch_live_secondary_rate()

    if rate is not None:
        settings.secondary_exchange_rate = rate
        settings.secondary_rate_updated_at = datetime.datetime.utcnow()
        try:
            db.session.commit()
            flash(f'Live rate fetched: 1 {settings.secondary_currency_code} = {settings.currency_symbol} {rate:.4f}', 'success')
        except Exception as e:
            db.session.rollback()
            print(f"Error saving fetched exchange rate: {e}")
            flash('Fetched the rate but could not save it — try again.', 'error')
    else:
        flash(f'Could not fetch a live rate: {error}', 'error')

    return redirect(url_for('business_settings'))

# --- Payments (checkout with an integration such as M-Pesa) ---
@app.route('/payments/<int:payment_id>')
@login_required
def payment_status_page(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    if payment.status == 'paid':
        return redirect(url_for('view_receipt', sale_id=payment.sale_id))
    config, instance = get_integration(payment.provider) if payment.provider else (None, None)
    return render_template('payment_status.html', payment=payment, config=config,
                           is_mock=bool(instance and instance.is_mock))


@app.route('/payments/<int:payment_id>/status')
@login_required
def payment_status_json(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    # If the callback is slow, ask the provider directly (at most every ~15 seconds per payment).
    if payment.status == 'pending' and payment.provider and payment.provider_ref:
        age = (datetime.datetime.utcnow() - (payment.updated_at or payment.created_at)).total_seconds()
        if age > 15:
            config, instance = get_integration(payment.provider)
            if instance is not None and not instance.is_mock:
                try:
                    result = instance.query_payment(payment.provider_ref)
                except NotImplementedError:
                    result = None
                if result is not None and result.ok and result.data.get('status') in ('paid', 'failed'):
                    finalize_payment(payment, result.data['status'], message=result.data.get('message'))
                else:
                    payment.updated_at = datetime.datetime.utcnow()
                    db.session.commit()
    return {
        'status': payment.status,
        'message': payment.message,
        'receipt_number': payment.receipt_number,
        'receipt_url': url_for('view_receipt', sale_id=payment.sale_id),
    }


@app.route('/payments/<int:payment_id>/retry', methods=['POST'])
@login_required
def retry_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    if payment.status == 'paid':
        return redirect(url_for('view_receipt', sale_id=payment.sale_id))
    phone = request.form.get('phone', '').strip()
    if phone:
        payment.phone = phone
    payment.provider_ref = None
    payment.status = 'pending'
    db.session.commit()
    result = request_payment(payment)
    if result is not None and result.ok:
        flash('Payment request sent again. Ask the customer to check their phone.', 'success')
    else:
        flash(f'Could not send the payment request: {payment.message}', 'error')
    return redirect(url_for('payment_status_page', payment_id=payment.id))


@app.route('/payments/<int:payment_id>/cash', methods=['POST'])
@login_required
def pay_with_cash_instead(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    if payment.status != 'paid':
        payment.method = 'cash'
        payment.provider = None
        if payment.sale.meta:
            payment.sale.meta.payment_method = 'cash'
        db.session.commit()
        finalize_payment(payment, 'paid', message=f'Marked as paid in cash by {current_user.username}')
        flash('Sale completed as a cash payment.', 'success')
    return redirect(url_for('view_receipt', sale_id=payment.sale_id))


@app.route('/payments/<int:payment_id>/cancel', methods=['POST'])
@login_required
def cancel_payment(payment_id):
    payment = Payment.query.get_or_404(payment_id)
    sale = payment.sale
    if payment.status == 'paid':
        flash('This sale is already paid. Use a refund instead.', 'error')
        return redirect(url_for('view_receipt', sale_id=sale.id))
    payment.status = 'cancelled'
    if not sale.refunded:
        restock_sale_items(sale, f"Cancelled unpaid Sale #{sale.id}", current_user.username)
        sale.refunded = True
        sale.refunded_at = datetime.datetime.utcnow()
        sale.refunded_by = current_user.username
        sale.refund_reason = 'Payment cancelled before it was received'
    db.session.commit()
    flash(f'Sale #{sale.id} cancelled and items restocked.', 'info')
    return redirect(url_for('process_sales'))


@app.route('/payments/<int:payment_id>/simulate', methods=['POST'])
@login_required
def simulate_payment(payment_id):
    """Mock mode only: pretend the provider called back, so the flow can be tried without credentials."""
    payment = Payment.query.get_or_404(payment_id)
    config, instance = get_integration(payment.provider) if payment.provider else (None, None)
    if instance is None or not instance.is_mock:
        flash('Simulation is only available while the integration is in mock mode.', 'error')
        return redirect(url_for('payment_status_page', payment_id=payment.id))
    outcome = request.form.get('outcome', 'paid')
    if outcome == 'paid':
        finalize_payment(payment, 'paid', receipt_number=f"MOCK{payment.id:06d}", message='Simulated payment')
        flash('Simulated a successful payment.', 'success')
        return redirect(url_for('view_receipt', sale_id=payment.sale_id))
    finalize_payment(payment, 'failed', message='Simulated: customer cancelled the request')
    return redirect(url_for('payment_status_page', payment_id=payment.id))


# --- Provider callbacks (called by external APIs, so no login and no CSRF) ---
@app.route('/integrations/<key>/callback', methods=['POST'])
@csrf.exempt
def integration_callback(key):
    config, instance = get_integration(key)
    if instance is None or not config.enabled:
        return {'ResultCode': 1, 'ResultDesc': 'Unknown integration'}, 404

    expected = instance.setting('callback_token') if any(f.name == 'callback_token' for f in instance.fields) else None
    if expected and not hmac.compare_digest(request.args.get('token', ''), expected):
        return {'ResultCode': 1, 'ResultDesc': 'Invalid token'}, 403

    body = request.get_json(silent=True) or {}
    db.session.add(IntegrationLog(integration_key=key, event='callback', status='success',
                                  payload_json=_to_json(body)))
    db.session.commit()
    try:
        cb = instance.parse_callback(body, request.args)
    except NotImplementedError:
        return {'ResultCode': 1, 'ResultDesc': 'This integration does not accept callbacks'}, 400

    payment = None
    if cb.provider_ref:
        payment = Payment.query.filter_by(provider=key, provider_ref=cb.provider_ref).first()
    if payment is None and cb.reference:
        # C2B: the customer typed e.g. "SALE42" as the account number
        ref = cb.reference.upper()
        prefix = (instance.setting('account_reference_prefix') or 'SALE').upper()
        if ref.startswith(prefix) and ref[len(prefix):].isdigit():
            payment = (Payment.query.filter_by(sale_id=int(ref[len(prefix):]))
                       .filter(Payment.status.in_(('pending', 'failed'))).order_by(Payment.id.desc()).first())
            if payment is not None:
                if cb.amount is not None and float(cb.amount) + 0.01 < payment.amount:
                    payment.message = f'Received {cb.amount} by C2B but {payment.amount:.2f} is due'
                    db.session.commit()
                    return cb.ack
                payment.provider = key
                payment.method = getattr(instance, 'payment_method_code', None) or payment.method
                payment.provider_ref = cb.receipt_number

    if payment is None:
        print(f"Callback for {key} did not match any payment: {body}")
        return cb.ack

    if cb.phone and not payment.phone:
        payment.phone = cb.phone
    if cb.status in ('paid', 'failed'):
        finalize_payment(payment, cb.status, receipt_number=cb.receipt_number, message=cb.message, raw=body)
    return cb.ack


@app.route('/integrations/<key>/c2b/validation', methods=['POST'])
@csrf.exempt
def integration_c2b_validation(key):
    # Accept every payment; matching to sales happens on confirmation.
    return {'ResultCode': 0, 'ResultDesc': 'Accepted'}


# --- Integrations admin ---
@app.route('/integrations')
@login_required
@admin_required
def list_integrations():
    configs = IntegrationConfig.query.order_by(IntegrationConfig.id).all()
    rows = []
    for config in configs:
        instance = config.instance()
        rows.append({
            'config': config,
            'adapter': config.adapter_class,
            'mode': instance.mode if instance else None,
            'missing': instance.missing_settings() if instance else [],
            'events': sorted(instance.subscribed_events) if instance is not None and hasattr(instance, 'subscribed_events')
                      else list(config.adapter_class.events) if config.adapter_class else [],
        })
    configured = {c.adapter for c in configs}
    addable = [cls for cls in integrations.adapters().values()
               if cls.allow_multiple or cls.adapter not in configured]
    logs = IntegrationLog.query.order_by(IntegrationLog.id.desc()).limit(50).all()
    return render_template('integrations.html', rows=rows, addable=addable, logs=logs)


@app.route('/integrations/new', methods=['POST'])
@login_required
@admin_required
def add_integration():
    adapter_id = request.form.get('adapter', '')
    cls = integrations.get_adapter(adapter_id)
    if cls is None:
        flash('Unknown integration type.', 'error')
        return redirect(url_for('list_integrations'))
    if not cls.allow_multiple and IntegrationConfig.query.filter_by(adapter=adapter_id).first():
        flash(f'{cls.name} is already set up.', 'error')
        return redirect(url_for('list_integrations'))
    name = request.form.get('name', '').strip() or cls.name
    base_key = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_') or adapter_id
    key, n = base_key, 2
    while IntegrationConfig.query.filter_by(key=key).first():
        key, n = f'{base_key}_{n}', n + 1
    config = IntegrationConfig(key=key, adapter=adapter_id, name=name, enabled=False,
                               settings={f.name: f.default for f in cls.fields if not f.secret})
    db.session.add(config)
    db.session.commit()
    flash(f'{name} added. Fill in its settings, then enable it.', 'success')
    return redirect(url_for('edit_integration', key=key))


@app.route('/integrations/<key>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_integration(key):
    config = IntegrationConfig.query.filter_by(key=key).first_or_404()
    cls = config.adapter_class
    if cls is None:
        flash(f'The adapter "{config.adapter}" is not installed.', 'error')
        return redirect(url_for('list_integrations'))

    if request.method == 'POST':
        settings = config.settings
        for f in cls.fields:
            if f.secret:
                continue  # secrets only ever come from environment variables
            if f.type == 'bool':
                settings[f.name] = bool(request.form.get(f.name))
            else:
                settings[f.name] = request.form.get(f.name, '').strip()
        config.settings = settings
        config.name = request.form.get('display_name', '').strip() or config.name
        config.enabled = bool(request.form.get('enabled'))
        db.session.commit()
        flash(f'{config.name} saved.', 'success')
        return redirect(url_for('edit_integration', key=key))

    instance = config.instance()
    fields = cls.describe_fields()
    for f in fields:
        f['value'] = '' if f['secret'] else config.settings.get(f['name'], f['default'])
    callback_url = None
    if cls.category == 'payments':
        callback_url = integration_callback_url(config, instance)
    logs = IntegrationLog.query.filter_by(integration_key=key).order_by(IntegrationLog.id.desc()).limit(25).all()
    return render_template('integration_edit.html', config=config, adapter=cls, instance=instance,
                           fields=fields, callback_url=callback_url, logs=logs,
                           actions=integration_actions(instance))


def integration_actions(instance):
    """Extra one-off setup buttons an adapter offers."""
    actions = []
    if hasattr(instance, 'initialize_device'):
        actions.append(('initialize_device', 'Initialise eTIMS device (get CMC key)'))
    if hasattr(instance, 'register_c2b_urls'):
        actions.append(('register_c2b_urls', 'Register C2B URLs with Safaricom'))
    return actions


@app.route('/integrations/<key>/test', methods=['POST'])
@login_required
@admin_required
def test_integration(key):
    config, instance = get_integration(key)
    if instance is None:
        abort(404)
    try:
        result = instance.test_connection()
    except Exception as e:
        result = integrations.Result.failure(str(e))
    db.session.add(IntegrationLog(integration_key=key, event='test', status='success' if result.ok else 'failed',
                                  request_json=_to_json(result.request), response_json=_to_json(result.response or result.data),
                                  error=result.error))
    db.session.commit()
    if result.ok:
        flash(f"{config.name}: {result.data.get('message', 'OK')}", 'success')
    else:
        flash(f'{config.name} test failed: {result.error}', 'error')
    return redirect(url_for('edit_integration', key=key))


@app.route('/integrations/<key>/action/<action>', methods=['POST'])
@login_required
@admin_required
def run_integration_action(key, action):
    config, instance = get_integration(key)
    if instance is None or action not in dict(integration_actions(instance)):
        abort(404)
    if action == 'initialize_device':
        result = instance.initialize_device()
        if result.ok:
            flash(f"Device initialised. Save this CMC key in the ETIMS_CMC_KEY environment variable: {result.data.get('cmc_key')}", 'success')
    else:
        callback = integration_callback_url(config, instance)
        base = (os.environ.get('PUBLIC_BASE_URL') or request.url_root).rstrip('/')
        result = instance.register_c2b_urls(callback, f"{base}{url_for('integration_c2b_validation', key=key)}")
        if result.ok:
            flash(result.data.get('message', 'Done'), 'success')
    if not result.ok:
        flash(f'{config.name}: {result.error}', 'error')
    db.session.add(IntegrationLog(integration_key=key, event=action, status='success' if result.ok else 'failed',
                                  request_json=_to_json(result.request), response_json=_to_json(result.response),
                                  error=result.error))
    db.session.commit()
    return redirect(url_for('edit_integration', key=key))


@app.route('/integrations/<key>/delete', methods=['POST'])
@login_required
@admin_required
def delete_integration(key):
    config = IntegrationConfig.query.filter_by(key=key).first_or_404()
    db.session.delete(config)
    db.session.commit()
    flash(f'{config.name} removed.', 'info')
    return redirect(url_for('list_integrations'))


@app.route('/integrations/logs/<int:log_id>/retry', methods=['POST'])
@login_required
@admin_required
def retry_integration_log(log_id):
    log = IntegrationLog.query.get_or_404(log_id)
    config, instance = get_integration(log.integration_key)
    if instance is None or not log.payload_json or log.event not in integrations.ALL_EVENTS:
        flash('This entry cannot be retried.', 'error')
        return redirect(request.referrer or url_for('list_integrations'))
    payload = json.loads(log.payload_json)
    sale = Sale.query.get(log.sale_id) if log.sale_id else None
    if sale is not None:
        payload['sale'] = sale_payload(sale)  # use fresh data, e.g. a customer PIN added later
    result = run_integration(config, instance, log.event, payload, sale=sale, log=log)
    db.session.commit()
    flash(f'Retried: {"success" if result.ok else result.error}', 'success' if result.ok else 'error')
    return redirect(request.referrer or url_for('list_integrations'))

# =====================================================================
# Customers
# =====================================================================
def _parse_float(value, default=None):
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return default


def _parse_date(value, default=None):
    try:
        return datetime.datetime.strptime((value or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return default


def find_or_create_customer(form):
    """Pick the customer chosen at checkout, or save a new one when a name and phone are given."""
    customer_id = (form.get('customer_id') or '').strip()
    if customer_id.isdigit():
        return Customer.query.get(int(customer_id))
    name = (form.get('customer_name') or '').strip()
    phone = (form.get('customer_phone') or '').strip()
    if not phone:
        return None
    customer = Customer.query.filter_by(phone=phone).first()
    if customer is None and name:
        customer = Customer(name=name, phone=phone,
                            email=(form.get('customer_email') or '').strip() or None,
                            kra_pin=(form.get('customer_pin') or '').strip().upper() or None)
        db.session.add(customer)
        db.session.flush()
    return customer


def customer_balance(customer):
    return round(sum(d.balance for d in customer.documents if d.doc_type == 'invoice' and d.status in ('unpaid', 'partial')), 2)


@app.route('/customers', methods=['GET', 'POST'])
@login_required
def list_customers():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Customer name is required.', 'error')
        else:
            customer = Customer(name=name)
            _fill_contact(customer, request.form)
            customer.credit_limit = _parse_float(request.form.get('credit_limit'))
            db.session.add(customer)
            db.session.commit()
            flash(f'Customer {name} added.', 'success')
            return redirect(url_for('view_customer', customer_id=customer.id))
    q = request.args.get('q', '').strip()
    query = Customer.query
    if q:
        like = f'%{q}%'
        query = query.filter(db.or_(Customer.name.ilike(like), Customer.phone.ilike(like), Customer.kra_pin.ilike(like)))
    customers = query.order_by(Customer.name).all()
    return render_template('customers.html', customers=customers, q=q, balance=customer_balance)


def _fill_contact(obj, form):
    obj.name = (form.get('name') or obj.name or '').strip()
    obj.phone = (form.get('phone') or '').strip() or None
    obj.email = (form.get('email') or '').strip() or None
    obj.kra_pin = (form.get('kra_pin') or '').strip().upper() or None
    obj.address = (form.get('address') or '').strip() or None
    obj.notes = (form.get('notes') or '').strip() or None


@app.route('/customers/<int:customer_id>', methods=['GET', 'POST'])
@login_required
def view_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if request.method == 'POST':
        _fill_contact(customer, request.form)
        customer.credit_limit = _parse_float(request.form.get('credit_limit'))
        db.session.commit()
        flash('Customer updated.', 'success')
        return redirect(url_for('view_customer', customer_id=customer.id))
    sales = (Sale.query.join(SaleMeta, SaleMeta.sale_id == Sale.id)
             .filter(SaleMeta.customer_id == customer.id).order_by(Sale.timestamp.desc()).limit(50).all())
    documents = Document.query.filter_by(customer_id=customer.id).order_by(Document.id.desc()).all()
    total_spent = sum(s.total_amount for s in sales if not s.refunded)
    return render_template('customer_view.html', customer=customer, sales=sales, documents=documents,
                           total_spent=total_spent, balance=customer_balance(customer))


def aging_buckets(invoices, today=None):
    today = today or datetime.date.today()
    buckets = {'current': 0.0, '1-30': 0.0, '31-60': 0.0, '61-90': 0.0, '90+': 0.0}
    for inv in invoices:
        if inv.status not in ('unpaid', 'partial') or inv.balance <= 0:
            continue
        days = (today - (inv.due_date or inv.issue_date)).days
        key = 'current' if days <= 0 else '1-30' if days <= 30 else '31-60' if days <= 60 else '61-90' if days <= 90 else '90+'
        buckets[key] += inv.balance
    return {k: round(v, 2) for k, v in buckets.items()}


@app.route('/customers/<int:customer_id>/statement')
@login_required
def customer_statement(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    invoices = [d for d in customer.documents if d.doc_type == 'invoice' and d.status not in ('draft', 'void')]
    entries = []
    for inv in invoices:
        entries.append({'date': inv.issue_date, 'ref': inv.number, 'description': f'Invoice (due {inv.due_date or "-"})',
                        'debit': inv.total, 'credit': 0.0, 'doc': inv})
        for pay in inv.payments:
            entries.append({'date': pay.paid_on, 'ref': inv.number, 'description': f'Payment ({pay.method}{" " + pay.reference if pay.reference else ""})',
                            'debit': 0.0, 'credit': pay.amount, 'doc': inv})
    entries.sort(key=lambda e: (e['date'], e['credit'] > 0))
    running = 0.0
    for e in entries:
        running += e['debit'] - e['credit']
        e['balance'] = round(running, 2)
    return render_template('statement.html', customer=customer, entries=entries, balance=round(running, 2),
                           aging=aging_buckets(invoices), settings=BusinessSettings.get(), today=datetime.date.today())


# =====================================================================
# Shifts & cash-up
# =====================================================================
def current_shift(user=None):
    user = user or current_user
    if not getattr(user, 'is_authenticated', False):
        return None
    return Shift.query.filter_by(user_id=user.id, closed_at=None).order_by(Shift.id.desc()).first()


@app.context_processor
def inject_shift():
    try:
        return {'open_shift': current_shift()}
    except Exception:
        return {'open_shift': None}


def cash_summary(sales, document_payments, opening_float=0.0):
    """Totals for a Z report over a set of sales and invoice payments."""
    by_method = defaultdict(float)
    gross = discounts = refunds = cash_refunds = 0.0
    count = 0
    for sale in sales:
        meta = sale.meta
        is_invoice = meta is not None and meta.document_id
        if is_invoice:
            continue  # credit invoices are counted when they're paid
        count += 1
        gross += sale.subtotal
        discounts += sale.discount_amount or 0
        paid = [p for p in sale.payments if p.status == 'paid']
        for p in paid:
            by_method[p.method] += p.amount
        if sale.refunded:
            refunds += sum(p.amount for p in paid)
            cash_refunds += sum(p.amount for p in paid if p.method == 'cash')
    invoice_by_method = defaultdict(float)
    for pay in document_payments:
        invoice_by_method[pay.method] += pay.amount
    cash_in = by_method.get('cash', 0.0) + invoice_by_method.get('cash', 0.0)
    return {
        'transactions': count,
        'gross': round(gross, 2),
        'discounts': round(discounts, 2),
        'net': round(gross - discounts, 2),
        'refunds': round(refunds, 2),
        'by_method': {k: round(v, 2) for k, v in sorted(by_method.items())},
        'invoice_by_method': {k: round(v, 2) for k, v in sorted(invoice_by_method.items())},
        'invoice_collections': round(sum(invoice_by_method.values()), 2),
        'opening_float': round(opening_float or 0, 2),
        'expected_cash': round((opening_float or 0) + cash_in - cash_refunds, 2),
    }


def shift_summary(shift):
    sales = (Sale.query.join(SaleMeta, SaleMeta.sale_id == Sale.id)
             .filter(SaleMeta.shift_id == shift.id).order_by(Sale.timestamp).all())
    payments = DocumentPayment.query.filter_by(shift_id=shift.id).all()
    return cash_summary(sales, payments, shift.opening_float)


@app.route('/shift', methods=['GET', 'POST'])
@login_required
def my_shift():
    shift = current_shift()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'open' and shift is None:
            shift = Shift(user_id=current_user.id, opening_float=_parse_float(request.form.get('opening_float'), 0.0) or 0.0)
            db.session.add(shift)
            db.session.commit()
            flash('Shift opened. Sales you make now are counted in this shift.', 'success')
            return redirect(url_for('process_sales'))
        if action == 'close' and shift is not None:
            summary = shift_summary(shift)
            shift.counted_cash = _parse_float(request.form.get('counted_cash'), 0.0)
            shift.expected_cash = summary['expected_cash']
            shift.notes = request.form.get('notes', '').strip() or None
            shift.closed_at = datetime.datetime.utcnow()
            db.session.commit()
            flash('Shift closed.', 'success')
            return redirect(url_for('shift_report', shift_id=shift.id))
    summary = shift_summary(shift) if shift else None
    recent = Shift.query.order_by(Shift.id.desc()).limit(20).all() if current_user.is_admin else \
        Shift.query.filter_by(user_id=current_user.id).order_by(Shift.id.desc()).limit(10).all()
    return render_template('shift.html', shift=shift, summary=summary, recent=recent)


@app.route('/shift/<int:shift_id>/report')
@login_required
def shift_report(shift_id):
    shift = Shift.query.get_or_404(shift_id)
    if shift.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    summary = shift_summary(shift)
    if shift.expected_cash is not None:
        summary['expected_cash'] = shift.expected_cash
    return render_template('z_report.html', title=f'Shift #{shift.id} Z report', shift=shift, summary=summary,
                           settings=BusinessSettings.get(), period=None)


@app.route('/reports/end-of-day')
@login_required
@admin_required
def end_of_day_report():
    day = _parse_date(request.args.get('date'), datetime.date.today())
    start = datetime.datetime.combine(day, datetime.time.min)
    end = start + datetime.timedelta(days=1)
    sales = Sale.query.filter(Sale.timestamp >= start, Sale.timestamp < end).order_by(Sale.timestamp).all()
    payments = DocumentPayment.query.filter(DocumentPayment.paid_on == day).all()
    shifts = Shift.query.filter(Shift.opened_at >= start, Shift.opened_at < end).all()
    summary = cash_summary(sales, payments, sum(s.opening_float for s in shifts))
    return render_template('z_report.html', title=f'End of day report {day.isoformat()}', shift=None, summary=summary,
                           settings=BusinessSettings.get(), period=day, shifts=shifts)


# =====================================================================
# Quotations, proforma invoices and invoices
# =====================================================================
DOC_TYPES = ('quotation', 'proforma', 'invoice')


def next_document_number(doc_type):
    prefix = Document.PREFIXES[doc_type]
    highest = 0
    for (number,) in db.session.query(Document.number).filter(Document.number.like(f'{prefix}-%')):
        tail = number.split('-', 1)[1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f'{prefix}-{highest + 1:04d}'


def parse_document_lines(form):
    """Read the line-item rows from the document form."""
    products = {p.id: p for p in Product.query.all()}
    lines = []
    for pid, desc, qty, price, tax in zip(form.getlist('line_product_id'), form.getlist('line_description'),
                                          form.getlist('line_quantity'), form.getlist('line_price'),
                                          form.getlist('line_tax_type')):
        product = products.get(int(pid)) if pid and pid.isdigit() else None
        desc = (desc or '').strip() or (product.name if product else '')
        quantity = _parse_float(qty, 0) or 0
        if not desc or quantity <= 0:
            continue
        unit_price = _parse_float(price)
        if unit_price is None:
            unit_price = product.price if product else 0.0
        tax = (tax or (product.tax_type if product else 'B') or 'B').upper()
        lines.append({
            'product_id': product.id if product else None,
            'description': desc,
            'quantity': quantity,
            'price': round(unit_price, 2),
            'tax_type': tax if tax in TAX_RATES else 'B',
        })
    return lines


def _apply_document_form(doc, form):
    customer_id = (form.get('customer_id') or '').strip()
    doc.customer_id = int(customer_id) if customer_id.isdigit() else None
    doc.issue_date = _parse_date(form.get('issue_date'), doc.issue_date or datetime.date.today())
    doc.valid_until = _parse_date(form.get('valid_until'))
    doc.due_date = _parse_date(form.get('due_date'))
    doc.discount_amount = max(_parse_float(form.get('discount_amount'), 0.0) or 0.0, 0.0)
    doc.notes = form.get('notes', '').strip() or None
    doc.terms = form.get('terms', '').strip() or None
    doc.lines = parse_document_lines(form)


def _document_form_context(doc):
    products = Product.query.order_by(Product.name).all()
    return dict(doc=doc, customers=Customer.query.order_by(Customer.name).all(), products=products,
                tax_types=TAX_TYPES)


@app.route('/documents')
@login_required
def list_documents():
    doc_type = request.args.get('type', 'invoice')
    if doc_type not in DOC_TYPES:
        doc_type = 'invoice'
    status = request.args.get('status', '')
    query = Document.query.filter_by(doc_type=doc_type)
    if status and status != 'overdue':
        query = query.filter_by(status=status)
    docs = query.order_by(Document.id.desc()).all()
    if status == 'overdue':
        docs = [d for d in docs if d.is_overdue]
    outstanding = sum(d.balance for d in docs if d.doc_type == 'invoice' and d.status in ('unpaid', 'partial'))
    return render_template('documents.html', docs=docs, doc_type=doc_type, status=status,
                           label=Document.LABELS[doc_type], outstanding=round(outstanding, 2))


@app.route('/documents/new', methods=['GET', 'POST'])
@login_required
def new_document():
    doc_type = request.values.get('type', 'quotation')
    if doc_type not in DOC_TYPES:
        abort(404)
    settings = BusinessSettings.get()
    doc = Document(doc_type=doc_type, status='draft', issue_date=datetime.date.today(), terms=settings.invoice_terms)
    if doc_type == 'invoice':
        doc.due_date = datetime.date.today() + datetime.timedelta(days=settings.invoice_due_days or 30)
    else:
        doc.valid_until = datetime.date.today() + datetime.timedelta(days=settings.quotation_valid_days or 14)
    if request.args.get('customer_id', '').isdigit():
        doc.customer_id = int(request.args['customer_id'])
    if request.method == 'POST':
        _apply_document_form(doc, request.form)
        if not doc.lines:
            flash('Add at least one line item.', 'error')
            return render_template('document_form.html', **_document_form_context(doc))
        doc.number = next_document_number(doc_type)
        doc.created_by = current_user.username
        db.session.add(doc)
        db.session.commit()
        flash(f'{doc.label} {doc.number} saved as a draft.', 'success')
        return redirect(url_for('view_document', doc_id=doc.id))
    return render_template('document_form.html', **_document_form_context(doc))


@app.route('/documents/<int:doc_id>')
@login_required
def view_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    settings = BusinessSettings.get()
    qr_b64 = generate_qr_code_b64(doc.sale.kra_qr_code_data) if doc.sale and doc.sale.kra_qr_code_data else None
    children = Document.query.filter_by(source_id=doc.id).all()
    return render_template('document_view.html', doc=doc, settings=settings, qr_b64=qr_b64, children=children,
                           today=datetime.date.today())


@app.route('/documents/<int:doc_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if not doc.is_editable:
        flash('Only drafts can be edited. Convert or duplicate it instead.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    if request.method == 'POST':
        _apply_document_form(doc, request.form)
        if not doc.lines:
            flash('Add at least one line item.', 'error')
            return render_template('document_form.html', **_document_form_context(doc))
        db.session.commit()
        flash(f'{doc.number} updated.', 'success')
        return redirect(url_for('view_document', doc_id=doc.id))
    return render_template('document_form.html', **_document_form_context(doc))


@app.route('/documents/<int:doc_id>/status', methods=['POST'])
@login_required
def set_document_status(doc_id):
    doc = Document.query.get_or_404(doc_id)
    new_status = request.form.get('status')
    allowed = {
        'quotation': {'draft': ('sent', 'accepted', 'declined'), 'sent': ('accepted', 'declined'), 'accepted': ('declined',), 'declined': ('accepted',)},
        'proforma': {'draft': ('sent',), 'sent': ('draft',)},
    }
    if new_status in allowed.get(doc.doc_type, {}).get(doc.status, ()):
        doc.status = new_status
        db.session.commit()
        flash(f'{doc.number} marked as {new_status}.', 'success')
    else:
        flash('That status change is not allowed.', 'error')
    return redirect(url_for('view_document', doc_id=doc.id))


@app.route('/documents/<int:doc_id>/convert/<target>', methods=['POST'])
@login_required
def convert_document(doc_id, target):
    doc = Document.query.get_or_404(doc_id)
    allowed = {'quotation': ('proforma', 'invoice'), 'proforma': ('invoice',)}
    if target not in allowed.get(doc.doc_type, ()):
        flash('That conversion is not possible.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    settings = BusinessSettings.get()
    new = Document(doc_type=target, status='draft', customer_id=doc.customer_id, issue_date=datetime.date.today(),
                   discount_amount=doc.discount_amount, notes=doc.notes, terms=doc.terms, source_id=doc.id,
                   created_by=current_user.username)
    new.lines = doc.lines
    if target == 'invoice':
        new.due_date = datetime.date.today() + datetime.timedelta(days=settings.invoice_due_days or 30)
    else:
        new.valid_until = datetime.date.today() + datetime.timedelta(days=settings.quotation_valid_days or 14)
    new.number = next_document_number(target)
    if doc.status in ('draft', 'sent', 'accepted'):
        doc.status = 'converted'
    db.session.add(new)
    db.session.commit()
    flash(f'{new.label} {new.number} created from {doc.number}. Review it, then issue it.', 'success')
    return redirect(url_for('view_document', doc_id=new.id))


@app.route('/documents/<int:doc_id>/duplicate', methods=['POST'])
@login_required
def duplicate_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    new = Document(doc_type=doc.doc_type, status='draft', customer_id=doc.customer_id, issue_date=datetime.date.today(),
                   discount_amount=doc.discount_amount, notes=doc.notes, terms=doc.terms, created_by=current_user.username,
                   valid_until=doc.valid_until, due_date=doc.due_date)
    new.lines = doc.lines
    new.number = next_document_number(doc.doc_type)
    db.session.add(new)
    db.session.commit()
    flash(f'Copied to {new.number}.', 'success')
    return redirect(url_for('edit_document', doc_id=new.id))


@app.route('/documents/<int:doc_id>/to-cart', methods=['POST'])
@login_required
def document_to_cart(doc_id):
    """Sell a quotation/proforma over the counter: load its items into the POS cart."""
    doc = Document.query.get_or_404(doc_id)
    cart = []
    skipped = []
    for line in doc.lines:
        product = Product.query.get(line['product_id']) if line.get('product_id') else None
        if product is None:
            skipped.append(line['description'])
            continue
        qty = int(line['quantity'])
        cart.append({'id': product.id, 'name': product.name, 'price': line['price'], 'quantity': qty,
                     'subtotal': round(line['price'] * qty, 2), 'tax_type': line.get('tax_type') or product.tax_type or 'B'})
    session['active_transaction'] = cart
    session['cart_document_id'] = doc.id
    session['cart_customer_id'] = doc.customer_id
    if skipped:
        flash(f'Not added (not stock items): {", ".join(skipped)}', 'info')
    flash(f'{doc.number} loaded into the cart. Apply the discount ({doc.discount_amount:.2f}) if it had one, then take payment.', 'success')
    return redirect(url_for('process_sales'))


@app.route('/documents/<int:doc_id>/issue', methods=['POST'])
@login_required
def issue_invoice(doc_id):
    """Finalise an invoice on credit: deduct stock, record it as a sale, and send it to tax integrations."""
    doc = Document.query.get_or_404(doc_id)
    if doc.doc_type != 'invoice' or doc.status != 'draft':
        flash('Only draft invoices can be issued.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    if not doc.lines:
        flash('The invoice has no lines.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))

    items = []
    for line in doc.lines:
        product = Product.query.get(line['product_id']) if line.get('product_id') else None
        qty = int(line['quantity']) if float(line['quantity']).is_integer() else line['quantity']
        if product is not None:
            if product.quantity < qty:
                flash(f'Not enough stock for {product.name}: {product.quantity} left, {qty:g} on the invoice.', 'error')
                db.session.rollback()
                return redirect(url_for('view_document', doc_id=doc.id))
            product.quantity -= int(qty)
        items.append({'id': product.id if product else None, 'name': line['description'], 'price': line['price'],
                      'quantity': qty, 'subtotal': round(line['price'] * qty, 2), 'tax_type': line.get('tax_type') or 'B'})

    customer = doc.customer
    sale = Sale(total_amount=doc.total, items=json.dumps(items), user_id=current_user.id,
                customer_name=customer.name if customer else None,
                customer_phone=customer.phone if customer else None,
                customer_email=customer.email if customer else None,
                discount_amount=doc.discount_amount or 0.0,
                discount_reason=f'Invoice {doc.number}' if doc.discount_amount else None)
    db.session.add(sale)
    db.session.flush()
    shift = current_shift()
    db.session.add(SaleMeta(sale_id=sale.id, customer_pin=customer.kra_pin if customer else None,
                            payment_method='credit', customer_id=doc.customer_id,
                            shift_id=shift.id if shift else None, document_id=doc.id))
    doc.sale_id = sale.id
    doc.status = 'unpaid'
    db.session.commit()
    emit_event(integrations.SALE_COMPLETED, sale=sale)
    doc.refresh_payment_status()
    db.session.commit()
    flash(f'Invoice {doc.number} issued. Stock updated{" and sent to eTIMS" if sale.kra_invoice_id else ""}.', 'success')
    return redirect(url_for('view_document', doc_id=doc.id))


@app.route('/documents/<int:doc_id>/payments', methods=['POST'])
@login_required
def record_document_payment(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.doc_type != 'invoice' or doc.status not in ('unpaid', 'partial'):
        flash('Payments can only be recorded on issued, unpaid invoices.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    amount = _parse_float(request.form.get('amount'), 0) or 0
    if amount <= 0:
        flash('Enter the amount received.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    if amount > doc.balance + 0.005:
        flash(f'That is more than the balance of {doc.balance:.2f}.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    shift = current_shift()
    doc.payments.append(DocumentPayment(amount=round(amount, 2),
                                   method=request.form.get('method', 'cash'),
                                   reference=request.form.get('reference', '').strip() or None,
                                   paid_on=_parse_date(request.form.get('paid_on'), datetime.date.today()),
                                   recorded_by=current_user.username, shift_id=shift.id if shift else None))
    doc.refresh_payment_status()
    db.session.commit()
    flash(f'Payment of {amount:.2f} recorded. Balance: {doc.balance:.2f}.', 'success')
    return redirect(url_for('view_document', doc_id=doc.id))


@app.route('/documents/<int:doc_id>/void', methods=['POST'])
@login_required
@admin_required
def void_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    if doc.status == 'void':
        return redirect(url_for('view_document', doc_id=doc.id))
    if doc.payments:
        flash('This invoice has payments recorded. Refund those first.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    reason = request.form.get('reason', '').strip() or 'Voided'
    sale = doc.sale
    if sale is not None and not sale.refunded:
        restock_sale_items(sale, f'Void of {doc.number} ({reason})', current_user.username)
        sale.refunded = True
        sale.refunded_at = datetime.datetime.utcnow()
        sale.refunded_by = current_user.username
        sale.refund_reason = reason
    doc.status = 'void'
    doc.notes = ((doc.notes or '') + f'\nVoided: {reason}').strip()
    db.session.commit()
    if sale is not None:
        emit_event(integrations.SALE_REFUNDED, sale=sale)  # e.g. eTIMS credit note
    flash(f'{doc.number} voided.', 'info')
    return redirect(url_for('view_document', doc_id=doc.id))


@app.route('/documents/<int:doc_id>/email', methods=['POST'])
@login_required
def email_document(doc_id):
    doc = Document.query.get_or_404(doc_id)
    recipient = request.form.get('email', '').strip() or (doc.customer.email if doc.customer else None)
    if not recipient:
        flash('Enter an email address.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    if not os.environ.get('MAIL_SERVER'):
        flash('Email is not configured yet. Set MAIL_SERVER, MAIL_USERNAME, MAIL_PASSWORD and MAIL_DEFAULT_SENDER.', 'error')
        return redirect(url_for('view_document', doc_id=doc.id))
    try:
        from flask_mail import Mail, Message
        for key, default in (('MAIL_SERVER', None), ('MAIL_USERNAME', None), ('MAIL_PASSWORD', None), ('MAIL_DEFAULT_SENDER', None)):
            app.config.setdefault(key, os.environ.get(key, default))
        app.config.setdefault('MAIL_PORT', int(os.environ.get('MAIL_PORT', 587)))
        app.config.setdefault('MAIL_USE_TLS', os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true')
        settings = BusinessSettings.get()
        html = render_template('document_view.html', doc=doc, settings=settings, qr_b64=None, children=[],
                               today=datetime.date.today(), for_email=True)
        msg = Message(subject=f'{doc.label} {doc.number} from {settings.business_name or "us"}',
                      recipients=[recipient], html=html)
        Mail(app).send(msg)
        if doc.doc_type in ('quotation', 'proforma') and doc.status == 'draft':
            doc.status = 'sent'
            db.session.commit()
        flash(f'{doc.number} emailed to {recipient}.', 'success')
    except Exception as e:
        print(f'Error emailing document: {e}')
        flash('Could not send the email. Check your MAIL_* settings.', 'error')
    return redirect(url_for('view_document', doc_id=doc.id))


@app.route('/reports/debtors')
@login_required
@admin_required
def debtors_report():
    open_invoices = Document.query.filter(Document.doc_type == 'invoice', Document.status.in_(('unpaid', 'partial'))).all()
    by_customer = defaultdict(list)
    for inv in open_invoices:
        by_customer[inv.customer_id].append(inv)
    rows = []
    for customer_id, invoices in by_customer.items():
        customer = Customer.query.get(customer_id) if customer_id else None
        rows.append({'customer': customer, 'invoices': invoices, 'aging': aging_buckets(invoices),
                     'total': round(sum(i.balance for i in invoices), 2)})
    rows.sort(key=lambda r: r['total'], reverse=True)
    totals = aging_buckets(open_invoices)
    return render_template('debtors.html', rows=rows, totals=totals, grand_total=round(sum(totals.values()), 2))


# =====================================================================
# Suppliers & purchase orders
# =====================================================================
@app.route('/suppliers', methods=['GET', 'POST'])
@login_required
@admin_required
def list_suppliers():
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id', '')
        supplier = Supplier.query.get(int(supplier_id)) if supplier_id.isdigit() else Supplier()
        if not request.form.get('name', '').strip():
            flash('Supplier name is required.', 'error')
            return redirect(url_for('list_suppliers'))
        _fill_contact(supplier, request.form)
        db.session.add(supplier)
        db.session.commit()
        flash(f'Supplier {supplier.name} saved.', 'success')
        return redirect(url_for('list_suppliers'))
    suppliers = Supplier.query.order_by(Supplier.name).all()
    edit = Supplier.query.get(int(request.args['edit'])) if request.args.get('edit', '').isdigit() else None
    return render_template('suppliers.html', suppliers=suppliers, edit=edit)


@app.route('/purchase-orders')
@login_required
@admin_required
def list_purchase_orders():
    orders = PurchaseOrder.query.order_by(PurchaseOrder.id.desc()).all()
    low_stock = [p for p in Product.query.order_by(Product.name).all() if p.is_low_stock]
    return render_template('purchase_orders.html', orders=orders, low_stock=low_stock)


@app.route('/purchase-orders/new', methods=['GET', 'POST'])
@login_required
@admin_required
def new_purchase_order():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    products = Product.query.order_by(Product.name).all()
    if request.method == 'POST':
        supplier_id = request.form.get('supplier_id', '')
        if not supplier_id.isdigit():
            flash('Choose a supplier (add one under Suppliers first).', 'error')
            return redirect(url_for('new_purchase_order'))
        by_id = {p.id: p for p in products}
        lines = []
        for pid, qty, cost in zip(request.form.getlist('line_product_id'), request.form.getlist('line_quantity'),
                                  request.form.getlist('line_unit_cost')):
            product = by_id.get(int(pid)) if pid.isdigit() else None
            quantity = int(_parse_float(qty, 0) or 0)
            if product is None or quantity <= 0:
                continue
            unit_cost = _parse_float(cost)
            if unit_cost is None:
                unit_cost = product.cost_price or 0.0
            lines.append({'product_id': product.id, 'name': product.name, 'quantity': quantity, 'unit_cost': round(unit_cost, 2)})
        if not lines:
            flash('Add at least one product.', 'error')
            return redirect(url_for('new_purchase_order'))
        po = PurchaseOrder(supplier_id=int(supplier_id), created_by=current_user.username,
                           expected_on=_parse_date(request.form.get('expected_on')),
                           notes=request.form.get('notes', '').strip() or None)
        po.lines = lines
        db.session.add(po)
        db.session.commit()
        flash(f'{po.number} created.', 'success')
        return redirect(url_for('view_purchase_order', po_id=po.id))
    prefill = [int(x) for x in request.args.getlist('product_id') if x.isdigit()]
    if request.args.get('low_stock'):
        prefill = [p.id for p in products if p.is_low_stock]
    return render_template('purchase_order_form.html', suppliers=suppliers, products=products, prefill=prefill)


@app.route('/purchase-orders/<int:po_id>')
@login_required
@admin_required
def view_purchase_order(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    return render_template('purchase_order_view.html', po=po, settings=BusinessSettings.get())


@app.route('/purchase-orders/<int:po_id>/receive', methods=['POST'])
@login_required
@admin_required
def receive_purchase_order(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status != 'ordered':
        flash('Only open purchase orders can be received.', 'error')
        return redirect(url_for('view_purchase_order', po_id=po.id))
    received = {k[len('received_'):]: v for k, v in request.form.items() if k.startswith('received_')}
    lines = po.lines
    for idx, line in enumerate(lines):
        qty = int(_parse_float(received.get(str(idx)), line['quantity']) or 0)
        line['received'] = qty
        product = Product.query.get(line['product_id'])
        if product is None or qty <= 0:
            continue
        previous = product.quantity
        product.quantity += qty
        if line.get('unit_cost'):
            product.cost_price = line['unit_cost']
        db.session.add(StockLog(product_id=product.id, quantity_added=qty, previous_quantity=previous,
                                new_quantity=product.quantity, note=f'Received {po.number} from {po.supplier.name}',
                                restocked_by=current_user.username))
    po.lines = lines
    po.status = 'received'
    po.received_at = datetime.datetime.utcnow()
    po.received_by = current_user.username
    po.supplier_invoice_no = request.form.get('supplier_invoice_no', '').strip() or None
    db.session.commit()
    flash(f'{po.number} received and stock updated.', 'success')
    return redirect(url_for('view_purchase_order', po_id=po.id))


@app.route('/purchase-orders/<int:po_id>/cancel', methods=['POST'])
@login_required
@admin_required
def cancel_purchase_order(po_id):
    po = PurchaseOrder.query.get_or_404(po_id)
    if po.status == 'ordered':
        po.status = 'cancelled'
        db.session.commit()
        flash(f'{po.number} cancelled.', 'info')
    return redirect(url_for('view_purchase_order', po_id=po.id))

# --- Main App Execution ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
