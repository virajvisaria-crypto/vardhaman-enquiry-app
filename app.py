import os
import sqlite3
import smtplib
import csv
import io
import re
import tempfile
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from functools import wraps
from urllib.parse import quote as urlquote

from flask import Flask, g, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # stops other sites from making the browser submit forms here
    SESSION_COOKIE_SECURE=bool(os.environ.get("RAILWAY_ENVIRONMENT")),  # https-only on Railway
)

IST = ZoneInfo("Asia/Kolkata")


def today_ist():
    """Railway's server clock runs on UTC/US time by default, which is wrong
    for an India-based business, use this everywhere instead of date.today()."""
    return datetime.now(IST).date()


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
    db.execute(
        """CREATE TABLE IF NOT EXISTS enquiry_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            enquiry_id INTEGER NOT NULL,
            product_category TEXT,
            specs TEXT,
            rate REAL,
            rate_unit TEXT,
            quantity REAL DEFAULT 1,
            confirmed INTEGER DEFAULT 0,
            confirmed_date TEXT,
            advance_received INTEGER DEFAULT 0,
            advance_received_date TEXT,
            dispatched INTEGER DEFAULT 0,
            dispatched_date TEXT,
            balance_received INTEGER DEFAULT 0,
            balance_received_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (enquiry_id) REFERENCES enquiries (id)
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

    if "enquiry_date" not in enquiry_cols:
        db.execute("ALTER TABLE enquiries ADD COLUMN enquiry_date TEXT")
        # Existing enquiries get today's date as a clean starting point, not the
        # old created_at timestamp, since that may not reflect the real enquiry
        # date anyway (some of this data went through an earlier reset). Partners
        # can correct individual enquiries by editing them.
        db.execute(
            "UPDATE enquiries SET enquiry_date = ? WHERE enquiry_date IS NULL",
            (today_ist().isoformat(),),
        )
        db.commit()

    if "tax_invoice_no" not in enquiry_cols:
        db.execute("ALTER TABLE enquiries ADD COLUMN tax_invoice_no TEXT")
        db.execute("ALTER TABLE enquiries ADD COLUMN tax_invoice_date TEXT")
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

    # One-time migration to multi-product line items. Guarded by an explicit
    # flag (not "table is empty") since the table could legitimately become
    # empty later if someone deletes items, and emptiness would wrongly
    # re-trigger this and duplicate everything.
    already_migrated = db.execute(
        "SELECT value FROM settings WHERE key = '_migrated_items_v1'"
    ).fetchone()
    if already_migrated is None:
        old_enquiries = db.execute("SELECT * FROM enquiries").fetchall()
        for e in old_enquiries:
            old_order = db.execute(
                "SELECT * FROM orders WHERE enquiry_id = ?", (e["id"],)
            ).fetchone()
            db.execute(
                """INSERT INTO enquiry_items
                   (enquiry_id, product_category, specs, rate, rate_unit, quantity,
                    confirmed, confirmed_date, advance_received, advance_received_date,
                    dispatched, dispatched_date, balance_received, balance_received_date,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    e["id"], e["product_category"], e["specs"], e["rate"], e["rate_unit"], e["quantity"] or 1,
                    (old_order["confirmed"] if old_order else 0) or 0,
                    old_order["confirmed_date"] if old_order else None,
                    (old_order["advance_received"] if old_order else 0) or 0,
                    old_order["advance_received_date"] if old_order else None,
                    (old_order["dispatched"] if old_order else 0) or 0,
                    old_order["dispatched_date"] if old_order else None,
                    (old_order["balance_received"] if old_order else 0) or 0,
                    old_order["balance_received_date"] if old_order else None,
                    e["created_at"], e["updated_at"],
                ),
            )
        db.execute("INSERT INTO settings (key, value) VALUES ('_migrated_items_v1', '1')")
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
            flash("Only an admin partner can do that.", "error")
            return redirect(url_for("enquiries"))
        return view(*args, **kwargs)
    return wrapped


def now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value, default=0.0):
    """Forgiving number reader for display and quote maths, so one bad value
    already sitting in the database can never crash a page."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _advance_pct(settings):
    pct = _to_float(settings.get("advance_percent"), 25.0)
    return pct if 0 <= pct <= 100 else 25.0


def _safe_next(target, fallback):
    """Only allow redirects to a page inside this app."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return fallback


