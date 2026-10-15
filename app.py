"""CloudCart - Monolithic e-commerce application.

A single Flask process serves the web UI, holds all the business logic
(catalogue, cart, checkout, orders) and talks to ONE shared database.

Database selection (environment variables):
  DB_HOST set     -> MySQL  (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME)
  DB_HOST not set -> SQLite file (handy for running locally without MySQL)
"""
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote_plus

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from sqlalchemy.exc import OperationalError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cloudcart-dev-secret")

STUDENT_NAME = os.environ.get("STUDENT_NAME", "Samaan Sayed")
FREE_SHIPPING_ABOVE = 999
SHIPPING_FEE = 49

DB_HOST = os.environ.get("DB_HOST", "")
if DB_HOST:
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "password")
    DB_PORT = os.environ.get("DB_PORT", "3306")
    DB_NAME = os.environ.get("DB_NAME", "shop_db")
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"mysql+pymysql://{DB_USER}:{quote_plus(DB_PASSWORD)}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
    )
    DB_LABEL = f"MySQL @ {DB_HOST}:{DB_PORT}/{DB_NAME}"
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///shop.db"
    DB_LABEL = "SQLite (shop.db)"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

db = SQLAlchemy(app)


# ---------------------------------------------------------------- models ---
class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    price = db.Column(db.Integer, nullable=False)  # whole rupees
    stock = db.Column(db.Integer, nullable=False, default=0)
    emoji = db.Column(db.String(16), nullable=False)


