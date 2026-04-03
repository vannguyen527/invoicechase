"""
InvoiceChase — Automated invoice reminder agent
Phase 3: User accounts + SQLite database + Dashboard + Email reminders
"""

import os
import re
import sqlite3
import hashlib
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from functools import wraps

import stripe
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------------------------------------------------------
# App Setup
# ------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_placeholder')
stripe_pub_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_placeholder')
PRICE_AMOUNT = int(os.environ.get('PRICE_AMOUNT', 2900))  # cents

# Email (Gmail SMTP)
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', SMTP_USER)

# DB path
DB_PATH = os.environ.get('DB_PATH', '/tmp/invoicechase.db')

# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            client_email TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            due_date DATE NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'unpaid',
            paid_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            scheduled_for DATETIME NOT NULL,
            sent_at TIMESTAMP,
            reminder_type TEXT NOT NULL,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS email_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            reminder_type TEXT NOT NULL,
            subject TEXT,
            body TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def get_current_user():
    if 'user_id' in session:
        conn = get_db()
        c = conn.cursor()
        user = c.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()
        return user
    return None

# ------------------------------------------------------------------
# Email sending
# ------------------------------------------------------------------

DEFAULT_SUBJECTS = {
    'reminder_3': 'Friendly Reminder: Invoice Due',
    'reminder_7': 'Invoice Overdue — 7 Days Past Due',
    'reminder_14': 'Final Notice: Invoice 14 Days Overdue',
}

DEFAULT_BODIES = {
    'reminder_3': '''Hi {client_name},

This is a friendly reminder that your invoice is due in 3 days.

Invoice Details:
  Amount: {amount}
  Due Date: {due_date}
  Description: {description}

Please arrange payment at your earliest convenience.

Best regards,
{user_name}''',

    'reminder_7': '''Hi {client_name},

This is a follow-up regarding an invoice that is now 7 days overdue.

Invoice Details:
  Amount: {amount}
  Due Date: {due_date}
  Description: {description}

We kindly request that payment be made as soon as possible to avoid any further delays.

Best regards,
{user_name}''',

    'reminder_14': '''Hi {client_name},

This is a final notice that your invoice is now 14 days overdue.

Invoice Details:
  Amount: {amount}
  Due Date: {due_date}
  Description: {description}

Please remit payment immediately to resolve this outstanding balance.

Best regards,
{user_name}''',
}

