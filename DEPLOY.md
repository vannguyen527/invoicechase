## InvoiceChase — Quick Deploy (No Git CLI Needed)

Van — no git credentials on this machine. Here's the 3-minute manual setup:

---

### STEP 1: Create GitHub Repo (2 min)

1. Go to: https://github.com/new
2. Repo name: `invoicechase`
3. Description: `InvoiceChase — Automated invoice reminder agent`
4. Public or Private — your choice
5. DO NOT add README, license, or .gitignore (we already have files)
6. Click **Create repository**
7. On the empty repo page, look for **"push an existing existing repository from the command line"** — copy those 2 commands and paste them into your terminal. They look like:
   ```
   git remote add origin https://github.com/YOUR_USERNAME/invoicechase.git
   git push -u origin main
   ```
8. After pasting those commands, your code is on GitHub

---

### STEP 2: Deploy to Render (2 min)

1. Go to https://dashboard.render.com → **"New +" → "Web Service"**
2. Connect your GitHub account and select the `invoicechase` repo
3. Settings:
   - Name: `invoicechase`
   - Region: Oregon
   - Branch: `main`
   - Runtime: `Python 3`
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app`
   - Instance: **Free**

4. Add Environment Variables:
   - `STRIPE_PUBLISHABLE_KEY` = `pk_test_...`
   - `STRIPE_SECRET_KEY` = `sk_test_...`
   - `STRIPE_PRICE_ID` = `price_...`
   - `STRIPE_WEBHOOK_SECRET` = `whsec_...`
   - `SECRET_KEY` = any random string (run `python3 -c "import secrets; print(secrets.token_hex(32))"`)

5. Click **Create Web Service** → wait 2-3 min → live!

---

### STEP 3: Set up Stripe (5 min)

1. Create Stripe account at stripe.com (free)
2. Create product: $29/mo recurring → copy Price ID
3. Get API keys from stripe.com/test/apikeys
4. Add webhook: `https://invoicechase.onrender.com/webhook` → event: `checkout.session.completed`

---

Once you've created the GitHub repo, paste those 2 git commands into your terminal and the code pushes up. Then tell me the Render URL and I'll verify it works.
