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
from apscheduler.triggers.interval import IntervalTrigger

# ------------------------------------------------------------------
# App Setup
# ------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_placeholder')
stripe_pub_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_placeholder')
PRICE_AMOUNT = int(os.environ.get('PRICE_AMOUNT') or 2900)  # cents

# Email (Gmail SMTP)
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT') or 587)
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
FROM_EMAIL = os.environ.get('FROM_EMAIL', SMTP_USER)

# DB path — use /tmp on Render free tier (survives worker restarts within same instance)
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            user_id INTEGER,
            actor_email TEXT,
            target_type TEXT,
            target_id INTEGER,
            metadata TEXT,
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            assigned_to TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS paid_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            stripe_session_id TEXT,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            activated INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0
        )
    ''')
    # Migration: add activated to paid_users if it doesn't exist (for existing records)
    try:
        c.execute("ALTER TABLE paid_users ADD COLUMN activated INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE paid_users ADD COLUMN notified INTEGER DEFAULT 0")
    except Exception:
        pass
    c.execute('''
        CREATE TABLE IF NOT EXISTS ticket_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            author_type TEXT NOT NULL,
            author_id INTEGER,
            author_email TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (ticket_id) REFERENCES support_tickets(id)
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

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr, '').split(',')[0].strip()

