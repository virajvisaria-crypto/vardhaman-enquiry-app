import os
import sqlite3
import smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from functools import wraps
from urllib.parse import quote as urlquote

from flask import Flask, g, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "vardhaman.db"))

DEFAULT_SETTINGS = {
    "company_name": "Vardhaman Elastomer LLP",
    "company_location": "Matunga, Mumbai",
    "gst_number": "27AAWFV2676B1ZP",
    "bank_account_name": "Vardhaman Elastomer LLP",
    "bank_account_number": "",
    "bank_ifsc": "",
    "bank_name": "",
    "advance_percent": "25",
    "balance_days": "30",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _column_names(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def init_db():
    """Create tables if they don't exist, then migrate in missing columns.

    Written to be safe to run against an existing production DB: it never
    drops or rewrites data, it only adds what's missing.
    """
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    db.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS enquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            company_name TEXT,
            phone TEXT NOT NULL,
            email TEXT,
            product_category TEXT,
            specs TEXT,
            delivery_date TEXT,
            rate REAL,
            rate_unit TEXT,
            partner_name TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enquiry_id INTEGER NOT NULL,
            status TEXT DEFAULT 'open',
            advance_received INTEGER DEFAULT 0,
            advance_received_date TEXT,
            balance_received INTEGER DEFAULT 0,
            balance_received_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (enquiry_id) REFERENCES enquiries (id)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    db.commit()

    # --- migrations: add columns that older deployments won't have yet ---
    user_cols = _column_names(db, "users")
    if "is_admin" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        db.commit()

    enquiry_cols = _column_names(db, "enquiries")
    if "quantity" not in enquiry_cols:
        db.execute("ALTER TABLE enquiries ADD COLUMN quantity REAL DEFAULT 1")
        db.commit()

    if "is_active" not in user_cols:
        db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
        db.commit()

    order_cols = _column_names(db, "orders")
    if "confirmed" not in order_cols:
        db.execute("ALTER TABLE orders ADD COLUMN confirmed INTEGER DEFAULT 0")
        db.execute("ALTER TABLE orders ADD COLUMN confirmed_date TEXT")
        db.execute("ALTER TABLE orders ADD COLUMN dispatched INTEGER DEFAULT 0")
        db.execute("ALTER TABLE orders ADD COLUMN dispatched_date TEXT")
        db.commit()
        # Backfill: any order that already had advance or balance marked received
        # must logically have already been "confirmed", and anything with balance
        # received must have already been dispatched. Without this, every order
        # already in progress would silently vanish from the Orders page the
        # moment this deploys, since it now only shows confirmed orders.
        db.execute(
            "UPDATE orders SET confirmed = 1, confirmed_date = COALESCE(confirmed_date, advance_received_date, created_at) "
            "WHERE advance_received = 1 OR balance_received = 1"
        )
        db.execute(
            "UPDATE orders SET dispatched = 1, dispatched_date = COALESCE(dispatched_date, balance_received_date) "
            "WHERE balance_received = 1"
        )
        db.commit()

    # make sure exactly one admin exists (earliest-created user, if none flagged)
    admin_row = db.execute("SELECT id FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
    if admin_row is None:
        first_user = db.execute("SELECT id FROM users ORDER BY id ASC LIMIT 1").fetchone()
        if first_user:
            db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (first_user["id"],))
            db.commit()

    # seed default settings values that don't exist yet
    for k, v in DEFAULT_SETTINGS.items():
        row = db.execute("SELECT key FROM settings WHERE key = ?", (k,)).fetchone()
        if row is None:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()
    db.close()


def get_settings():
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        out[r["key"]] = r["value"]
    return out


@app.context_processor
def inject_globals():
    return {
        "current_user": get_current_user(),
        "settings": get_settings() if "user_id" in session else DEFAULT_SETTINGS,
    }


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        user = get_current_user()
        if user is None or not user["is_active"]:
            session.clear()
            flash("Your access has been disabled. Contact your admin.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            flash("Only an admin partner can access Settings.", "error")
            return redirect(url_for("enquiries"))
        return view(*args, **kwargs)
    return wrapped


def now_str():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    """Bootstrap-only. Works exactly once, when the users table is empty, so the
    very first admin account can be created without touching the database by
    hand. After that it always redirects to login. New partners are added by an
    admin from Settings > Partners instead of self-signup.
    """
    db = get_db()
    any_user = db.execute("SELECT id FROM users LIMIT 1").fetchone()
    if any_user is not None:
        flash("Sign-up is closed. Ask your admin to create your account from Settings.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()
        if not email or not password or not name:
            flash("Name, email and password are all required.", "error")
            return render_template("register.html")
        db.execute(
            "INSERT INTO users (email, password, name, created_at, is_admin, is_active) VALUES (?, ?, ?, ?, 1, 1)",
            (email, generate_password_hash(password), name, now_str()),
        )
        db.commit()
        flash("Admin account created. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None or not check_password_hash(user["password"], password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html")
        if not user["is_active"]:
            flash("Your access has been disabled. Contact your admin.", "error")
            return render_template("login.html")
        session["user_id"] = user["id"]
        return redirect(url_for("enquiries"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/account/password", methods=["GET", "POST"])
@login_required
def account_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        user = get_current_user()
        if not check_password_hash(user["password"], current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new != confirm:
            flash("New password and confirmation don't match.", "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
            db.commit()
            flash("Password updated.", "success")
            return redirect(url_for("enquiries"))
    return render_template("account_password.html")


# ---------------------------------------------------------------------------
# Enquiries
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("enquiries"))


@app.route("/enquiries")
@login_required
def enquiries():
    q = request.args.get("q", "").strip()
    db = get_db()
    base = """SELECT e.*, o.confirmed
              FROM enquiries e LEFT JOIN orders o ON o.enquiry_id = e.id"""
    if q:
        rows = db.execute(
            base + " WHERE e.customer_name LIKE ? OR e.company_name LIKE ? ORDER BY e.created_at DESC",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = db.execute(base + " ORDER BY e.created_at DESC").fetchall()
    return render_template("list.html", enquiries=rows, q=q)


@app.route("/enquiry/add", methods=["GET", "POST"])
@login_required
def enquiry_add():
    if request.method == "POST":
        db = get_db()
        ts = now_str()
        db.execute(
            """INSERT INTO enquiries
               (customer_name, company_name, phone, email, product_category, specs,
                delivery_date, rate, rate_unit, quantity, partner_name, user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form.get("customer_name", "").strip(),
                request.form.get("company_name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("product_category", "").strip(),
                request.form.get("specs", "").strip(),
                request.form.get("delivery_date", "").strip(),
                request.form.get("rate") or None,
                request.form.get("rate_unit", "").strip(),
                request.form.get("quantity") or 1,
                request.form.get("partner_name", "").strip(),
                session["user_id"],
                ts,
                ts,
            ),
        )
        db.commit()
        flash("Enquiry added.", "success")
        return redirect(url_for("enquiries"))
    return render_template("form.html", enquiry=None)


@app.route("/enquiry/<int:eid>")
@login_required
def enquiry_detail(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    order = db.execute("SELECT * FROM orders WHERE enquiry_id = ?", (eid,)).fetchone()
    return render_template("detail.html", enquiry=enquiry, order=order)


@app.route("/enquiry/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def enquiry_edit(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    if request.method == "POST":
        db.execute(
            """UPDATE enquiries SET customer_name=?, company_name=?, phone=?, email=?,
               product_category=?, specs=?, delivery_date=?, rate=?, rate_unit=?, quantity=?,
               partner_name=?, updated_at=? WHERE id=?""",
            (
                request.form.get("customer_name", "").strip(),
                request.form.get("company_name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("product_category", "").strip(),
                request.form.get("specs", "").strip(),
                request.form.get("delivery_date", "").strip(),
                request.form.get("rate") or None,
                request.form.get("rate_unit", "").strip(),
                request.form.get("quantity") or 1,
                request.form.get("partner_name", "").strip(),
                now_str(),
                eid,
            ),
        )
        db.commit()
        flash("Enquiry updated.", "success")
        return redirect(url_for("enquiry_detail", eid=eid))
    return render_template("form.html", enquiry=enquiry)


@app.route("/enquiry/<int:eid>/delete", methods=["POST"])
@login_required
def enquiry_delete(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    db.execute("DELETE FROM orders WHERE enquiry_id = ?", (eid,))
    db.execute("DELETE FROM enquiries WHERE id = ?", (eid,))
    db.commit()
    flash(f"Deleted enquiry for {enquiry['customer_name']}.", "success")
    return redirect(url_for("enquiries"))


# ---------------------------------------------------------------------------
# Orders (payment tracking)
# ---------------------------------------------------------------------------

def _get_or_create_order(db, eid):
    order = db.execute("SELECT * FROM orders WHERE enquiry_id = ?", (eid,)).fetchone()
    if order is None:
        ts = now_str()
        db.execute(
            "INSERT INTO orders (enquiry_id, created_at, updated_at) VALUES (?, ?, ?)",
            (eid, ts, ts),
        )
        db.commit()
        order = db.execute("SELECT * FROM orders WHERE enquiry_id = ?", (eid,)).fetchone()
    return order


@app.route("/enquiry/<int:eid>/order", methods=["POST"])
@login_required
def enquiry_order_update(eid):
    db = get_db()
    order = _get_or_create_order(db, eid)
    confirmed = 1 if request.form.get("confirmed") else 0
    advance_received = 1 if request.form.get("advance_received") else 0
    dispatched = 1 if request.form.get("dispatched") else 0
    balance_received = 1 if request.form.get("balance_received") else 0

    # Forward cascade: marking a later stage implies every earlier stage happened
    # too, even if someone forgot to tick the earlier boxes. Keeps the pipeline
    # state internally consistent (e.g. never dispatched=1 with confirmed=0).
    if balance_received:
        dispatched = 1
    if dispatched:
        advance_received = 1
    if advance_received:
        confirmed = 1

    ts = now_str()
    db.execute(
        """UPDATE orders SET confirmed=?, confirmed_date=?, advance_received=?, advance_received_date=?,
           dispatched=?, dispatched_date=?, balance_received=?, balance_received_date=?, updated_at=?
           WHERE id=?""",
        (
            confirmed,
            ts if confirmed and not order["confirmed"] else order["confirmed_date"],
            advance_received,
            ts if advance_received and not order["advance_received"] else order["advance_received_date"],
            dispatched,
            ts if dispatched and not order["dispatched"] else order["dispatched_date"],
            balance_received,
            ts if balance_received and not order["balance_received"] else order["balance_received_date"],
            ts,
            order["id"],
        ),
    )
    db.commit()
    flash("Order status updated.", "success")
    return redirect(url_for("enquiry_detail", eid=eid))


STAGE_ORDER = ["confirmed", "advance", "dispatched", "balance"]


def _current_stage(order):
    """Which pipeline stage an already-confirmed order is currently sitting at,
    waiting to move to the next one."""
    if not order["balance_received"]:
        if not order["dispatched"]:
            if not order["advance_received"]:
                return "confirmed"
            return "advance"
        return "dispatched"
    return "balance"


@app.route("/enquiry/<int:eid>/order/advance-stage", methods=["POST"])
@login_required
def enquiry_order_advance_stage(eid):
    db = get_db()
    order = _get_or_create_order(db, eid)
    stage = _current_stage(order)
    ts = now_str()
    if stage == "confirmed":
        db.execute("UPDATE orders SET advance_received=1, advance_received_date=?, updated_at=? WHERE id=?", (ts, ts, order["id"]))
    elif stage == "advance":
        db.execute("UPDATE orders SET dispatched=1, dispatched_date=?, updated_at=? WHERE id=?", (ts, ts, order["id"]))
    elif stage == "dispatched":
        db.execute("UPDATE orders SET balance_received=1, balance_received_date=?, updated_at=? WHERE id=?", (ts, ts, order["id"]))
    db.commit()
    tab = request.form.get("tab", "confirmed")
    return redirect(url_for("orders_page", tab=tab))


@app.route("/orders")
@login_required
def orders_page():
    tab = request.args.get("tab", "confirmed")
    if tab not in STAGE_ORDER:
        tab = "confirmed"
    db = get_db()
    rows = db.execute(
        """SELECT e.*, o.confirmed, o.advance_received, o.dispatched, o.balance_received,
                  o.confirmed_date, o.advance_received_date, o.dispatched_date, o.balance_received_date
           FROM enquiries e JOIN orders o ON o.enquiry_id = e.id
           WHERE o.confirmed = 1
           ORDER BY e.created_at DESC"""
    ).fetchall()

    today_str = date.today().isoformat()
    items = []
    for r in rows:
        stage = _current_stage(r)
        if stage != tab:
            continue
        overdue = (
            not r["balance_received"]
            and r["delivery_date"]
            and r["delivery_date"] < today_str
        )
        items.append({"row": r, "overdue": overdue, "stage": stage})

    next_label = {
        "confirmed": "Mark advance received",
        "advance": "Mark dispatched",
        "dispatched": "Mark balance received",
        "balance": None,
    }[tab]

    return render_template("orders.html", items=items, tab=tab, next_label=next_label)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    month_prefix = date.today().strftime("%Y-%m")

    total_this_month = db.execute(
        "SELECT COUNT(*) c FROM enquiries WHERE created_at LIKE ?", (f"{month_prefix}%",)
    ).fetchone()["c"]

    total_enquiries = db.execute("SELECT COUNT(*) c FROM enquiries").fetchone()["c"]

    confirmed_count = db.execute(
        "SELECT COUNT(*) c FROM orders WHERE confirmed = 1"
    ).fetchone()["c"]

    pending_advance = db.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(e.rate * e.quantity), 0) amt
           FROM enquiries e JOIN orders o ON o.enquiry_id = e.id
           WHERE o.confirmed = 1 AND o.advance_received = 0"""
    ).fetchone()

    pending_balance = db.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(e.rate * e.quantity), 0) amt
           FROM enquiries e JOIN orders o ON o.enquiry_id = e.id
           WHERE o.dispatched = 1 AND o.balance_received = 0"""
    ).fetchone()

    conversion_rate = 0
    if total_enquiries:
        conversion_rate = round(100 * confirmed_count / total_enquiries, 1)

    avg_row = db.execute(
        "SELECT AVG(rate * quantity) a FROM enquiries WHERE rate IS NOT NULL"
    ).fetchone()
    avg_quote_value = round(avg_row["a"], 2) if avg_row["a"] else 0

    by_category = db.execute(
        """SELECT COALESCE(NULLIF(TRIM(product_category), ''), 'Uncategorized') cat, COUNT(*) c
           FROM enquiries GROUP BY cat ORDER BY c DESC"""
    ).fetchall()
    max_cat = max([r["c"] for r in by_category], default=1)

    stats = {
        "total_this_month": total_this_month,
        "total_enquiries": total_enquiries,
        "confirmed_count": confirmed_count,
        "pending_advance_count": pending_advance["c"],
        "pending_advance_amount": round(pending_advance["amt"], 2),
        "pending_balance_count": pending_balance["c"],
        "pending_balance_amount": round(pending_balance["amt"], 2),
        "conversion_rate": conversion_rate,
        "avg_quote_value": avg_quote_value,
    }
    return render_template("dashboard.html", stats=stats, by_category=by_category, max_cat=max_cat)


# ---------------------------------------------------------------------------
# Settings (admin only)
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings_page():
    db = get_db()
    if request.method == "POST":
        for key in DEFAULT_SETTINGS.keys():
            value = request.form.get(key, "").strip()
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        db.commit()
        flash("Settings updated.", "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=get_settings())


@app.route("/settings/partners")
@login_required
@admin_required
def partners_page():
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return render_template("partners.html", users=users)


@app.route("/settings/partners/add", methods=["POST"])
@login_required
@admin_required
def partners_add():
    db = get_db()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not name or not email or not password:
        flash("Name, email and a temporary password are all required.", "error")
        return redirect(url_for("partners_page"))
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash("That email already has an account.", "error")
        return redirect(url_for("partners_page"))
    db.execute(
        "INSERT INTO users (email, password, name, created_at, is_admin, is_active) VALUES (?, ?, ?, ?, 0, 1)",
        (email, generate_password_hash(password), name, now_str()),
    )
    db.commit()
    flash(f"Account created for {name}. Share the temporary password with them directly, they can change it under Account.", "success")
    return redirect(url_for("partners_page"))


@app.route("/settings/partners/<int:uid>/toggle-active", methods=["POST"])
@login_required
@admin_required
def partners_toggle_active(uid):
    if uid == session.get("user_id"):
        flash("You can't disable your own account.", "error")
        return redirect(url_for("partners_page"))
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None:
        flash("Account not found.", "error")
        return redirect(url_for("partners_page"))
    new_status = 0 if user["is_active"] else 1
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, uid))
    db.commit()
    flash(f"{user['name']} {'enabled' if new_status else 'disabled'}.", "success")
    return redirect(url_for("partners_page"))


@app.route("/settings/partners/<int:uid>/toggle-admin", methods=["POST"])
@login_required
@admin_required
def partners_toggle_admin(uid):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if user is None:
        flash("Account not found.", "error")
        return redirect(url_for("partners_page"))
    if user["is_admin"]:
        admin_count = db.execute("SELECT COUNT(*) c FROM users WHERE is_admin = 1").fetchone()["c"]
        if admin_count <= 1:
            flash("Can't remove the last admin. Make someone else admin first.", "error")
            return redirect(url_for("partners_page"))
    new_status = 0 if user["is_admin"] else 1
    db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_status, uid))
    db.commit()
    flash(f"{user['name']} {'is now' if new_status else 'is no longer'} an admin.", "success")
    return redirect(url_for("partners_page"))


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def build_quote_text(enquiry, settings):
    rate = enquiry["rate"] or 0
    quantity = enquiry["quantity"] or 1
    total = round(rate * quantity, 2)
    advance_pct = float(settings.get("advance_percent", 25) or 25)
    advance_amt = round(total * advance_pct / 100, 2)
    balance_amt = round(total - advance_amt, 2)

    raw_unit = (enquiry["rate_unit"] or "unit").strip()
    unit_noun = raw_unit[4:].strip() if raw_unit.lower().startswith("per ") else raw_unit

    lines = [
        settings.get("company_name", ""),
        settings.get("company_location", ""),
        f"GST: {settings.get('gst_number', '')}",
        "",
        f"Quote for: {enquiry['customer_name']}" + (f" ({enquiry['company_name']})" if enquiry["company_name"] else ""),
        f"Product: {enquiry['product_category'] or '-'}",
        f"Specs: {enquiry['specs'] or '-'}",
        f"Quantity: {quantity} {unit_noun}".strip(),
        f"Rate: {rate} per {unit_noun}",
        f"Total: Rs. {total}",
        "",
        f"Advance ({advance_pct:.0f}%): Rs. {advance_amt}",
        f"Balance ({100 - advance_pct:.0f}%): Rs. {balance_amt}",
        f"Balance due within {settings.get('balance_days', '30')} days of delivery",
        "",
        f"Delivery: {enquiry['delivery_date'] or 'TBD'}",
        "",
        "Bank details:",
        f"  A/c name: {settings.get('bank_account_name', '')}",
        f"  A/c no: {settings.get('bank_account_number', '') or 'not set in Settings'}",
        f"  IFSC: {settings.get('bank_ifsc', '') or 'not set in Settings'}",
        f"  Bank: {settings.get('bank_name', '') or 'not set in Settings'}",
    ]
    return "\n".join(lines), total, advance_amt, balance_amt


@app.route("/enquiry/<int:eid>/quote")
@login_required
def enquiry_quote(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    settings = get_settings()
    quote_text, total, advance_amt, balance_amt = build_quote_text(enquiry, settings)
    whatsapp_url = "https://wa.me/?text=" + urlquote(quote_text)
    return render_template(
        "quote.html",
        enquiry=enquiry,
        quote_text=quote_text,
        total=total,
        advance_amt=advance_amt,
        balance_amt=balance_amt,
        whatsapp_url=whatsapp_url,
    )


@app.route("/enquiry/<int:eid>/quote/email", methods=["POST"])
@login_required
def enquiry_quote_email(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    if not enquiry["email"]:
        flash("This enquiry has no customer email on file.", "error")
        return redirect(url_for("enquiry_quote", eid=eid))

    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = os.environ.get("SMTP_PORT", "587")
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)

    if not all([smtp_host, smtp_user, smtp_pass]):
        flash(
            "Email isn't configured yet. Set SMTP_HOST, SMTP_USER, SMTP_PASS "
            "(and optionally SMTP_PORT, SMTP_FROM) as Railway env vars first.",
            "error",
        )
        return redirect(url_for("enquiry_quote", eid=eid))

    settings = get_settings()
    quote_text, *_ = build_quote_text(enquiry, settings)

    msg = MIMEText(quote_text)
    msg["Subject"] = f"Quote from {settings.get('company_name', 'Vardhaman Elastomer')}"
    msg["From"] = smtp_from
    msg["To"] = enquiry["email"]

    try:
        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [enquiry["email"]], msg.as_string())
        flash(f"Quote emailed to {enquiry['email']}.", "success")
    except Exception as exc:  # noqa: BLE001 - surface the real error to the admin
        flash(f"Email failed to send: {exc}", "error")

    return redirect(url_for("enquiry_quote", eid=eid))


# ---------------------------------------------------------------------------

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
