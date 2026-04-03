# InvoiceChase App — Full Audit Report

**Date:** April 3, 2026
**App:** /home/lisa/micro-saas/app.py
**DB:** /home/lisa/micro-saas/invoicechase.db (SQLite)

---

## FLOW 1: Payment → Account Creation

### Code Path
```
Stripe Checkout → POST /webhook (checkout.session.completed)
  → provision_paid_account(email, stripe_session_id)
    → Creates user in `users` table
    → Creates record in `paid_users` table
    → Sends welcome email with credentials
```

### Status: ✅ WORKING

**Test results:**
- `provision_paid_account()` correctly inserts into both `users` and `paid_users` tables
- User ID is returned, temp password generated
- Welcome email is sent (mocked: prints to console since SMTP creds not set)
- Login succeeds with temp password

**BUG (minor):** Welcome email body starts with `HiTest User,` — missing space after comma:
```python
# Line 370 - BROKEN
welcome_body = f"""Hi{name + ',' if name else ''}  # "HiTest User," ✓ but "Hi," when no name
```
Fix:
```python
welcome_body = f"""Hi {name + ',' if name else ''}  # "Hi Test User," or "Hi,"
```

**BUG (minor):** `stripe_session_id=session_id` in UPDATE has corrupted SQL (line 356):
```python
# Line 356 - BROKEN (SQL will fail)
conn.execute('''UPDATE paid_users SET password_hash=*** name=?, ...
```
Should be:
```python
conn.execute('''UPDATE paid_users SET password_hash=?, name=?, ...
```
However this branch only runs for existing users, and the existing user check at line 319 already returns before reaching this, so it's effectively dead code. Needs fixing for correctness.

---

## FLOW 2: Support Ticket Flow

### Code Path
```
POST /support → inserts into support_tickets → flash success → redirect /support/tickets
GET /support/tickets → lists user's tickets (login_required)
GET /support/tickets/<id> → shows ticket + replies
POST /support/tickets/<id>/reply → adds user reply
GET /admin/tickets → admin panel (restricted to van.nguyen@email.com or admin@invoicechase.com)
POST /admin/tickets/<id>/reply → admin reply + email to customer + status update
```

### Status: ✅ MOSTLY WORKING

**What works:**
- Customer can submit ticket at `/support`
- Tickets stored in `support_tickets` table
- Customer gets confirmation flash and redirected to `/support/tickets`
- Admin dashboard at `/admin/tickets` with filter tabs (open/resolved/closed/all)
- Admin reply modal sends email to customer
- Status updates correctly

**BUG:** `support_ticket_view.html` is rendered but there's no `support_ticket_view.html` template file in the templates directory. The route would crash.
```python
# Line 1059-1060 - template missing
return render_template('support_ticket_view.html', ...)
```
**Fix:** Create `/home/lisa/micro-saas/templates/support_ticket_view.html` (similar to `support_tickets.html` but showing single ticket + reply form).

