# InvoiceChase — Deployment Guide

## What this is
A micro-SaaS that automatically chases overdue invoices via email reminders.
- Landing page at `/`
- Stripe Checkout for $29/mo subscription
- Webhook handler for new subscriptions
- Keep-alive ping for Render free tier

## Prerequisites to deploy
1. **Stripe account** (free) — stripe.com
2. **GitHub account** (free) — github.com
3. **Render account** (free) — render.com

---

## Step 1: Set up Stripe

1. Go to https://dashboard.stripe.com/test/products
2. Click "New product"
   - Name: `InvoiceChase Monthly`
   - Pricing: $29/month (one-time or recurring — choose "Recurring")
   - Billing period: Monthly
3. Copy the **Price ID** (looks like `price_abc123...`)
4. Go to https://dashboard.stripe.com/test/apikeys
   - Copy **Publishable key** (pk_test_...)
   - Copy **Secret key** (sk_test_...)
5. Go to https://dashboard.stripe.com/test/webhooks
   - Add endpoint: `https://your-app.onrender.com/webhook`
   - Select event: `checkout.session.completed`
   - Copy the **Webhook signing secret** (whsec_...)

---

## Step 2: Push to GitHub

```bash
cd /home/lisa/micro-saas
git init
git add .
git commit -m "InvoiceChase MVP"
gh repo create invoicechase --public --push
```
Or manually create a repo on GitHub and push.

---

## Step 3: Deploy to Render

1. Go to https://dashboard.render.com
2. Click **"New +" → "Web Service"**
3. Connect your GitHub repo (`invoicechase`)
4. Configure:
   - **Name:** `invoicechase`
   - **Region:** Oregon (closest to you)
   - **Branch:** `main`
   - **Root directory:** (leave blank)
   - **Runtime:** `Python 3`
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
   - **Instance type:** `Free`

5. **Add Environment Variables** (click "Environment"):

   | Key | Value |
   |-----|-------|
   | `STRIPE_PUBLISHABLE_KEY` | `pk_test_...` from Stripe dashboard |
   | `STRIPE_SECRET_KEY` | `sk_test_...` from Stripe dashboard |
   | `STRIPE_PRICE_ID` | `price_...` from Stripe product |
   | `STRIPE_WEBHOOK_SECRET` | `whsec_...` from Stripe webhook |
   | `SECRET_KEY` | Any random string (run `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `FLASK_DEBUG` | `0` |

6. Click **"Create Web Service"**

Render will build and deploy. Wait 2-3 minutes. Your app will be live at:
`https://invoicechase.onrender.com`

---

## Step 4: Update Stripe webhook

Once Render gives you the URL (e.g. `https://invoicechase.onrender.com`), add it as a webhook in Stripe:
- Endpoint: `https://invoicechase.onrender.com/webhook`
- Events: `checkout.session.completed`

---

## Step 5: Test it

1. Go to `https://invoicechase.onrender.com`
2. Enter an email → click buy
3. You'll go through Stripe Checkout (test mode)
4. Use Stripe test card: `4242 4242 4242 4242`
5. On success, you should land on `/success`

---

## Switching to Production

When ready for real payments:
1. Change all `STRIPE_*_KEY` values from `test_` to live keys
2. Change `pk_test_` to `pk_live_` in the app or pass via env var
3. Update Stripe webhook to production endpoint

---

## Render Free Tier Notes

- Instance sleeps after **15 minutes** of no traffic
- It wakes on the next request (may take 30-60 seconds)
- Keep-alive ping is set up at `/ping` — set an uptime monitor (e.g. UptimeRobot, free) to ping every 5 min
- Never lose a subscriber

## Product Roadmap (Phase 2)
- Email reminder logic (SMTP or SendGrid)
- Dashboard to add/track invoices
- Per-user authentication
- Custom reminder templates