def send_email(to_email, subject, body):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[EMAIL MOCK] To: {to_email}\nSubject: {subject}\n\n{body}")
        return True

    try:
        msg = MIMEMultipart()
        msg['From'] = FROM_EMAIL
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        print(f"[EMAIL SENT] To: {to_email} | Subject: {subject}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

def get_email_template(user_id, reminder_type):
    conn = get_db()
    template = conn.execute(
        'SELECT * FROM email_templates WHERE user_id = ? AND reminder_type = ?',
        (user_id, reminder_type)
    ).fetchone()
    conn.close()
    if template and template['subject'] and template['body']:
        return template['subject'], template['body']
    return DEFAULT_SUBJECTS.get(reminder_type, 'Invoice Reminder'), DEFAULT_BODIES.get(reminder_type, '')

# ------------------------------------------------------------------
# Reminder scheduler
# ------------------------------------------------------------------

scheduler = BackgroundScheduler()

def check_and_send_reminders():
    with app.app_context():
        now = datetime.now()
        conn = get_db()
        reminders = conn.execute('''
            SELECT r.*, i.client_name, i.client_email, i.amount, i.due_date,
                   i.description, i.status, u.name as user_name, u.email as user_email, i.user_id
            FROM reminders r
            JOIN invoices i ON r.invoice_id = i.id
            JOIN users u ON i.user_id = u.id
            WHERE r.sent_at IS NULL
            AND r.scheduled_for <= ?
            AND i.status = 'unpaid'
        ''', (now,)).fetchall()
        conn.close()

        for r in reminders:
            subject, body_template = get_email_template(r['user_id'], r['reminder_type'])
            body = body_template.format(
                client_name=r['client_name'],
                amount=f"${r['amount']:.2f}",
                due_date=r['due_date'],
                description=r['description'] or '',
                user_name=r['user_name'] or r['user_email']
            )
            if send_email(r['client_email'], subject, body):
                conn2 = get_db()
                conn2.execute('UPDATE reminders SET sent_at = ? WHERE id = ?',
                              (datetime.now(), r['id']))
                conn2.commit()
                conn2.close()

# Run every 15 minutes
scheduler.add_job(func=check_and_send_reminders, trigger='interval', minutes=15, id='reminder_check')
scheduler.start()

# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

# ---- Landing ----
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html', stripe_pub_key=stripe_pub_key)

# ---- Auth ----
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        name = request.form.get('name', '').strip()

        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            flash('Valid email required', 'error')
            return render_template('register.html')

        if len(password) < 8:
            flash('Password must be at least 8 characters', 'error')
            return render_template('register.html')

        conn = get_db()
        existing = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
        if existing:
            conn.close()
            flash('An account with this email already exists', 'error')
            return render_template('register.html')

        password_hash = hash_password(password)
        try:
            cur = conn.execute(
                'INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)',
                (email, password_hash, name)
            )
            conn.commit()
            user_id = cur.lastrowid
            conn.close()

            session['user_id'] = user_id
            flash('Account created! Add your first invoice.', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            conn.close()
            flash('Something went wrong. Try again.', 'error')
            return render_template('register.html')

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()

        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            flash(f'Welcome back, {user["name"] or user["email"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

# ---- Dashboard ----
@app.route('/dashboard')
@login_required
def dashboard():
    user = get_current_user()
    conn = get_db()
    invoices = conn.execute('''
        SELECT * FROM invoices WHERE user_id = ? ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()

    total_outstanding = sum(inv['amount'] for inv in invoices if inv['status'] == 'unpaid')
    overdue_count = sum(1 for inv in invoices if inv['status'] == 'unpaid' and inv['due_date'] < str(date.today()))

    return render_template('dashboard.html',
                           user=user,
                           invoices=invoices,
                           total_outstanding=total_outstanding,
                           overdue_count=overdue_count,
                           today=str(date.today()))

# ---- Invoices ----
@app.route('/invoices/add', methods=['GET', 'POST'])
@login_required
def add_invoice():
    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        client_email = request.form.get('client_email', '').strip()
        amount = request.form.get('amount', '')
        due_date = request.form.get('due_date', '')
        description = request.form.get('description', '').strip()

        if not all([client_name, client_email, amount, due_date]):
            flash('Please fill in all required fields', 'error')
            return render_template('add_invoice.html')

        try:
            amount = float(amount)
        except ValueError:
            flash('Amount must be a number', 'error')
            return render_template('add_invoice.html')

        due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()

        conn = get_db()
        cur = conn.execute('''
            INSERT INTO invoices (user_id, client_name, client_email, amount, due_date, description)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session['user_id'], client_name, client_email, amount, due_date, description))
        invoice_id = cur.lastrowid
        conn.commit()
        conn.close()

        # Schedule reminders
        schedule_reminders(invoice_id, due_date_obj)

        flash('Invoice added! Reminders scheduled automatically.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('add_invoice.html')

def schedule_reminders(invoice_id, due_date):
    """Schedule 3/7/14 day overdue reminders"""
    conn = get_db()

    # Delete any existing unsent reminders for this invoice
    conn.execute('DELETE FROM reminders WHERE invoice_id = ? AND sent_at IS NULL', (invoice_id,))

    reminders_to_schedule = [
        ('reminder_3', due_date + timedelta(days=3)),
        ('reminder_7', due_date + timedelta(days=7)),
        ('reminder_14', due_date + timedelta(days=14)),
    ]

    for reminder_type, sched_date in reminders_to_schedule:
        conn.execute('''
            INSERT INTO reminders (invoice_id, scheduled_for, reminder_type)
            VALUES (?, ?, ?)
        ''', (invoice_id, sched_date, reminder_type))

    conn.commit()
    conn.close()

@app.route('/invoices/<int:invoice_id>/paid', methods=['POST'])
@login_required
def mark_paid(invoice_id):
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id = ? AND user_id = ?',
                            (invoice_id, session['user_id'])).fetchone()
    if not invoice:
        conn.close()
        flash('Invoice not found', 'error')
        return redirect(url_for('dashboard'))

    # Cancel pending reminders
    conn.execute('DELETE FROM reminders WHERE invoice_id = ? AND sent_at IS NULL', (invoice_id,))
    conn.execute('UPDATE invoices SET status = ?, paid_at = ? WHERE id = ?',
                  ('paid', datetime.now(), invoice_id))
    conn.commit()
    conn.close()

    flash('Invoice marked as paid!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@login_required
def delete_invoice(invoice_id):
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id = ? AND user_id = ?',
                            (invoice_id, session['user_id'])).fetchone()
    if not invoice:
        conn.close()
        flash('Invoice not found', 'error')
        return redirect(url_for('dashboard'))

    conn.execute('DELETE FROM reminders WHERE invoice_id = ?', (invoice_id,))
    conn.execute('DELETE FROM invoices WHERE id = ?', (invoice_id,))
    conn.commit()
    conn.close()

    flash('Invoice deleted.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/invoices/<int:invoice_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_invoice(invoice_id):
    conn = get_db()
    invoice = conn.execute('SELECT * FROM invoices WHERE id = ? AND user_id = ?',
                            (invoice_id, session['user_id'])).fetchone()
    conn.close()

    if not invoice:
        flash('Invoice not found', 'error')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        client_email = request.form.get('client_email', '').strip()
        amount = request.form.get('amount', '')
        due_date = request.form.get('due_date', '')
        description = request.form.get('description', '').strip()

        try:
            amount = float(amount)
        except ValueError:
            flash('Amount must be a number', 'error')
            return render_template('edit_invoice.html', invoice=invoice)

        due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()

        conn2 = get_db()
        conn2.execute('''
            UPDATE invoices SET client_name=?, client_email=?, amount=?, due_date=?, description=?
            WHERE id = ?
        ''', (client_name, client_email, amount, due_date, description, invoice_id))
        conn2.commit()
        conn2.close()

        # Reschedule reminders if due date changed
        if due_date_obj != datetime.strptime(invoice['due_date'], '%Y-%m-%d').date():
            schedule_reminders(invoice_id, due_date_obj)

        flash('Invoice updated!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('edit_invoice.html', invoice=invoice)

# ---- Email Templates ----
@app.route('/settings/email', methods=['GET', 'POST'])
@login_required
def email_settings():
    user = get_current_user()
    conn = get_db()

    if request.method == 'POST':
        for reminder_type in ['reminder_3', 'reminder_7', 'reminder_14']:
            subject = request.form.get(f'subject_{reminder_type}', '').strip()
            body = request.form.get(f'body_{reminder_type}', '').strip()
            existing = conn.execute(
                'SELECT id FROM email_templates WHERE user_id = ? AND reminder_type = ?',
                (session['user_id'], reminder_type)
            ).fetchone()
            if existing:
                conn.execute('''
                    UPDATE email_templates SET subject=?, body=? WHERE id=?
                ''', (subject, body, existing['id']))
            else:
                conn.execute('''
                    INSERT INTO email_templates (user_id, reminder_type, subject, body)
                    VALUES (?, ?, ?, ?)
                ''', (session['user_id'], reminder_type, subject, body))
        conn.commit()
        flash('Email templates saved!', 'success')

    templates = {}
    for rt in ['reminder_3', 'reminder_7', 'reminder_14']:
        t = conn.execute(
            'SELECT * FROM email_templates WHERE user_id = ? AND reminder_type = ?',
            (session['user_id'], rt)
        ).fetchone()
        if t:
            templates[rt] = {'subject': t['subject'], 'body': t['body']}
        else:
            templates[rt] = {'subject': DEFAULT_SUBJECTS.get(rt, ''), 'body': DEFAULT_BODIES.get(rt, '')}

    conn.close()
    return render_template('email_settings.html', templates=templates, user=user)

# ---- Checkout ----
@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        email = request.form.get('email', '').strip()
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            return jsonify({'error': 'Valid email required'}), 400

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='payment',
            customer_email=email,
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'unit_amount': PRICE_AMOUNT,
                    'product_data': {
                        'name': 'InvoiceChase',
                        'description': 'Automated invoice reminder agent — one-time purchase',
                    },
                },
                'quantity': 1,
            }],
            success_url=url_for('payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('index', _external=True),
            metadata={'email': email}
        )
        return redirect(checkout_session.url, code=303)
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 400

@app.route('/payment-success')
def payment_success():
    session_id = request.args.get('session_id', '')
    email = ''
    if session_id:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            email = sess.get('customer_email', '')
        except:
            pass
    return render_template('payment_success.html', email=email)

# ---- Success page (existing /success) ----
@app.route('/success')
def success():
    session_id = request.args.get('session_id', '')
    if session_id:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            email = sess.get('customer_email', '')
            return render_template('success.html', email=email)
        except:
            pass
    return render_template('success.html', email='')

# ---- Webhook ----
@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig = request.headers.get('Stripe-Signature', '')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    try:
        if endpoint_secret and sig:
            event = stripe.Webhook.construct_event(payload, sig, endpoint_secret)
        else:
            event = stripe.util.convert_to_stripe_object(payload)
    except ValueError:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        sess = event['data']['object']
        email = sess.get('customer_email', '')
        print(f"PAYMENT: {email} paid ${PRICE_AMOUNT/100}")

    return '', 200

# ---- Ping ----
@app.route('/ping')
def ping():
    return 'ok', 200

# ---- Terms / Privacy ----
@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# ------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