def log_audit(event_type, user_id=None, actor_email=None, target_type=None,
              target_id=None, metadata=None, ip_address=None):
    """Write an audit log entry. metadata should be a dict."""
    try:
        import json
        meta_str = json.dumps(metadata) if metadata is not None else None
        conn = get_db()
        conn.execute('''
            INSERT INTO audit_log (event_type, user_id, actor_email, target_type,
                                   target_id, metadata, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (event_type, user_id, actor_email, target_type, target_id, meta_str, ip_address))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AUDIT ERROR] {e}")

AUDIT_EVENTS = {
    'user_registered', 'user_login', 'user_logout', 'user_deleted',
    'invoice_created', 'invoice_updated', 'invoice_deleted', 'invoice_marked_paid',
    'reminder_sent', 'payment_received', 'ticket_created', 'ticket_replied',
    'ticket_status_changed', 'template_updated',
}

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

This is a friendly reminder that your invoice is now overdue.

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

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        print(f"[EMAIL SENT] To: {to_email} | Subject: {subject}")
        return True
    except smtplib.SMTPServerDisconnected as e:
        print(f"[EMAIL ERROR] SMTP server disconnected: {e} — check SMTP_HOST/USER/PASS")
        return False
    except smtplib.SMTPAuthenticationError as e:
        print(f"[EMAIL ERROR] SMTP auth failed: {e}")
        return False
    except ConnectionRefusedError as e:
        print(f"[EMAIL ERROR] SMTP connection refused: {e}")
        return False
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def provision_paid_account(email, name=None, stripe_session_id=None):
    """
    Creates a user account after payment and sends welcome email with credentials.
    Idempotent — safe to call multiple times for the same email.
    Returns (user_id, password) on success, (None, None) on failure.
    """
    import hashlib, secrets as _secrets

    temp_password = _secrets.token_urlsafe(12)
    password_hash = hashlib.sha256(temp_password.encode()).hexdigest()

    conn = get_db()
    try:
        # Check if already a paid subscriber
        existing_pu = conn.execute(
            'SELECT id, activated FROM paid_users WHERE email = ?', (email,)
        ).fetchone()

        # Check if user already exists in main users table
        existing_user = conn.execute(
            'SELECT id FROM users WHERE email = ?', (email,)
        ).fetchone()

        if existing_user:
            uid = existing_user['id']
            if existing_pu:
                conn.execute('''
                    UPDATE paid_users SET activated=1, notified=1,
                    stripe_session_id=? WHERE email=?
                ''', (stripe_session_id or '', email))
            else:
                conn.execute('''
                    INSERT INTO paid_users (email, password_hash, name, stripe_session_id, activated, notified)
                    VALUES (?, ?, ?, ?, 1, 1)
                ''', (email, password_hash, name or '', stripe_session_id or ''))
            conn.commit()
            conn.close()
            print(f"[PROVISION] Existing user {email} marked as paid subscriber, uid={uid}")
            return uid, None

        # Create new user in main users table
        conn.execute(
            'INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)',
            (email, password_hash, name or '')
        )
        user_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]

        # Record in paid_users
        if existing_pu:
            conn.execute('''
                UPDATE paid_users
                SET password_hash=?, name=?, stripe_session_id=?, activated=1, notified=1
                WHERE email=?
            ''', (password_hash, name or '', stripe_session_id or '', email))
        else:
            conn.execute('''
                INSERT INTO paid_users (email, password_hash, name, stripe_session_id, activated, notified)
                VALUES (?, ?, ?, ?, 1, 1)
            ''', (email, password_hash, name or '', stripe_session_id or ''))

        conn.commit()
        conn.close()

        # Send welcome email
        welcome_subject = "Your InvoiceChase account is ready — here's how to log in"
        welcome_body = f"""Hi {name + ',' if name else ''}

Your InvoiceChase account is now active. Here's your login:

  Email:    {email}
  Password: {temp_password}

Log in here: https://invoicechase.onrender.com/login

Once logged in, you can:
- Add your outstanding invoices (client name, email, amount, due date)
- Customize reminder email templates
- Track payment status on your dashboard

The automated reminders go out at 3, 7, and 14 days past due — polite but firm.

Questions? Reply to this email or visit https://invoicechase.onrender.com/support

— The InvoiceChase Team
"""
        send_email(email, welcome_subject, welcome_body)
        print(f"[PROVISION] Account created and welcome email sent to {email}")
        return user_id, temp_password

    except Exception as e:
        print(f"[PROVISION ERROR] {e}")
        try:
            conn.close()
        except Exception:
            pass
        return None, None


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
# Reminder scheduler — request-triggered fallback
# ------------------------------------------------------------------

_scheduler_started = False

def start_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(
            func=_run_reminder_check,
            trigger=IntervalTrigger(minutes=15),
            id='reminder_check',
            replace_existing=True
        )
        scheduler.start()
        _scheduler_started = True
        print("[SCHEDULER] Started background reminder scheduler")
    except Exception as e:
        print(f"[SCHEDULER] Could not start: {e}")

def _run_reminder_check():
    """Wrapper so we can call it outside app context"""
    with app.app_context():
        check_and_send_reminders()

def check_and_send_reminders():
    now = datetime.now()
    conn = get_db()
    print(f"[CRON DEBUG] now={now}, checking reminders...")
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
    print(f"[CRON DEBUG] found {len(reminders)} reminders to send")
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

            log_audit('reminder_sent', user_id=r['user_id'],
                      actor_email=r['user_email'],
                      target_type='invoice', target_id=r['invoice_id'],
                      metadata={'client_email': r['client_email'],
                                'reminder_type': r['reminder_type'],
                                'subject': subject},
                      ip_address=None)

# Cron endpoint — call this every 15 min via Render cron or external service
@app.route('/cron/reminders')
def cron_reminders():
    check_and_send_reminders()
    return 'ok', 200

# Also trigger reminder check on any dashboard load (passive trigger)
@app.before_request
def maybe_check_reminders():
    if request.endpoint == 'dashboard' and 'user_id' in session:
        check_and_send_reminders()

# Start scheduler in this process (single-worker dev; Render uses gunicorn single worker on free tier)
if os.environ.get('FLASK_DEBUG') == '1' or os.environ.get('RUN_SCHEDULER') == '1':
    start_scheduler()

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
            log_audit('user_registered', user_id=user_id, actor_email=email,
                      metadata={'name': name}, ip_address=get_client_ip())
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
        password=request.form.get('password', '')

        conn = get_db()
        user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
        conn.close()

        print(f"[LOGIN] email={email}, user_found={user is not None}, user_dict={dict(user) if user else None}")
        if user and user['password_hash'] == hash_password(password):
            session['user_id'] = user['id']
            log_audit('user_login', user_id=user['id'], actor_email=email,
                      ip_address=get_client_ip())
            flash(f'Welcome back, {user["name"] or user["email"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            stored_hash = user['password_hash'][:20] if user else 'N/A'
            print(f"[LOGIN] Failed — stored_hash={stored_hash}... provided_hash={hash_password(password)[:20]}...")
            flash('Invalid email or password', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    email = None
    if user_id:
        conn = get_db()
        u = conn.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
        if u:
            email = u['email']
        conn.close()
    log_audit('user_logout', user_id=user_id, actor_email=email, ip_address=get_client_ip())
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
    user = get_current_user()
    print(f"[INVOICE DEBUG] user={user['email'] if user else 'NONE'}, session.user_id={session.get('user_id')}")
    if request.method == 'POST':
        client_name = request.form.get('client_name', '').strip()
        client_email = request.form.get('client_email', '').strip()
        amount = request.form.get('amount', '')
        due_date = request.form.get('due_date', '')
        description = request.form.get('description', '').strip()
        print(f"[INVOICE DEBUG] POST client_name={client_name}, amount={amount}, due_date={due_date}")

        if not all([client_name, client_email, amount, due_date]):
            flash('Please fill in all required fields', 'error')
            return render_template('add_invoice.html', user=user)

        try:
            amount = float(amount)
        except ValueError:
            flash('Amount must be a number', 'error')
            return render_template('add_invoice.html', user=user)

        try:
            due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid due date format', 'error')
            return render_template('add_invoice.html', user=user)

        try:
            conn = get_db()
            cur = conn.execute('''
                INSERT INTO invoices (user_id, client_name, client_email, amount, due_date, description)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session['user_id'], client_name, client_email, amount, due_date, description))
            invoice_id = cur.lastrowid
            conn.commit()
            conn.close()
            print(f"[INVOICE DEBUG] created id={invoice_id}")
        except Exception as e:
            print(f"[INVOICE INSERT ERROR] {e}")
            flash(f'Database error: {e}', 'error')
            return render_template('add_invoice.html', user=user)

        try:
            schedule_reminders(invoice_id, due_date_obj)
            print(f"[INVOICE DEBUG] reminders scheduled")
        except Exception as e:
            print(f"[REMINDER SCHEDULE ERROR] {e}")

        try:
            log_audit('invoice_created', user_id=session['user_id'],
                      actor_email=user['email'] if user else None,
                      target_type='invoice', target_id=invoice_id,
                      metadata={'client_name': client_name, 'client_email': client_email,
                                'amount': amount, 'due_date': due_date},
                      ip_address=get_client_ip())
        except Exception as e:
            print(f"[AUDIT ERROR] {e}")

        flash('Invoice added! Reminders scheduled automatically.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('add_invoice.html', user=user)

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

    user = get_current_user()
    log_audit('invoice_marked_paid', user_id=session['user_id'],
              actor_email=user['email'] if user else None,
              target_type='invoice', target_id=invoice_id,
              metadata={'client_name': invoice['client_name'],
                        'client_email': invoice['client_email'],
                        'amount': invoice['amount']},
              ip_address=get_client_ip())

    flash('Invoice marked as paid!', 'success')

    # Send receipt to debtor
    send_email(
        invoice['client_email'],
        f"Payment Received — Invoice from {user['name'] or user['email']}",
        f"""Hi {invoice['client_name']},

Payment has been received. Thank you!

Invoice Details:
  Amount: ${invoice['amount']:.2f}
  Due Date: {invoice['due_date']}
  Description: {invoice['description'] or 'N/A'}

Paid on: {datetime.now().strftime('%Y-%m-%d')}

Best regards,
{user['name'] or user['email']}
"""
    )

    # Send confirmation to customer
    send_email(
        user['email'],
        f"Invoice marked as paid — {invoice['client_name']}",
        f"You marked the invoice for {invoice['client_name']} ({invoice['client_email']}) as paid. "
        f"Amount: ${invoice['amount']:.2f}. Reminders have been cancelled."
    )

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

    user = get_current_user()
    log_audit('invoice_deleted', user_id=session['user_id'],
              actor_email=user['email'] if user else None,
              target_type='invoice', target_id=invoice_id,
              metadata={'client_name': invoice['client_name'],
                        'client_email': invoice['client_email'],
                        'amount': invoice['amount']},
              ip_address=get_client_ip())

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
        amount = sess.get('amount_total', PRICE_AMOUNT) / 100
        session_id = sess.get('id', '')
        print(f"PAYMENT: {email} paid ${amount}")

        # Provision account and send welcome email with credentials
        try:
            uid, pwd = provision_paid_account(email, stripe_session_id=session_id)
            if uid:
                log_audit('account_provisioned', user_id=uid, actor_email=email,
                          metadata={'amount': amount, 'session_id': session_id}, ip_address=None)
                print(f"PROVISION: Account ready for {email}")
            else:
                print(f"PROVISION: {email} already had an active account")
        except Exception as e:
            print(f"PROVISION ERROR: {e}")

        log_audit('payment_received', user_id=None, actor_email=email,
                  metadata={'amount': amount, 'session_id': session_id,
                             'currency': sess.get('currency', 'usd')},
                  ip_address=None)

    return '', 200

# ---- Ping ----
@app.route('/ping')
def ping():
    return 'ok', 200

# ---- Test Email (remove after verifying SMTP) ----
@app.route('/test-email')
def test_email():
    test_to = request.args.get('to', FROM_EMAIL or 'your@email.com')
    sent = send_email(
        test_to,
        "InvoiceChase SMTP test",
        f"This is a test from InvoiceChase.\n\nSMTP_HOST: {SMTP_HOST}\nSMTP_PORT: {SMTP_PORT}\nSMTP_USER: {SMTP_USER}\nFROM_EMAIL: {FROM_EMAIL}\n\nIf you received this, email is working!"
    )
    return jsonify({'sent': sent, 'to': test_to}), 200

# ---- Terms / Privacy ----
@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

# ------------------------------------------------------------------
# Support Tickets
# ------------------------------------------------------------------

@app.route('/support', methods=['GET', 'POST'])
def support():
    user = get_current_user()

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        subject = request.form.get('subject', '').strip()
        body = request.form.get('body', '').strip()

        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            flash('Valid email required', 'error')
            return render_template('support.html', user=user)

        if not subject or not body:
            flash('Subject and message are required', 'error')
            return render_template('support.html', user=user)

        print(f"[SUPPORT DEBUG] user_id={session.get('user_id')}, email={email}, subject={subject[:30]}")
        conn = get_db()
        try:
            cur = conn.execute('''
                INSERT INTO support_tickets (user_id, email, subject, body)
                VALUES (?, ?, ?, ?)
            ''', (session.get('user_id'), email, subject, body))
            ticket_id = cur.lastrowid
            print(f"[SUPPORT DEBUG] ticket inserted, id={ticket_id}")
            conn.commit()
            conn.close()
        except Exception as e:
            conn.rollback()
            conn.close()
            print(f"[TICKET INSERT ERROR] {e}")
            flash(f'Database error: {e}', 'error')
            return render_template('support.html', user=user)

        try:
            log_audit('ticket_created', user_id=session.get('user_id'),
                      actor_email=email, target_type='ticket', target_id=ticket_id,
                      metadata={'subject': subject}, ip_address=get_client_ip())
            print(f"[SUPPORT DEBUG] audit logged")
        except Exception as e:
            print(f"[AUDIT CALL ERROR] {e}")

        print(f"[SUPPORT DEBUG] redirecting to support_tickets")

        flash(f'Thank you! Your message has been received. (Ticket #{ticket_id})', 'success')
        try:
            return redirect(url_for('support_tickets'))
        except Exception as e:
            print(f"[SUPPORT REDIRECT ERROR] {e}")
            return redirect(url_for('dashboard'))

    return render_template('support.html', user=user)

@app.route('/support/tickets')
@login_required
def support_tickets():
    user = get_current_user()
    conn = get_db()
    tickets = conn.execute('''
        SELECT * FROM support_tickets WHERE user_id = ? ORDER BY created_at DESC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return render_template('support_tickets.html', tickets=tickets, user=user)

@app.route('/support/tickets/<int:ticket_id>')
@login_required
def support_ticket_view(ticket_id):
    user = get_current_user()
    conn = get_db()
    ticket = conn.execute(
        'SELECT * FROM support_tickets WHERE id = ? AND user_id = ?',
        (ticket_id, session['user_id'])
    ).fetchone()
    if not ticket:
        conn.close()
        flash('Ticket not found', 'error')
        return redirect(url_for('support_tickets'))

    replies = conn.execute(
        'SELECT * FROM ticket_replies WHERE ticket_id = ? ORDER BY created_at ASC',
        (ticket_id,)
    ).fetchall()
    conn.close()
    return render_template('support_ticket_view.html',
                           ticket=ticket, replies=replies, user=user)

@app.route('/support/tickets/<int:ticket_id>/reply', methods=['POST'])
@login_required
def support_reply(ticket_id):
    body = request.form.get('body', '').strip()
    if not body:
        flash('Message cannot be empty', 'error')
        return redirect(url_for('support_ticket_view', ticket_id=ticket_id))

    conn = get_db()
    ticket = conn.execute(
        'SELECT * FROM support_tickets WHERE id = ? AND user_id = ?',
        (ticket_id, session['user_id'])
    ).fetchone()
    if not ticket:
        conn.close()
        flash('Ticket not found', 'error')
        return redirect(url_for('support_tickets'))

    conn.execute('''
        INSERT INTO ticket_replies (ticket_id, author_type, author_id, author_email, body)
        VALUES (?, ?, ?, ?, ?)
    ''', (ticket_id, 'user', session['user_id'], get_current_user()['email'], body))
    conn.commit()
    conn.close()

    log_audit('ticket_replied', user_id=session['user_id'],
              actor_email=get_current_user()['email'],
              target_type='ticket', target_id=ticket_id,
              metadata={'body_preview': body[:100]}, ip_address=get_client_ip())

    flash('Reply sent!', 'success')
    return redirect(url_for('support_ticket_view', ticket_id=ticket_id))

# ---- Admin: view all tickets (add /admin/tickets route) ----
@app.route('/admin/tickets')
@login_required
def admin_tickets():
    user = get_current_user()
    if user['email'] != 'van.nguyen@email.com' and user['email'] != 'admin@invoicechase.com':
        flash('Access denied', 'error')
        return redirect(url_for('dashboard'))

    status = request.args.get('status', 'open')
    conn = get_db()
    if status == 'all':
        tickets = conn.execute('''
            SELECT t.*, u.name as user_name
            FROM support_tickets t
            LEFT JOIN users u ON t.user_id = u.id
            ORDER BY t.created_at DESC LIMIT 50
        ''').fetchall()
    else:
        tickets = conn.execute('''
            SELECT t.*, u.name as user_name
            FROM support_tickets t
            LEFT JOIN users u ON t.user_id = u.id
            WHERE t.status = ?
            ORDER BY t.created_at DESC LIMIT 50
        ''', (status,)).fetchall()
    conn.close()
    return render_template('admin_tickets.html', tickets=tickets, status=status, user=user)

@app.route('/admin/tickets/<int:ticket_id>/reply', methods=['POST'])
@login_required
def admin_reply_ticket(ticket_id):
    user = get_current_user()
    body = request.form.get('body', '').strip()
    new_status = request.form.get('status', 'open')

    if not body:
        flash('Reply cannot be empty', 'error')
        return redirect(url_for('admin_tickets'))

    conn = get_db()
    ticket = conn.execute('SELECT * FROM support_tickets WHERE id = ?', (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        flash('Ticket not found', 'error')
        return redirect(url_for('admin_tickets'))

    # Insert admin reply
    conn.execute('''
        INSERT INTO ticket_replies (ticket_id, author_type, author_id, author_email, body)
        VALUES (?, ?, ?, ?, ?)
    ''', (ticket_id, 'admin', session['user_id'], user['email'], body))
    conn.execute('UPDATE support_tickets SET status = ?, updated_at = ? WHERE id = ?',
                 (new_status, datetime.now(), ticket_id))
    conn.commit()
    conn.close()

    # Send email to customer
    send_email(
        ticket['email'],
        f"Re: [{ticket['subject']}] — InvoiceChase Support",
        f"InvoiceChase support team replied to your ticket:\n\n{ticket['subject']}\n\n---\n\n{body}\n\n---\nView your ticket: https://invoicechase.onrender.com/support/tickets/{ticket_id}\n"
    )

    log_audit('ticket_replied', user_id=session['user_id'],
              actor_email=user['email'], target_type='ticket', target_id=ticket_id,
              metadata={'body_preview': body[:100], 'as': 'admin', 'new_status': new_status},
              ip_address=get_client_ip())

    flash('Reply sent to customer!', 'success')
    return redirect(url_for('admin_tickets'))

# ------------------------------------------------------------------
# Database migration endpoint (run once after schema updates)
# ------------------------------------------------------------------
@app.route('/admin/migrate')
def admin_migrate():
    if os.environ.get('MIGRATION_SECRET') and request.args.get('key') != os.environ.get('MIGRATION_SECRET'):
        return 'Forbidden', 403
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id INTEGER,
                actor_email TEXT,
                target_type TEXT,
                target_id INTEGER,
                metadata TEXT,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                priority TEXT DEFAULT 'normal',
                assigned_to TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS ticket_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL,
                author_type TEXT NOT NULL,
                author_id INTEGER,
                author_email TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        return 'Migration complete: audit_log, support_tickets, ticket_replies created', 200
    except Exception as e:
        return f'Migration failed: {e}', 500

# ------------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT') or 5000)
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