**Missing:** No notification to customer when their ticket is replied to (the admin reply sends email, but the ticket status change doesn't notify).

---

## FLOW 3: Invoice Reminder Flow (CORE PRODUCT)

### Code Path
```
POST /invoices/add → INSERT invoice → schedule_reminders() → INSERT 3 reminders
  Reminders scheduled at: due_date+3, due_date+7, due_date+14 days

Background check (every 15 min if scheduler running):
  check_and_send_reminders()
    → SELECT reminders WHERE sent_at IS NULL AND scheduled_for <= now AND status=unpaid
    → get_email_template(user_id, reminder_type) → fills {client_name, amount, due_date, description, user_name}
    → send_email(client_email, subject, body) ← SENT TO DEBTOR, not customer
    → UPDATE reminders SET sent_at = now
```

### Status: ⚠️ WORKS BUT WITH RENDER DEPLOYMENT ISSUE

**Test results (live test passed):**
```
[EMAIL MOCK] To: overdue@debtor.com          ← CORRECTLY SENT TO DEBTOR
Subject: Friendly Reminder: Invoice Due
Hi Overdue Corp,
This is a friendly reminder that your invoice is due in 3 days.
Invoice Details:
  Amount: $500.00
  Due Date: 2026-03-29
Best regards,
Test User 2
```

- Reminders are correctly sent to `client_email` (debtor's email), NOT the customer
- Email templates use `{client_name}`, `{amount}`, `{due_date}`, `{description}`, `{user_name}` correctly
- Due date + 3/7/14 day schedule is correct

**CRITICAL ISSUE — BackgroundScheduler won't work on Render Free Tier:**

```python
# Line 420-437
def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=_run_reminder_check, trigger=IntervalTrigger(minutes=15), ...)
    scheduler.start()
```

Render free tier runs `gunicorn app:app` (1 worker). Background processes get killed after each request cycle. The scheduler may start in the main process but:
1. With 1 gunicorn worker, it's in the same process as Flask
2. On Render's free tier, idle processes are spun down after 15 minutes
3. The scheduler is only started if `FLASK_DEBUG=1` or `RUN_SCHEDULER=1`

**Current mitigation:** `/cron/reminders` endpoint exists (line 485-488) — this is a public no-auth endpoint that triggers `check_and_send_reminders()`. On Render, you can set up an external cron job (e.g., cron-job.org, or Render's paid cron feature) to hit this every 15 minutes.

**Email template issue:** `reminder_3` says "your invoice is **due in 3 days**" — but by the time reminder_3 fires (3+ days past due), the invoice is already overdue. The template text is misleading. Should say "is now X days overdue" for reminder_7 and reminder_14.

**Fix for email templates (lines 229-271):**
```python
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
...''',

    'reminder_14': '''Hi {client_name},
This is a final notice that your invoice is now 14 days overdue.
...''',
}
```

---

## FLOW 4: Invoice Paid / Receipt Flow

### Code Path
```
POST /invoices/<id>/paid → UPDATE invoices SET status='paid', paid_at=now
  → DELETE FROM reminders WHERE invoice_id=? AND sent_at IS NULL (cancel pending)
  → log_audit(...)
  → flash 'Invoice marked as paid!' → redirect dashboard
```

### Status: ❌ BROKEN — NO RECEIPT EMAIL

**What works:**
- Invoice correctly marked as `paid` in DB
- Pending reminders cancelled (deleted)
- Audit log written

**What's missing:**
- **No receipt email sent to debtor** (`client_email`)
- **No confirmation email sent to customer** (the InvoiceChase user)
- No `invoice_paid` email template defined

The core product promise is "get paid automatically." When an invoice is marked paid, nothing is sent to either party.

**Required fix — add to `mark_paid()` (around line 731):**
```python
# After conn.commit() and before flash:
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
InvoiceChase
"""
)

# Optional: send confirmation to customer
send_email(
    user['email'],
    f"Invoice marked as paid — {invoice['client_name']}",
    f"You marked the invoice for {invoice['client_name']} ({invoice['client_email']}) as paid. "
    f"Amount: ${invoice['amount']:.2f}"
)
```

---

## FLOW 5: Login Flow

### Code Path
```
POST /login → SELECT * FROM users WHERE email=?
  → verify password_hash matches
  → session['user_id'] = user['id']
  → flash welcome → redirect /dashboard

GET /dashboard (@login_required) → SELECT invoices WHERE user_id=? → render dashboard
```

### Status: ✅ WORKING

- Login works correctly after account creation
- Session persisted
- Dashboard shows invoices with correct status badges (paid/unpaid/overdue)
- Stats (total outstanding, overdue count) calculate correctly
- `maybe_check_reminders()` (line 491-494) triggers on every dashboard load as passive reminder check

---

## Summary of Issues

| # | Flow | Severity | Issue |
|---|------|----------|-------|
| 1 | Payment | Minor | Welcome email formatting (`HiTest User,` missing space) |
| 2 | Payment | Medium | Dead code SQL typo in `provision_paid_account` line 356 |
| 3 | Support | High | `support_ticket_view.html` template missing — crashes ticket view |
| 4 | Reminders | Critical | BackgroundScheduler won't persist on Render free tier — need external cron |
| 5 | Reminders | Minor | `reminder_3` template says "due in 3 days" but invoice is already past due |
| 6 | Paid/Receipt | Critical | No receipt email to debtor or customer on `mark_paid` |
| 7 | Support | Minor | Admin emails hardcoded (only 2 emails allowed) |

---

## Testing Commands Used

```python
# Simulate webhook locally
from app import app, provision_paid_account
uid, pwd = provision_paid_account('test@example.com', 'Test User', 'test_session')
# Check DB: SELECT * FROM users WHERE email='test@example.com'

# Test login
with app.test_client() as c:
    c.post('/login', data={'email': 'test@example.com', 'password': pwd})
    rv = c.get('/dashboard')

# Test reminder (set reminder to past, then call cron endpoint)
# Manually: UPDATE reminders SET scheduled_for='2026-04-03 00:00:00' WHERE ...
with app.test_client() as c:
    rv = c.get('/cron/reminders')  # triggers check_and_send_reminders()

# Test mark_paid
with app.test_client() as c:
    c.post('/login', data={'email': 'test@example.com', 'password': pwd})
    rv = c.post(f'/invoices/{invoice_id}/paid', follow_redirects=True)
```

---

## Render Deployment Checklist

1. **Cron for reminders:** Set up external cron (e.g., cron-job.org) to `GET https://invoicechase.onrender.com/cron/reminders` every 15 minutes
2. **SMTP credentials:** Set `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT`, `FROM_EMAIL` environment variables
3. **Stripe webhook:** Point to `https://invoicechase.onrender.com/webhook` for `checkout.session.completed`
4. **Database:** `/tmp/invoicechase.db` works on Render but is ephemeral — for production, use a persistent SQLite path or PostgreSQL
5. **BackgroundScheduler:** Do NOT rely on it. Use the `/cron/reminders` endpoint instead.
