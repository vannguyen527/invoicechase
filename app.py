"""
InvoiceChase — Automated invoice reminder agent
Phase 2 MVP: Landing page + Stripe checkout + email reminder scheduling
"""

import os
import re
from flask import Flask, render_template, request, redirect, url_for, jsonify
import stripe

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# Stripe — use test keys locally, real keys on Render
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', 'sk_test_placeholder')
stripe_pub_key = os.environ.get('STRIPE_PUBLISHABLE_KEY', 'pk_test_placeholder')
PRICE_ID = os.environ.get('STRIPE_PRICE_ID', 'price_placeholder')  # $29/mo recurring

# Render free tier: instance idles after 15min of no traffic
# Keep-alive ping every 5 minutes
PING_INTERVAL = int(os.environ.get('PING_INTERVAL', 280))  # seconds

# ----------------------------------------------------------------
# Landing Page
# ----------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html',
                           stripe_pub_key=stripe_pub_key,
                           price_id=PRICE_ID)

# ----------------------------------------------------------------
# Checkout — create Stripe Checkout Session
# ----------------------------------------------------------------
@app.route('/checkout', methods=['POST'])
def checkout():
    try:
        email = request.form.get('email', '').strip()
        if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
            return jsonify({'error': 'Valid email required'}), 400

        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',
            customer_email=email,
            line_items=[{
                'price': PRICE_ID,
                'quantity': 1,
            }],
            success_url=url_for('success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('index', _external=True),
            metadata={'email': email}
        )
        return redirect(session.url, code=303)
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 400

# ----------------------------------------------------------------
# Success page
# ----------------------------------------------------------------
@app.route('/success')
def success():
    session_id = request.args.get('session_id', '')
    if session_id:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            email = session.get('customer_email', '')
            return render_template('success.html', email=email)
        except:
            pass
    return render_template('success.html', email='')

# ----------------------------------------------------------------
# Stripe Webhook — record successful subscription
# ----------------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig = request.headers.get('Stripe-Signature', '')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

    try:
        if endpoint_secret and sig:
            event = stripe.Webhook.construct_event(payload, sig, endpoint_secret)
        else:
            event = stripe.api_version and stripe.util.convert_to_stripe_object(payload)
    except ValueError:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        email = session.get('customer_email', '')
        sub_id = session.get('subscription', '')
        print(f"NEW SUBSCRIBER: {email} | sub: {sub_id}")
        # TODO: add to email reminder list, schedule first reminder

    return '', 200

# ----------------------------------------------------------------
# Health check + keep-alive for Render free tier
# ----------------------------------------------------------------
@app.route('/ping')
def ping():
    return 'ok', 200

# ----------------------------------------------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG') == '1')
