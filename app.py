from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'vardhaman-2024-secret-change-in-production')

DATABASE = 'vardhaman.db'

def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS enquiries (
            id INTEGER PRIMARY KEY,
            customer_name TEXT NOT NULL,
            company_name TEXT,
            phone TEXT NOT NULL,
            email TEXT,
            product_category TEXT,
            specs TEXT,
            delivery_date TEXT,
            rate REAL,
            rate_unit TEXT,
            partner_name TEXT,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            enquiry_id INTEGER UNIQUE,
            status TEXT DEFAULT 'quote_sent',
            advance_received INTEGER DEFAULT 0,
            advance_received_date TEXT,
            balance_received INTEGER DEFAULT 0,
            balance_received_date TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(enquiry_id) REFERENCES enquiries(id)
        );
        ''')
        db.commit()

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Routes

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('list_enquiries'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')
        
        if not email or not password or not name:
            return render_template('register.html', error='All fields required')
        
        db = get_db()
        try:
            db.execute(
                'INSERT INTO users (email, password, name) VALUES (?, ?, ?)',
                (email, generate_password_hash(password), name)
            )
            db.commit()
            session['user_id'] = db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()[0]
            session['user_name'] = name
            return redirect(url_for('list_enquiries'))
        except sqlite3.IntegrityError:
            return render_template('register.html', error='Email already exists')
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['user_name'] = user['name']
            return redirect(url_for('list_enquiries'))
        
        return render_template('login.html', error='Invalid credentials')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/enquiries')
@login_required
def list_enquiries():
    db = get_db()
    search = request.args.get('search', '')
    
    if search:
        enquiries = db.execute(
            'SELECT * FROM enquiries WHERE customer_name LIKE ? OR company_name LIKE ? ORDER BY created_at DESC',
            (f'%{search}%', f'%{search}%')
        ).fetchall()
    else:
        enquiries = db.execute('SELECT * FROM enquiries ORDER BY created_at DESC').fetchall()
    
    return render_template('list.html', enquiries=enquiries, search=search)

@app.route('/enquiry/add', methods=['GET', 'POST'])
@login_required
def add_enquiry():
    if request.method == 'POST':
        db = get_db()
        db.execute(
            '''INSERT INTO enquiries 
            (customer_name, company_name, phone, email, product_category, specs, delivery_date, rate, rate_unit, partner_name, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                request.form.get('customer_name'),
                request.form.get('company_name'),
                request.form.get('phone'),
                request.form.get('email'),
                request.form.get('product_category'),
                request.form.get('specs'),
                request.form.get('delivery_date'),
                request.form.get('rate') or None,
                request.form.get('rate_unit') or 'nos.',
                request.form.get('partner_name'),
                session['user_id']
            )
        )
        db.commit()
        return redirect(url_for('list_enquiries'))
    
    return render_template('form.html')

@app.route('/enquiry/<int:id>')
@login_required
def view_enquiry(id):
    db = get_db()
    enquiry = db.execute('SELECT * FROM enquiries WHERE id = ?', (id,)).fetchone()
    order = db.execute('SELECT * FROM orders WHERE enquiry_id = ?', (id,)).fetchone()
    
    if not enquiry:
        return redirect(url_for('list_enquiries'))
    
    return render_template('detail.html', enquiry=enquiry, order=order)

@app.route('/enquiry/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def edit_enquiry(id):
    db = get_db()
    enquiry = db.execute('SELECT * FROM enquiries WHERE id = ?', (id,)).fetchone()
    
    if not enquiry:
        return redirect(url_for('list_enquiries'))
    
    if request.method == 'POST':
        db.execute(
            '''UPDATE enquiries 
            SET customer_name=?, company_name=?, phone=?, email=?, product_category=?, specs=?, 
                delivery_date=?, rate=?, rate_unit=?, partner_name=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?''',
            (
                request.form.get('customer_name'),
                request.form.get('company_name'),
                request.form.get('phone'),
                request.form.get('email'),
                request.form.get('product_category'),
                request.form.get('specs'),
                request.form.get('delivery_date'),
                request.form.get('rate') or None,
                request.form.get('rate_unit') or 'nos.',
                request.form.get('partner_name'),
                id
            )
        )
        db.commit()
        return redirect(url_for('view_enquiry', id=id))
    
    return render_template('form.html', enquiry=enquiry)

@app.route('/enquiry/<int:id>/quote')
@login_required
def generate_quote(id):
    db = get_db()
    enquiry = db.execute('SELECT * FROM enquiries WHERE id = ?', (id,)).fetchone()
    
    if not enquiry:
        return redirect(url_for('list_enquiries'))
    
    rate = enquiry['rate'] or 0
    advance = round(rate * 0.25)
    balance = round(rate * 0.75)
    
    quote_text = f"""QUOTE

Vardhaman Elastomer LLP
Matunga, Mumbai
GST: 27AAWFV2676B1ZP

Bill To:
{enquiry['customer_name']}
{enquiry['company_name'] or ''}
{enquiry['phone']}

Product Details:
Product: {enquiry['product_category']}
Specifications: {enquiry['specs']}
Delivery Timeline: {enquiry['delivery_date']}

Pricing:
Rate: ₹{enquiry['rate']}
Advance (25%): ₹{advance}
Balance: ₹{balance}

Payment Terms:
25% advance payment to proceed with order
Balance payment due 30 days post-delivery

Bank Details:
[Add bank details in admin settings]

Generated: {datetime.now().strftime('%d-%m-%Y')}"""
    
    return render_template('quote.html', 
        enquiry=enquiry, 
        quote_text=quote_text,
        advance=advance,
        balance=balance
    )

@app.route('/enquiry/<int:id>/order', methods=['POST'])
@login_required
def update_order(id):
    db = get_db()
    
    advance_received = request.form.get('advance_received') == 'on'
    balance_received = request.form.get('balance_received') == 'on'
    
    existing = db.execute('SELECT * FROM orders WHERE enquiry_id = ?', (id,)).fetchone()
    
    if existing:
        db.execute(
            '''UPDATE orders 
            SET advance_received=?, advance_received_date=?, balance_received=?, 
                balance_received_date=?, updated_at=CURRENT_TIMESTAMP
            WHERE enquiry_id=?''',
            (
                1 if advance_received else 0,
                datetime.now().strftime('%Y-%m-%d') if advance_received else None,
                1 if balance_received else 0,
                datetime.now().strftime('%Y-%m-%d') if balance_received else None,
                id
            )
        )
    else:
        db.execute(
            '''INSERT INTO orders 
            (enquiry_id, advance_received, advance_received_date, balance_received, balance_received_date)
            VALUES (?, ?, ?, ?, ?)''',
            (
                id,
                1 if advance_received else 0,
                datetime.now().strftime('%Y-%m-%d') if advance_received else None,
                1 if balance_received else 0,
                datetime.now().strftime('%Y-%m-%d') if balance_received else None
            )
        )
    
    db.commit()
    return redirect(url_for('view_enquiry', id=id))

if __name__ == '__main__':
    init_db()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