_PLAIN_NUMBER = re.compile(r"^[+-]?[\d\s()-]+$")


def _csv_cell(value):
    """Stop Excel/Sheets from running a cell as a formula (a name starting with
    '=' or '@', say). Phone numbers like +91 98... are left alone."""
    if value is None:
        return ""
    text = str(value)
    if text and (text[0] in "=@\t\r" or (text[0] in "+-" and not _PLAIN_NUMBER.match(text))):
        return "'" + text
    return text


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


ARCHIVE_DAYS = 180  # ~6 months


def _query_enquiries(q, product_filter, delivery_from, delivery_to, sort, sort_dir, bucket="active"):
    db = get_db()
    clauses = []
    params = []
    if q:
        clauses.append("(customer_name LIKE ? OR company_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if delivery_from:
        clauses.append("delivery_date >= ?")
        params.append(delivery_from)
    if delivery_to:
        clauses.append("delivery_date <= ?")
        params.append(delivery_to)

    cutoff = (today_ist() - timedelta(days=ARCHIVE_DAYS)).isoformat()
    if bucket == "archive":
        clauses.append("enquiry_date < ?")
        params.append(cutoff)
    else:
        clauses.append("(enquiry_date IS NULL OR enquiry_date >= ?)")
        params.append(cutoff)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"SELECT * FROM enquiries {where} ORDER BY enquiry_date DESC, created_at DESC", params
    ).fetchall()

    results = []
    for e in rows:
        items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ?", (e["id"],)).fetchall()
        # Once any product on an enquiry is confirmed, the whole enquiry moves
        # to the Orders page instead, it doesn't belong on Enquiries or Archive
        # anymore, regardless of whether its other products are still unconfirmed.
        if any(i["confirmed"] for i in items):
            continue
        categories = [i["product_category"] for i in items if i["product_category"]]
        if product_filter and product_filter not in categories:
            continue
        if len(categories) > 2:
            product_summary = f"{categories[0]}, {categories[1]} +{len(categories) - 2} more"
        else:
            product_summary = ", ".join(categories) if categories else "-"
        results.append({"e": e, "product_summary": product_summary, "confirmed": False})

    sort_keys = {
        "customer": lambda r: (r["e"]["customer_name"] or "").lower(),
        "enquiry_date": lambda r: r["e"]["enquiry_date"] or "",
        "product": lambda r: r["product_summary"].lower(),
        "delivery": lambda r: r["e"]["delivery_date"] or "9999-99-99",
    }
    if sort in sort_keys:
        results.sort(key=sort_keys[sort], reverse=(sort_dir == "desc"))
    return results


@app.route("/enquiries")
@login_required
def enquiries():
    q = request.args.get("q", "").strip()
    product_filter = request.args.get("product", "").strip()
    delivery_from = request.args.get("delivery_from", "").strip()
    delivery_to = request.args.get("delivery_to", "").strip()
    sort = request.args.get("sort", "")
    sort_dir = request.args.get("dir", "asc")

    results = _query_enquiries(q, product_filter, delivery_from, delivery_to, sort, sort_dir, bucket="active")

    db = get_db()
    all_categories = [
        r["product_category"] for r in db.execute(
            "SELECT DISTINCT product_category FROM enquiry_items "
            "WHERE product_category IS NOT NULL AND product_category != '' ORDER BY product_category"
        ).fetchall()
    ]

    def sort_link(col):
        next_dir = "desc" if sort == col and sort_dir == "asc" else "asc"
        return url_for("enquiries", q=q or None, product=product_filter or None,
                        delivery_from=delivery_from or None, delivery_to=delivery_to or None,
                        sort=col, dir=next_dir)

    return render_template(
        "list.html", enquiries=results, q=q, product_filter=product_filter,
        delivery_from=delivery_from, delivery_to=delivery_to, all_categories=all_categories,
        sort=sort, sort_dir=sort_dir, sort_link=sort_link, archive=False,
    )


