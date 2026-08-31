import os
import io
import csv
import datetime
import json
import requests
from collections import defaultdict
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, Response
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
if 'sslmode' not in app.config['SQLALCHEMY_DATABASE_URI']:
    app.config['SQLALCHEMY_DATABASE_URI'] += '?sslmode=require'


app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
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

# --- Startup: self-healing schema init ---
# No Shell access on Render's free tier and no Alembic migrations folder yet,
# so create any tables that don't exist and seed a default admin. This only
# creates MISSING tables — it never touches or alters tables that already
# exist, so it's safe to run on every boot and won't overwrite real data.
with app.app_context():
    try:
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin_password = os.environ.get('ADMIN_PASSWORD')
            if not admin_password:
                admin_password = 'admin123'
                print("⚠️  ADMIN_PASSWORD env var not set — using insecure default 'admin123'. Set ADMIN_PASSWORD and change the password after first login.")
            admin_user = User(username='admin', password=admin_password, role='admin')
            db.session.add(admin_user)
            db.session.commit()
            print("✅ Default admin user created.")
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

        return render_template(
            'index.html',
            total_sales=total_sales,
            total_items=total_items,
            products=all_products,
            low_stock_products=low_stock_products
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
            db.session.add(new_product)
            db.session.commit()
            flash('Product added successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            print(f"Error adding product: {e}")
            flash('Error adding product to database', 'error')
            
        return redirect(url_for('manage_products'))

    try:
        all_products = Product.query.all()
    except Exception as e:
        print(f"Error fetching products: {e}")
        all_products = []
        flash('Error loading products', 'error')
        
    return render_template('products.html', products=all_products)

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
                    return render_template('edit_product.html', product=product)

            product.name = request.form.get('name')
            product.price = float(request.form.get('price'))
            product.quantity = int(request.form.get('quantity'))
            product.barcode = new_barcode
            reorder_level_str = request.form.get('reorder_level')
            if reorder_level_str:
                product.reorder_level = int(reorder_level_str)
            product.updated_at = datetime.datetime.utcnow()
            image = request.files.get('image')

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
            
    return render_template('edit_product.html', product=product)

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
                        'subtotal': product.price * quantity
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
        
    return render_template('sales.html', products=all_products_dict, transaction=session.get('active_transaction', []), total=total)

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
        
        # In a real KRA integration, you would make an API call here.
        # For now, we'll simulate the data that would be returned.
        kra_invoice_id = f"KRA-INV-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        kra_qr_code_data = f"https://etims.kra.go.ke/verify?id={kra_invoice_id}"

        new_sale = Sale(
            total_amount=total_amount,
            items=items_str,
            user_id=current_user.id if current_user.is_authenticated else None,
            kra_invoice_id=kra_invoice_id,
            kra_qr_code_data=kra_qr_code_data,
            customer_name=request.form.get('customer_name', '').strip() or None,
            customer_phone=request.form.get('customer_phone', '').strip() or None,
            customer_email=request.form.get('customer_email', '').strip() or None,
            discount_amount=discount_amount,
            discount_reason=discount_reason if discount_amount > 0 else None
        )
        db.session.add(new_sale)
        
        db.session.commit()
        
        # Store the ID of the new sale in the session to easily redirect to its receipt
        session['last_sale_id'] = new_sale.id
        session.pop('active_transaction', None)
        
        flash('Sale completed successfully!', 'success')
        return redirect(url_for('view_receipt', sale_id=new_sale.id))
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
            secondary_amount=secondary_amount
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

    reason = request.form.get('refund_reason', '').strip() or None

    try:
        # Restock every item from the sale back into inventory, with an audit trail.
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
                    note=f"Refund of Sale #{sale.id}" + (f" ({reason})" if reason else ""),
                    restocked_by=current_user.username
                ))

        sale.refunded = True
        sale.refunded_at = datetime.datetime.utcnow()
        sale.refunded_by = current_user.username
        sale.refund_reason = reason

        db.session.commit()
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

# --- Main App Execution ---
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