class CartItem(db.Model):
    __tablename__ = "cart_items"
    id = db.Column(db.Integer, primary_key=True)
    cart_id = db.Column(db.String(32), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    product = db.relationship("Product")


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    address = db.Column(db.String(255), nullable=False)
    payment_method = db.Column(db.String(30), nullable=False)
    subtotal = db.Column(db.Integer, nullable=False)
    shipping = db.Column(db.Integer, nullable=False)
    total = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    items = db.relationship("OrderItem", backref="order", cascade="all, delete-orphan")


class OrderItem(db.Model):
    __tablename__ = "order_items"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    product_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    emoji = db.Column(db.String(16), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, nullable=False)


SEED_PRODUCTS = [
    ("Wireless Headphones", "Noise-cancelling over-ear headphones, 30 h battery.", "Audio", 2999, 25, "🎧"),
    ("Mechanical Keyboard", "Hot-swappable switches with RGB backlight.", "Accessories", 3499, 15, "⌨️"),
    ("Smart Watch", "AMOLED display with heart-rate and SpO2 tracking.", "Wearables", 4999, 20, "⌚"),
    ("Bluetooth Speaker", "Portable, waterproof speaker with deep bass.", "Audio", 1799, 30, "🔊"),
    ("Laptop Backpack", "Water-resistant, fits laptops up to 15.6 inches.", "Bags", 1299, 40, "🎒"),
    ("Wireless Mouse", "Silent clicks, 2.4 GHz and Bluetooth.", "Accessories", 699, 50, "🖱️"),
    ("Action Camera", "4K60 video with image stabilisation.", "Cameras", 8999, 10, "📷"),
    ("Power Bank", "20000 mAh with 22.5 W fast charging.", "Accessories", 1499, 35, "🔋"),
]


# --------------------------------------------------------------- helpers ---
@app.template_filter("inr")
def inr(amount):
    return f"₹{amount:,}"


def current_cart_id():
    if "cart_id" not in session:
        session["cart_id"] = uuid.uuid4().hex
    return session["cart_id"]


def cart_items():
    rows = CartItem.query.filter_by(cart_id=current_cart_id()).order_by(CartItem.id).all()
    return [
        {
            "id": row.id,
            "product_id": row.product_id,
            "name": row.product.name,
            "emoji": row.product.emoji,
            "price": row.product.price,
            "stock": row.product.stock,
            "quantity": row.quantity,
            "line_total": row.product.price * row.quantity,
        }
        for row in rows
    ]


def cart_totals(items):
    subtotal = sum(i["line_total"] for i in items)
    shipping = 0 if subtotal == 0 or subtotal >= FREE_SHIPPING_ABOVE else SHIPPING_FEE
    return {"subtotal": subtotal, "shipping": shipping, "total": subtotal + shipping}


def order_dict(order):
    return {
        "id": order.id,
        "customer_name": order.customer_name,
        "email": order.email,
        "address": order.address,
        "payment_method": order.payment_method,
        "subtotal": order.subtotal,
        "shipping": order.shipping,
        "total": order.total,
        "created_at": order.created_at.strftime("%d %b %Y, %H:%M UTC"),
        "items": [
            {"name": i.name, "emoji": i.emoji, "price": i.price, "quantity": i.quantity,
             "line_total": i.price * i.quantity}
            for i in order.items
        ],
    }


@app.context_processor
def inject_layout_data():
    count = (
        db.session.query(func.coalesce(func.sum(CartItem.quantity), 0))
        .filter(CartItem.cart_id == current_cart_id())
        .scalar()
    )
    return {
        "architecture": "Monolith",
        "cart_count": count,
        "db_label": DB_LABEL,
        "hostname": socket.gethostname(),
        "student_name": STUDENT_NAME,
    }


# ------------------------------------------------------------------ shop ---
@app.route("/")
def index():
    category = request.args.get("category")
    query = Product.query.order_by(Product.id)
    if category:
        query = query.filter_by(category=category)
    categories = [c for (c,) in db.session.query(Product.category).distinct().order_by(Product.category)]
    return render_template("index.html", products=query.all(), categories=categories, active_category=category)


@app.route("/cart")
def view_cart():
    items = cart_items()
    return render_template("cart.html", items=items, totals=cart_totals(items))


@app.post("/cart/add/<int:product_id>")
def add_to_cart(product_id):
    product = db.get_or_404(Product, product_id)
    item = CartItem.query.filter_by(cart_id=current_cart_id(), product_id=product_id).first()
    wanted = (item.quantity if item else 0) + 1
    if wanted > product.stock:
        flash(f"Sorry, only {product.stock} × {product.name} in stock.", "error")
    else:
        if item:
            item.quantity = wanted
        else:
            db.session.add(CartItem(cart_id=current_cart_id(), product_id=product_id, quantity=1))
        db.session.commit()
        flash(f"Added {product.name} to your cart.", "success")
    return redirect(request.referrer or url_for("index"))


@app.post("/cart/update/<int:item_id>")
def update_cart(item_id):
    item = CartItem.query.filter_by(id=item_id, cart_id=current_cart_id()).first_or_404()
    quantity = request.form.get("quantity", type=int, default=1)
    if quantity <= 0:
        db.session.delete(item)
    elif quantity > item.product.stock:
        flash(f"Only {item.product.stock} × {item.product.name} in stock.", "error")
        return redirect(url_for("view_cart"))
    else:
        item.quantity = quantity
    db.session.commit()
    return redirect(url_for("view_cart"))


@app.post("/cart/remove/<int:item_id>")
def remove_from_cart(item_id):
    item = CartItem.query.filter_by(id=item_id, cart_id=current_cart_id()).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash("Item removed from cart.", "success")
    return redirect(url_for("view_cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items = cart_items()
    if not items:
        flash("Your cart is empty.", "error")
        return redirect(url_for("index"))
    totals = cart_totals(items)

    if request.method == "GET":
        return render_template("checkout.html", items=items, totals=totals, form={})

    form = {k: request.form.get(k, "").strip() for k in ("customer_name", "email", "address", "payment_method")}
    if not all(form.values()):
        flash("Please fill in every field.", "error")
        return render_template("checkout.html", items=items, totals=totals, form=form)

    # Everything happens in ONE database transaction - a key trait of a monolith.
    rows = CartItem.query.filter_by(cart_id=current_cart_id()).all()
    for row in rows:
        if row.quantity > row.product.stock:
            flash(f"Only {row.product.stock} × {row.product.name} left - please update your cart.", "error")
            return redirect(url_for("view_cart"))

    order = Order(**form, **totals)
    for row in rows:
        row.product.stock -= row.quantity
        order.items.append(OrderItem(product_id=row.product_id, name=row.product.name, emoji=row.product.emoji,
                                     price=row.product.price, quantity=row.quantity))
        db.session.delete(row)
    db.session.add(order)
    db.session.commit()
    return redirect(url_for("order_detail", order_id=order.id, new=1))


@app.route("/orders")
def orders():
    all_orders = Order.query.order_by(Order.id.desc()).all()
    return render_template("orders.html", orders=[order_dict(o) for o in all_orders])


@app.route("/orders/<int:order_id>")
def order_detail(order_id):
    order = db.get_or_404(Order, order_id)
    return render_template("order_detail.html", order=order_dict(order), new=request.args.get("new"))


@app.route("/status")
def status():
    counts = {
        "products": Product.query.count(),
        "cart_items": CartItem.query.count(),
        "orders": Order.query.count(),
    }
    return render_template("status.html", counts=counts)


@app.route("/health")
def health():
    return jsonify(status="ok", architecture="monolith", database=DB_LABEL, host=socket.gethostname(),
                   products=Product.query.count(), orders=Order.query.count())


# ----------------------------------------------------------------- start ---
def init_db(max_retries=40, delay_seconds=3):
    """MySQL may still be starting when this container starts, so retry."""
    for attempt in range(1, max_retries + 1):
        try:
            with app.app_context():
                db.create_all()
                if Product.query.count() == 0:
                    db.session.add_all(
                        Product(name=n, description=d, category=c, price=p, stock=s, emoji=e)
                        for (n, d, c, p, s, e) in SEED_PRODUCTS
                    )
                    db.session.commit()
            print(f"Connected to {DB_LABEL}; tables ready.", flush=True)
            return
        except OperationalError as exc:
            print(f"Database not ready ({attempt}/{max_retries}): {exc.orig}", flush=True)
            time.sleep(delay_seconds)
    raise RuntimeError("Could not connect to the database.")


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