@app.route("/enquiries/archive")
@login_required
def enquiries_archive():
    q = request.args.get("q", "").strip()
    product_filter = request.args.get("product", "").strip()
    delivery_from = request.args.get("delivery_from", "").strip()
    delivery_to = request.args.get("delivery_to", "").strip()
    sort = request.args.get("sort", "")
    sort_dir = request.args.get("dir", "asc")

    results = _query_enquiries(q, product_filter, delivery_from, delivery_to, sort, sort_dir, bucket="archive")

    db = get_db()
    all_categories = [
        r["product_category"] for r in db.execute(
            "SELECT DISTINCT product_category FROM enquiry_items "
            "WHERE product_category IS NOT NULL AND product_category != '' ORDER BY product_category"
        ).fetchall()
    ]

    def sort_link(col):
        next_dir = "desc" if sort == col and sort_dir == "asc" else "asc"
        return url_for("enquiries_archive", q=q or None, product=product_filter or None,
                        delivery_from=delivery_from or None, delivery_to=delivery_to or None,
                        sort=col, dir=next_dir)

    return render_template(
        "list.html", enquiries=results, q=q, product_filter=product_filter,
        delivery_from=delivery_from, delivery_to=delivery_to, all_categories=all_categories,
        sort=sort, sort_dir=sort_dir, sort_link=sort_link, archive=True,
    )


@app.route("/enquiries/export")
@login_required
def enquiries_export():
    q = request.args.get("q", "").strip()
    product_filter = request.args.get("product", "").strip()
    delivery_from = request.args.get("delivery_from", "").strip()
    delivery_to = request.args.get("delivery_to", "").strip()
    sort = request.args.get("sort", "")
    sort_dir = request.args.get("dir", "asc")
    bucket = "archive" if request.args.get("archive") == "1" else "active"
    results = _query_enquiries(q, product_filter, delivery_from, delivery_to, sort, sort_dir, bucket=bucket)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Customer", "Company", "Phone", "Email", "Enquiry Date", "Delivery Date", "Products", "Partner", "Order"])
    for r in results:
        e = r["e"]
        writer.writerow([_csv_cell(v) for v in [
            e["customer_name"], e["company_name"] or "", e["phone"], e["email"] or "",
            e["enquiry_date"] or "", e["delivery_date"] or "", r["product_summary"],
            e["partner_name"], "Yes" if r["confirmed"] else "No",
        ]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={'archive' if bucket == 'archive' else 'enquiries'}.csv"},
    )


MAX_ITEMS = 10


def _parse_item_slots(form):
    """Read item_1_* .. item_10_* fields from a submitted form. A slot counts
    as filled if it has a product category or a rate, empty trailing slots
    (from the "+ Add another product" UI) are simply ignored."""
    slots = []
    for n in range(1, MAX_ITEMS + 1):
        category = form.get(f"item_{n}_product_category", "").strip()
        rate = form.get(f"item_{n}_rate", "").strip()
        if not category and not rate:
            continue
        qty_raw = form.get(f"item_{n}_quantity", "").strip()
        try:
            rate_val = float(rate) if rate else None
            qty_val = float(qty_raw) if qty_raw else 1.0
        except ValueError:
            raise ValueError(f"Product {n}: rate and quantity must be numbers.")
        if (rate_val is not None and rate_val < 0) or qty_val <= 0:
            raise ValueError(f"Product {n}: rate can't be negative and quantity must be above zero.")
        slots.append({
            "item_id": form.get(f"item_{n}_id", "").strip(),
            "product_category": category,
            "specs": form.get(f"item_{n}_specs", "").strip(),
            "rate": rate_val,
            "rate_unit": form.get(f"item_{n}_rate_unit", "").strip(),
            "quantity": qty_val,
            "delete": form.get(f"item_{n}_delete") == "1",
        })
    return slots


def _check_enquiry_form(form):
    """Server-side backstop for the form. Returns (items, error_message)."""
    for field, label in (("customer_name", "Customer name"), ("phone", "Phone"), ("partner_name", "Partner name")):
        if not form.get(field, "").strip():
            return None, f"{label} is required."
    try:
        return _parse_item_slots(form), None
    except ValueError as exc:
        return None, str(exc)


@app.route("/enquiry/add", methods=["GET", "POST"])
@login_required
def enquiry_add():
    if request.method == "POST":
        parsed_items, error = _check_enquiry_form(request.form)
        if error:
            flash(error + " Nothing was saved, please try again.", "error")
            return redirect(url_for("enquiry_add"))
        db = get_db()
        ts = now_str()
        cur = db.execute(
            """INSERT INTO enquiries
               (customer_name, company_name, phone, email, delivery_date, enquiry_date,
                partner_name, user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request.form.get("customer_name", "").strip(),
                request.form.get("company_name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("delivery_date", "").strip(),
                request.form.get("enquiry_date", "").strip() or today_ist().isoformat(),
                request.form.get("partner_name", "").strip(),
                session["user_id"],
                ts,
                ts,
            ),
        )
        eid = cur.lastrowid

        items = parsed_items
        if not items:
            # Nobody filled in even one product, keep the old single-field
            # behaviour so this still works exactly like before for the
            # common case of one product.
            items = [{
                "product_category": "", "specs": "", "rate": None,
                "rate_unit": "", "quantity": 1,
            }]
        for item in items:
            db.execute(
                """INSERT INTO enquiry_items
                   (enquiry_id, product_category, specs, rate, rate_unit, quantity, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (eid, item["product_category"], item["specs"], item["rate"],
                 item["rate_unit"], item["quantity"], ts, ts),
            )
        db.commit()
        flash("Enquiry added.", "success")
        return redirect(url_for("enquiries"))
    return render_template("form.html", enquiry=None, items=[], today=today_ist().isoformat())


@app.route("/enquiry/<int:eid>")
@login_required
def enquiry_detail(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ? ORDER BY id", (eid,)).fetchall()
    items_with_stage = [{"item": i, "stage": _current_stage(i) if i["confirmed"] else None} for i in items]
    return render_template("detail.html", enquiry=enquiry, items=items_with_stage)


@app.route("/enquiry/<int:eid>/invoice", methods=["POST"])
@login_required
def enquiry_invoice_update(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    db.execute(
        "UPDATE enquiries SET tax_invoice_no = ?, tax_invoice_date = ?, updated_at = ? WHERE id = ?",
        (
            request.form.get("tax_invoice_no", "").strip(),
            request.form.get("tax_invoice_date", "").strip(),
            now_str(),
            eid,
        ),
    )
    db.commit()
    flash("Tax invoice number updated.", "success")
    return redirect(url_for("enquiry_detail", eid=eid))


@app.route("/enquiry/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def enquiry_edit(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    if request.method == "POST":
        parsed_items, error = _check_enquiry_form(request.form)
        if error:
            flash(error + " Nothing was saved, please try again.", "error")
            return redirect(url_for("enquiry_edit", eid=eid))
        db.execute(
            """UPDATE enquiries SET customer_name=?, company_name=?, phone=?, email=?,
               delivery_date=?, enquiry_date=?, partner_name=?, updated_at=? WHERE id=?""",
            (
                request.form.get("customer_name", "").strip(),
                request.form.get("company_name", "").strip(),
                request.form.get("phone", "").strip(),
                request.form.get("email", "").strip(),
                request.form.get("delivery_date", "").strip(),
                request.form.get("enquiry_date", "").strip() or enquiry["enquiry_date"] or today_ist().isoformat(),
                request.form.get("partner_name", "").strip(),
                now_str(),
                eid,
            ),
        )
        ts = now_str()
        for item in parsed_items:
            if item["item_id"]:
                if item["delete"]:
                    db.execute("DELETE FROM enquiry_items WHERE id = ? AND enquiry_id = ?", (item["item_id"], eid))
                else:
                    db.execute(
                        """UPDATE enquiry_items SET product_category=?, specs=?, rate=?, rate_unit=?,
                           quantity=?, updated_at=? WHERE id=? AND enquiry_id=?""",
                        (item["product_category"], item["specs"], item["rate"], item["rate_unit"],
                         item["quantity"], ts, item["item_id"], eid),
                    )
            elif not item["delete"]:
                db.execute(
                    """INSERT INTO enquiry_items
                       (enquiry_id, product_category, specs, rate, rate_unit, quantity, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (eid, item["product_category"], item["specs"], item["rate"],
                     item["rate_unit"], item["quantity"], ts, ts),
                )
        db.commit()
        flash("Enquiry updated.", "success")
        return redirect(url_for("enquiry_detail", eid=eid))
    items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ? ORDER BY id", (eid,)).fetchall()
    return render_template("form.html", enquiry=enquiry, items=items, today=today_ist().isoformat())


@app.route("/enquiry/<int:eid>/delete", methods=["POST"])
@login_required
@admin_required
def enquiry_delete(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    db.execute("DELETE FROM enquiry_items WHERE enquiry_id = ?", (eid,))
    db.execute("DELETE FROM orders WHERE enquiry_id = ?", (eid,))
    db.execute("DELETE FROM enquiries WHERE id = ?", (eid,))
    db.commit()
    flash(f"Deleted enquiry for {enquiry['customer_name']}.", "success")
    return redirect(url_for("enquiries"))


# ---------------------------------------------------------------------------
# Orders (payment tracking) - per product line, with a bulk "whole order" action
# ---------------------------------------------------------------------------

STAGE_ORDER = ["confirmed", "advance", "dispatched", "balance"]
STAGE_RANK = {"confirmed": 0, "advance": 1, "dispatched": 2, "balance": 3}
STAGE_LABEL = {"confirmed": "Confirmed", "advance": "Advance", "dispatched": "Dispatched", "balance": "Balance"}
NEXT_ACTION_LABEL = {
    "confirmed": "Mark advance received",
    "advance": "Mark dispatched",
    "dispatched": "Mark balance received",
    "balance": None,
}


def _current_stage(item):
    """Which pipeline stage an already-confirmed item is currently sitting at,
    waiting to move to the next one. Works on any row with the 4 stage flags,
    an enquiry_items row here."""
    if not item["balance_received"]:
        if not item["dispatched"]:
            if not item["advance_received"]:
                return "confirmed"
            return "advance"
        return "dispatched"
    return "balance"


def _advance_item(db, item, ts):
    stage = _current_stage(item)
    if stage == "confirmed":
        db.execute("UPDATE enquiry_items SET advance_received=1, advance_received_date=?, updated_at=? WHERE id=?", (ts, ts, item["id"]))
    elif stage == "advance":
        db.execute("UPDATE enquiry_items SET dispatched=1, dispatched_date=?, updated_at=? WHERE id=?", (ts, ts, item["id"]))
    elif stage == "dispatched":
        db.execute("UPDATE enquiry_items SET balance_received=1, balance_received_date=?, updated_at=? WHERE id=?", (ts, ts, item["id"]))


@app.route("/enquiry/<int:eid>/item/<int:item_id>/order", methods=["POST"])
@login_required
def item_order_update(eid, item_id):
    db = get_db()
    item = db.execute("SELECT * FROM enquiry_items WHERE id = ? AND enquiry_id = ?", (item_id, eid)).fetchone()
    if item is None:
        flash("Product line not found.", "error")
        return redirect(url_for("enquiry_detail", eid=eid))

    confirmed = 1 if request.form.get("confirmed") else 0
    advance_received = 1 if request.form.get("advance_received") else 0
    dispatched = 1 if request.form.get("dispatched") else 0
    balance_received = 1 if request.form.get("balance_received") else 0

    # Forward cascade: marking a later stage implies every earlier stage happened
    # too, even if someone forgot to tick the earlier boxes for this line.
    if balance_received:
        dispatched = 1
    if dispatched:
        advance_received = 1
    if advance_received:
        confirmed = 1

    ts = now_str()
    db.execute(
        """UPDATE enquiry_items SET confirmed=?, confirmed_date=?, advance_received=?, advance_received_date=?,
           dispatched=?, dispatched_date=?, balance_received=?, balance_received_date=?, updated_at=?
           WHERE id=?""",
        (
            confirmed,
            ts if confirmed and not item["confirmed"] else item["confirmed_date"],
            advance_received,
            ts if advance_received and not item["advance_received"] else item["advance_received_date"],
            dispatched,
            ts if dispatched and not item["dispatched"] else item["dispatched_date"],
            balance_received,
            ts if balance_received and not item["balance_received"] else item["balance_received_date"],
            ts,
            item["id"],
        ),
    )
    db.commit()
    flash("Product line status updated.", "success")
    return redirect(url_for("enquiry_detail", eid=eid))


@app.route("/enquiry/<int:eid>/item/<int:item_id>/order/advance-stage", methods=["POST"])
@login_required
def item_order_advance_stage(eid, item_id):
    db = get_db()
    item = db.execute("SELECT * FROM enquiry_items WHERE id = ? AND enquiry_id = ?", (item_id, eid)).fetchone()
    if item is not None:
        _advance_item(db, item, now_str())
        db.commit()
    return redirect(_safe_next(request.form.get("next"), url_for("enquiry_detail", eid=eid)))


@app.route("/enquiry/<int:eid>/order/advance-all", methods=["POST"])
@login_required
def order_advance_all(eid):
    """Bulk action: move every confirmed product line in this order forward
    one step, the "advance the entire order" option, as opposed to advancing
    one product line at a time from the enquiry detail page."""
    db = get_db()
    items = db.execute(
        "SELECT * FROM enquiry_items WHERE enquiry_id = ? AND confirmed = 1", (eid,)
    ).fetchall()
    if items:
        bottleneck = min(STAGE_RANK[_current_stage(i)] for i in items)
        ts = now_str()
        for i in items:
            if STAGE_RANK[_current_stage(i)] == bottleneck:
                _advance_item(db, i, ts)
        db.commit()
    return redirect(url_for("orders_page"))


def _query_orders(show_completed, sort, sort_dir):
    db = get_db()
    enquiry_rows = db.execute(
        """SELECT DISTINCT e.* FROM enquiries e
           JOIN enquiry_items i ON i.enquiry_id = e.id
           WHERE i.confirmed = 1"""
    ).fetchall()

    today_str = today_ist().isoformat()
    items = []
    for e in enquiry_rows:
        all_items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ?", (e["id"],)).fetchall()
        confirmed_items = [i for i in all_items if i["confirmed"]]
        stage = min((STAGE_RANK[_current_stage(i)] for i in confirmed_items), default=0)
        stage = STAGE_ORDER[stage]
        if stage == "balance" and not show_completed:
            continue
        overdue = (
            e["delivery_date"]
            and e["delivery_date"] < today_str
            and any(not i["balance_received"] for i in confirmed_items)
        )
        unconfirmed_items = [i for i in all_items if not i["confirmed"]]
        product_summary = ", ".join(i["product_category"] for i in confirmed_items if i["product_category"]) or "-"
        if unconfirmed_items:
            unconfirmed_names = ", ".join(i["product_category"] or "product" for i in unconfirmed_items)
            product_summary += f" (+ {unconfirmed_names}, not yet confirmed)"
        items.append({
            "row": e,
            "overdue": overdue,
            "stage": stage,
            "stage_label": STAGE_LABEL[stage],
            "next_label": NEXT_ACTION_LABEL[stage],
            "product_summary": product_summary,
            "item_count": len(confirmed_items),
            "has_unconfirmed": bool(unconfirmed_items),
            "mixed_stage": len({_current_stage(i) for i in confirmed_items}) > 1,
        })

    sort_keys = {
        "customer": lambda i: (i["row"]["customer_name"] or "").lower(),
        "product": lambda i: i["product_summary"].lower(),
        "delivery": lambda i: i["row"]["delivery_date"] or "9999-99-99",
        "stage": lambda i: STAGE_RANK[i["stage"]],
    }
    if sort in sort_keys:
        items.sort(key=sort_keys[sort], reverse=(sort_dir == "desc"))
    else:
        # Default: active stages first, then earliest delivery date within a stage.
        items.sort(key=lambda i: (STAGE_RANK[i["stage"]], i["row"]["delivery_date"] or "9999-99-99"))
    return items


@app.route("/orders")
@login_required
def orders_page():
    show_completed = request.args.get("show_completed") == "1"
    sort = request.args.get("sort", "")
    sort_dir = request.args.get("dir", "asc")
    items = _query_orders(show_completed, sort, sort_dir)

    def sort_link(col):
        next_dir = "desc" if sort == col and sort_dir == "asc" else "asc"
        return url_for("orders_page", show_completed=1 if show_completed else None, sort=col, dir=next_dir)

    return render_template(
        "orders.html", items=items, show_completed=show_completed,
        sort=sort, sort_dir=sort_dir, sort_link=sort_link,
    )


@app.route("/orders/export")
@login_required
def orders_export():
    show_completed = request.args.get("show_completed") == "1"
    sort = request.args.get("sort", "")
    sort_dir = request.args.get("dir", "asc")
    items = _query_orders(show_completed, sort, sort_dir)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Customer", "Company", "Products", "Delivery Date", "Stage", "Overdue", "Tax Invoice No.", "Tax Invoice Date"])
    for item in items:
        e = item["row"]
        writer.writerow([_csv_cell(v) for v in [
            e["customer_name"], e["company_name"] or "", item["product_summary"],
            e["delivery_date"] or "", item["stage_label"], "Yes" if item["overdue"] else "No",
            e["tax_invoice_no"] or "", e["tax_invoice_date"] or "",
        ]])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

PERIODS = {
    "today": "Today",
    "week": "Last 7 days",
    "month": "This month",
    "year": "This year",
    "all": "All time",
}


def _period_range(period, today):
    if period == "today":
        return today, today
    if period == "week":
        return today - timedelta(days=6), today
    if period == "month":
        return today.replace(day=1), today
    if period == "year":
        return today.replace(month=1, day=1), today
    return None, None  # "all"


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    period = request.args.get("period", "month")
    custom_from = request.args.get("from", "").strip()
    custom_to = request.args.get("to", "").strip()
    today = today_ist()

    if period == "custom" and custom_from and custom_to:
        date_clause = "WHERE e.enquiry_date BETWEEN ? AND ?"
        date_params = (custom_from, custom_to)
    else:
        if period not in PERIODS:
            period = "month"
        date_from, date_to = _period_range(period, today)
        if date_from:
            date_clause = "WHERE e.enquiry_date BETWEEN ? AND ?"
            date_params = (date_from.isoformat(), date_to.isoformat())
        else:
            date_clause = ""
            date_params = ()

    advance_pct = _advance_pct(get_settings())

    total_enquiries = db.execute(
        f"SELECT COUNT(*) c FROM enquiries e {date_clause}", date_params
    ).fetchone()["c"]

    confirmed_count = db.execute(
        f"""SELECT COUNT(DISTINCT e.id) c FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause}{' AND' if date_clause else 'WHERE'} i.confirmed = 1""",
        date_params,
    ).fetchone()["c"]

    confirmed_value = db.execute(
        f"""SELECT COALESCE(SUM(i.rate * i.quantity), 0) v FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause}{' AND' if date_clause else 'WHERE'} i.confirmed = 1""",
        date_params,
    ).fetchone()["v"]

    pending_advance = db.execute(
        f"""SELECT COUNT(*) c, COALESCE(SUM(i.rate * i.quantity), 0) amt
            FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause}{' AND' if date_clause else 'WHERE'} i.confirmed = 1 AND i.advance_received = 0""",
        date_params,
    ).fetchone()

    pending_balance = db.execute(
        f"""SELECT COUNT(*) c, COALESCE(SUM(i.rate * i.quantity), 0) amt
            FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause}{' AND' if date_clause else 'WHERE'} i.dispatched = 1 AND i.balance_received = 0""",
        date_params,
    ).fetchone()

    overdue_rows = db.execute(
        f"""SELECT i.rate, i.quantity FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause}{' AND' if date_clause else 'WHERE'} i.confirmed = 1 AND i.balance_received = 0
            AND e.delivery_date IS NOT NULL AND e.delivery_date != '' AND e.delivery_date < ?""",
        date_params + (today.isoformat(),),
    ).fetchall()
    overdue_count = len(overdue_rows)
    overdue_amount = round(sum(_to_float(r["rate"]) * (_to_float(r["quantity"], 1.0) or 1.0) for r in overdue_rows), 2)

    conversion_rate = 0
    if total_enquiries:
        conversion_rate = round(100 * confirmed_count / total_enquiries, 1)

    # Average quote value = average of each enquiry's total across all its
    # product lines, not the average of individual line items.
    per_enquiry_totals = db.execute(
        f"""SELECT SUM(i.rate * i.quantity) total FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause} GROUP BY e.id""",
        date_params,
    ).fetchall()
    totals = [r["total"] for r in per_enquiry_totals if r["total"] is not None]
    avg_quote_value = round(sum(totals) / len(totals), 2) if totals else 0

    by_category = db.execute(
        f"""SELECT COALESCE(NULLIF(TRIM(i.product_category), ''), 'Uncategorized') cat, COUNT(*) c
            FROM enquiries e JOIN enquiry_items i ON i.enquiry_id = e.id
            {date_clause} GROUP BY cat ORDER BY c DESC""",
        date_params,
    ).fetchall()
    max_cat = max([r["c"] for r in by_category], default=1)

    stats = {
        "total_enquiries": total_enquiries,
        "confirmed_count": confirmed_count,
        "confirmed_value": round(confirmed_value, 2),
        "pending_advance_count": pending_advance["c"],
        "pending_advance_amount": round(pending_advance["amt"] * advance_pct / 100, 2),
        "pending_balance_count": pending_balance["c"],
        "pending_balance_amount": round(pending_balance["amt"] * (100 - advance_pct) / 100, 2),
        "overdue_count": overdue_count,
        "overdue_amount": overdue_amount,
        "conversion_rate": conversion_rate,
        "avg_quote_value": avg_quote_value,
    }
    return render_template(
        "dashboard.html", stats=stats, by_category=by_category, max_cat=max_cat,
        period=period, periods=PERIODS, custom_from=custom_from, custom_to=custom_to,
    )


# ---------------------------------------------------------------------------
# Settings (admin only)
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings_page():
    db = get_db()
    if request.method == "POST":
        try:
            pct = float(request.form.get("advance_percent", "").strip())
            days = int(request.form.get("balance_days", "").strip())
            if not 0 <= pct <= 100 or days < 0:
                raise ValueError
        except ValueError:
            flash("Advance must be a number from 0 to 100 and balance days a whole number. Nothing was saved.", "error")
            return redirect(url_for("settings_page"))
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


@app.route("/settings/backup")
@login_required
@admin_required
def settings_backup():
    """Download a consistent copy of the whole database (admin only)."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        os.remove(tmp_path)
    stamp = datetime.now(IST).strftime("%Y-%m-%d_%H%M")
    return Response(
        data,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=vardhaman-backup-{stamp}.db"},
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def build_quote_text(enquiry, items, settings):
    advance_pct = _advance_pct(settings)

    product_lines = []
    grand_total = 0
    for item in items:
        rate = _to_float(item["rate"])
        quantity = _to_float(item["quantity"], 1.0) or 1.0
        rate = int(rate) if rate == int(rate) else rate
        quantity = int(quantity) if quantity == int(quantity) else quantity
        subtotal = round(rate * quantity, 2)
        grand_total += subtotal
        raw_unit = (item["rate_unit"] or "unit").strip()
        unit_noun = raw_unit[4:].strip() if raw_unit.lower().startswith("per ") else raw_unit
        product_lines.append(f"- {item['product_category'] or 'Product'}: {quantity} {unit_noun} x Rs. {rate} = Rs. {subtotal}")
        if item["specs"]:
            product_lines.append(f"  Specs: {item['specs']}")

    grand_total = round(grand_total, 2)
    advance_amt = round(grand_total * advance_pct / 100, 2)
    balance_amt = round(grand_total - advance_amt, 2)

    lines = [
        settings.get("company_name", ""),
        settings.get("company_location", ""),
        f"GST: {settings.get('gst_number', '')}",
        "",
        f"Quote for: {enquiry['customer_name']}" + (f" ({enquiry['company_name']})" if enquiry["company_name"] else ""),
        "",
        "Products:",
        *product_lines,
        "",
        f"Total: Rs. {grand_total}",
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
    return "\n".join(lines), grand_total, advance_amt, balance_amt


@app.route("/enquiry/<int:eid>/quote")
@login_required
def enquiry_quote(eid):
    db = get_db()
    enquiry = db.execute("SELECT * FROM enquiries WHERE id = ?", (eid,)).fetchone()
    if enquiry is None:
        flash("Enquiry not found.", "error")
        return redirect(url_for("enquiries"))
    items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ? ORDER BY id", (eid,)).fetchall()
    settings = get_settings()
    quote_text, total, advance_amt, balance_amt = build_quote_text(enquiry, items, settings)
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

    items = db.execute("SELECT * FROM enquiry_items WHERE enquiry_id = ? ORDER BY id", (eid,)).fetchall()
    settings = get_settings()
    quote_text, *_ = build_quote_text(enquiry, items, settings)

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
