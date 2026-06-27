import sys, os, secrets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import (Flask, render_template, redirect, url_for,
                   request, flash, session, jsonify)
from datetime import date, datetime, timedelta
import stripe
import resend
from dotenv import load_dotenv
load_dotenv()

resend.api_key = os.environ.get("RESEND_API_KEY", "")

from database import SessionLocal
from models import (Subscriber, Payment, DeliveryHold, PaymentAuditLog,
                    SubscriberEventLog, SubscriberStatus, PaymentMethod,
                    PLAN_LABELS, PLAN_PRICES)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
PAYPAL_CLIENT_ID       = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET   = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE            = os.environ.get("PAYPAL_MODE", "sandbox")
BASE_URL               = os.environ.get("BASE_URL", "http://localhost:5001")

TOKEN_TTL_DAYS = 7   # magic links expire after 7 days


# ── Helpers ────────────────────────────────────────────────────────────────────

def current_subscriber():
    sid = session.get("subscriber_id")
    if not sid:
        return None
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sid).first()
    db.close()
    return sub

def require_login():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    return sub

def _issues_left(exp_date):
    if not exp_date or exp_date < date.today():
        return 0
    delta = (exp_date - date.today()).days + 1
    return sum(1 for i in range(delta)
               if (date.today() + timedelta(days=i)).weekday() == 2)

def generate_token(sub, db):
    """Create a new portal token for this subscriber and persist it."""
    token = secrets.token_urlsafe(32)
    sub.portal_token = token
    sub.portal_token_expires = datetime.utcnow() + timedelta(days=TOKEN_TTL_DAYS)
    db.commit()
    return token

def make_portal_link(sub, db):
    token = generate_token(sub, db)
    return f"{BASE_URL}/t/{token}"

def _reset_reminder_flags(sub):
    """Clear all reminder flags so the renewal email sequence restarts."""
    for flag in ["reminder_35_sent","reminder_21_sent","reminder_14_sent",
                 "reminder_expire_sent","grace_14_sent","grace_final_sent"]:
        setattr(sub, flag, False)


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if current_subscriber():
        return redirect(url_for("account"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        account_num = request.form.get("account_num", "").strip()
        zipcode     = request.form.get("zipcode", "").strip().split("-")[0]  # accept 5+4
        db = SessionLocal()
        # Match by simplecirc_id OR clipper-circ id, plus zip
        sub = db.query(Subscriber).filter(
            Subscriber.zipcode.like(f"{zipcode}%"),
        ).filter(
            (Subscriber.simplecirc_id == account_num) |
            (Subscriber.id == account_num if account_num.isdigit() else False)
        ).first()
        db.close()
        if sub:
            session["subscriber_id"] = sub.id
            return redirect(url_for("account"))
        flash("Account not found. Please check your account number and zip code.")
    return render_template("login.html")


@app.route("/email-lookup", methods=["POST"])
def email_lookup():
    """User doesn't know their account#. Send them a magic link by email."""
    email = request.form.get("email", "").strip().lower()
    if not email:
        flash("Please enter your email address.")
        return redirect(url_for("login"))

    db = SessionLocal()
    sub = db.query(Subscriber).filter(
        Subscriber.email.ilike(email)
    ).first()

    if sub:
        link = make_portal_link(sub, db)
        from_email = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.net")
        try:
            resend.Emails.send({
                "from": f"Duxbury Clipper <{from_email}>",
                "to": sub.email,
                "subject": "Your Duxbury Clipper account link",
                "html": (
                    f"<div style='font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;'>"
                    f"<h2 style='color:#1a3a1a;'>Duxbury Clipper</h2>"
                    f"<p>Hi {sub.full_name.split()[0]},</p>"
                    f"<p>Click the button below to access your account. "
                    f"This link is valid for 7 days.</p>"
                    f"<p style='text-align:center;margin:28px 0;'>"
                    f"<a href='{link}' style='background:#2e7d32;color:white;padding:14px 32px;"
                    f"border-radius:6px;text-decoration:none;font-weight:bold;font-size:1em;'>"
                    f"Go to My Account</a></p>"
                    f"<p style='font-size:0.85em;color:#888;'>Button not working? Copy this link:<br>{link}</p>"
                    f"<hr style='border:none;border-top:1px solid #eee;margin:20px 0;'>"
                    f"<p style='font-size:0.8em;color:#aaa;'>Your account number is <strong>{sub.simplecirc_id or sub.id}</strong>."
                    f" You can use this with your zip code ({sub.zipcode[:5]}) to log in any time.</p>"
                    f"</div>"
                ),
            })
        except Exception as e:
            print(f"[EMAIL ERROR] {e}")
        if False:
            # Dev mode: just log the link
            print(f"[DEV] Magic link for {sub.email}: {link}")

    db.close()
    # Always show the same message to prevent email enumeration
    flash("If we found an account with that email, we sent a login link. Check your inbox (and spam folder).")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/t/<token>")
def token_login(token):
    """Magic-link login. Sets session and redirects to account."""
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(portal_token=token).first()
    if not sub or not sub.portal_token_expires:
        db.close()
        flash("This link has expired or is invalid. Please use the link in your latest renewal email, or log in with your account number.")
        return redirect(url_for("login"))
    if sub.portal_token_expires < datetime.utcnow():
        db.close()
        flash("This link has expired (links are valid for 7 days). Please log in with your account number.")
        return redirect(url_for("login"))
    # Valid — log them in and burn the token
    sub.portal_token = None
    sub.portal_token_expires = None
    db.commit()
    sub_id = sub.id
    db.close()
    session["subscriber_id"] = sub_id
    return redirect(url_for("account"))


# ── Account ────────────────────────────────────────────────────────────────────

@app.route("/account")
def account():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sub.id).first()
    holds = db.query(DeliveryHold).filter(
        DeliveryHold.subscriber_id == sub.id,
        DeliveryHold.hold_end >= date.today(),
    ).order_by(DeliveryHold.hold_start).all()
    payments = sorted(sub.payments, key=lambda p: p.paid_at, reverse=True)
    events = db.query(SubscriberEventLog).filter_by(
        subscriber_id=sub.id
    ).order_by(SubscriberEventLog.event_at.desc()).all()
    db.close()
    issues = _issues_left(sub.expiration_date)
    return render_template("account.html",
        subscriber=sub,
        holds=holds,
        payments=payments,
        events=events,
        plan_label=PLAN_LABELS[sub.plan],
        issues_left=issues,
        stripe_key=STRIPE_PUBLISHABLE_KEY,
    )


@app.route("/update-address", methods=["POST"])
def update_address():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sub.id).first()
    old = f"{sub.address1}, {sub.city} {sub.state} {sub.zipcode}"
    sub.address1 = request.form.get("address1","").strip()
    sub.address2 = request.form.get("address2","").strip() or None
    sub.city     = request.form.get("city","").strip()
    sub.state    = request.form.get("state","").strip().upper()
    sub.zipcode  = request.form.get("zipcode","").strip()
    new = f"{sub.address1}, {sub.city} {sub.state} {sub.zipcode}"
    db.add(SubscriberEventLog(
        subscriber_id=sub.id,
        event_type="ADDRESS_UPDATED",
        description=f"Primary address updated from: {old} → {new}",
        performed_by="subscriber",
    ))
    db.commit()
    db.close()
    flash("Address updated successfully.")
    return redirect(url_for("account"))


@app.route("/save-alt-address", methods=["POST"])
def save_alt_address():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()
    s.alt_address1 = request.form.get("alt_address1","").strip() or None
    s.alt_address2 = request.form.get("alt_address2","").strip() or None
    s.alt_city     = request.form.get("alt_city","").strip() or None
    s.alt_state    = request.form.get("alt_state","").strip().upper() or None
    s.alt_zipcode  = request.form.get("alt_zipcode","").strip() or None
    db.commit()
    db.close()
    flash("Alternate address saved.")
    return redirect(url_for("account") + "?tab=address")


@app.route("/toggle-address", methods=["POST"])
def toggle_address():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()
    s.using_alt_address = not s.using_alt_address
    which = "alternate" if s.using_alt_address else "primary"
    active_addr = f"{s.alt_address1}, {s.alt_city} {s.alt_state}" if s.using_alt_address else f"{s.address1}, {s.city} {s.state}"
    db.add(SubscriberEventLog(
        subscriber_id=s.id,
        event_type="ADDRESS_SWITCHED",
        description=f"Delivery switched to {which} address: {active_addr}",
        performed_by="subscriber",
    ))
    db.commit()
    db.close()
    flash(f"Delivery address switched to your {which} address.")
    return redirect(url_for("account") + "?tab=address")


@app.route("/toggle-autorenew", methods=["POST"])
def toggle_autorenew():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()
    # If checkbox was checked, turn on; otherwise toggle
    if "auto_renew" in request.form:
        s.auto_renew = True
    else:
        s.auto_renew = not s.auto_renew
    db.commit()
    db.close()
    flash("Auto-renew " + ("enabled." if s.auto_renew else "disabled."))
    return redirect(url_for("renew_self"))


@app.route("/request-email-change", methods=["POST"])
def request_email_change():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    new_email = request.form.get("new_email","").strip().lower()
    if not new_email:
        flash("Please enter a new email address.")
        return redirect(url_for("account") + "?tab=address")

    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()

    # Check not already in use by another subscriber
    existing = db.query(Subscriber).filter(
        Subscriber.email.ilike(new_email),
        Subscriber.id != s.id
    ).first()
    if existing:
        db.close()
        flash("That email address is already associated with another account.")
        return redirect(url_for("account") + "?tab=address")

    token = secrets.token_urlsafe(32)
    s.pending_email               = new_email
    s.pending_email_token         = token
    s.pending_email_token_expires = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    db.close()

    verify_link = f"{BASE_URL}/verify-email/{token}"
    from_email  = os.environ.get("FROM_EMAIL","subscribe@duxburyclipper.net")

    import sys
    sys.stderr.write(f"[EMAIL-CHANGE] key={resend.api_key[:10] if resend.api_key else 'EMPTY'} from={from_email} to={new_email}\n")
    sys.stderr.flush()

    try:
        # To new address: confirmation link
        resend.Emails.send({
            "from": f"Duxbury Clipper <{from_email}>",
            "to": new_email,
            "subject": "Confirm your new email address — Duxbury Clipper",
            "html": (
                f"<div style='font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;'>"
                f"<h2 style='color:#1a3a1a;'>Duxbury Clipper</h2>"
                f"<p>Hi {s.full_name.split()[0]},</p>"
                f"<p>We received a request to change the email address on your Clipper account to this address.</p>"
                f"<p>Click below to confirm. This link expires in 24 hours.</p>"
                f"<p style='text-align:center;margin:28px 0;'>"
                f"<a href='{verify_link}' style='background:#2e7d32;color:white;padding:14px 32px;"
                f"border-radius:6px;text-decoration:none;font-weight:bold;'>Confirm New Email</a></p>"
                f"<p style='font-size:0.85em;color:#888;'>If you didn't request this, ignore this email — "
                f"your account won't be changed.<br>Link: {verify_link}</p>"
                f"</div>"
            ),
        })
        # To old address: security notice
        if s.email:
            resend.Emails.send({
                "from": f"Duxbury Clipper <{from_email}>",
                "to": s.email,
                "subject": "Email change requested on your Duxbury Clipper account",
                "html": (
                    f"<div style='font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;'>"
                    f"<h2 style='color:#1a3a1a;'>Duxbury Clipper</h2>"
                    f"<p>Hi {s.full_name.split()[0]},</p>"
                    f"<p>A request was made to change the email address on your Duxbury Clipper account "
                    f"from this address to <strong>{new_email}</strong>.</p>"
                    f"<p>A confirmation link has been sent to the new address. "
                    f"If you did not make this request, please contact us immediately.</p>"
                    f"<p style='font-size:0.85em;color:#888;'>Questions? Call 781-934-2811 or reply to this email.</p>"
                    f"</div>"
                ),
            })
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")

    flash(f"A confirmation link has been sent to {new_email}. Click it to complete the change.")
    return redirect(url_for("account") + "?tab=address")


@app.route("/verify-email/<token>")
def verify_email(token):
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(pending_email_token=token).first()
    if not sub:
        db.close()
        flash("This link is invalid or has already been used.")
        return redirect(url_for("login"))
    if sub.pending_email_token_expires < datetime.utcnow():
        db.close()
        flash("This link has expired. Please request the email change again.")
        return redirect(url_for("login"))

    old_email = sub.email
    sub.email                     = sub.pending_email
    sub.pending_email             = None
    sub.pending_email_token       = None
    sub.pending_email_token_expires = None
    db.add(SubscriberEventLog(
        subscriber_id=sub.id,
        event_type="EMAIL_CHANGED",
        description=f"Primary email changed from {old_email} to {sub.email}",
        performed_by="subscriber",
    ))
    db.commit()
    sub_id = sub.id
    db.close()

    session["subscriber_id"] = sub_id
    flash("Your email address has been updated successfully.")
    return redirect(url_for("account") + "?tab=address")


@app.route("/update-contact", methods=["POST"])
def update_contact():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()
    s.phone         = request.form.get("phone","").strip() or None
    s.backup_email  = request.form.get("backup_email","").strip() or None
    db.commit()
    db.close()
    flash("Contact information updated.")
    return redirect(url_for("account") + "?tab=address")


@app.route("/add-hold", methods=["POST"])
def add_hold():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    try:
        hold_start = date.fromisoformat(request.form.get("hold_start",""))
        hold_end   = date.fromisoformat(request.form.get("hold_end",""))
    except ValueError:
        flash("Invalid dates.")
        return redirect(url_for("account"))
    if hold_end <= hold_start:
        flash("Hold end date must be after start date.")
        return redirect(url_for("account"))
    db = SessionLocal()
    sub_db = db.query(Subscriber).filter_by(id=sub.id).first()
    hold_days = (hold_end - hold_start).days
    db.add(DeliveryHold(subscriber_id=sub.id, hold_start=hold_start, hold_end=hold_end))
    if sub_db.expiration_date:
        sub_db.expiration_date += timedelta(days=hold_days)
    if hold_start <= date.today():
        sub_db.status = SubscriberStatus.ON_HOLD
    db.commit()
    db.close()
    flash(f"Delivery hold added — expiration extended by {hold_days} days.")
    return redirect(url_for("account"))


@app.route("/remove-hold/<int:hold_id>", methods=["POST"])
def remove_hold(hold_id):
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    hold = db.query(DeliveryHold).filter_by(id=hold_id, subscriber_id=sub.id).first()
    if hold:
        sub_db = db.query(Subscriber).filter_by(id=sub.id).first()
        hold_days = (hold.hold_end - hold.hold_start).days
        db.delete(hold)
        db.flush()
        if sub_db.expiration_date:
            sub_db.expiration_date -= timedelta(days=hold_days)
        remaining = db.query(DeliveryHold).filter(
            DeliveryHold.subscriber_id == sub.id,
            DeliveryHold.hold_start <= date.today(),
            DeliveryHold.hold_end >= date.today(),
        ).count()
        if remaining == 0 and sub_db.status == SubscriberStatus.ON_HOLD:
            sub_db.status = SubscriberStatus.ACTIVE
        db.commit()
    db.close()
    flash("Hold removed.")
    return redirect(url_for("account"))


# ── Renewal ────────────────────────────────────────────────────────────────────

@app.route("/renew")
def renew_self():
    """Subscriber clicks Renew from their account page."""
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sub.id).first()
    db.close()
    issues = _issues_left(sub.expiration_date)
    return render_template("renew.html",
        subscriber=sub,
        plan_label=PLAN_LABELS[sub.plan],
        price=PLAN_PRICES[sub.plan],
        issues_left=issues,
        all_plans=[(k,v,PLAN_PRICES[k]) for k,v in PLAN_LABELS.items()
                   if k.value not in ("COMPLIMENTARY","GIFT")],
        stripe_key=STRIPE_PUBLISHABLE_KEY,
        paypal_client_id=PAYPAL_CLIENT_ID,
    )


@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sub.id).first()
    db.close()

    selected_plan = request.form.get("plan", sub.plan.value)
    try:
        plan_code = PlanCode(selected_plan)
    except ValueError:
        plan_code = sub.plan
    price_cents = int(PLAN_PRICES[plan_code] * 100)
    checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        customer_email=sub.email,
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": price_cents,
                "product_data": {"name": f"Duxbury Clipper — {PLAN_LABELS[plan_code]}"},
            },
            "quantity": 1,
        }],
        metadata={"subscriber_id": str(sub.id), "plan": plan_code.value},
        success_url=f"{BASE_URL}/renewal-success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/renew",
    )
    return redirect(checkout.url)


@app.route("/renewal-success")
def renewal_success():
    sub = current_subscriber()
    return render_template("renewal_success.html", subscriber=sub)


# ── Stripe webhook ─────────────────────────────────────────────────────────────

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.data
    sig_header = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify(error="Invalid signature"), 400

    db = SessionLocal()

    if event["type"] == "checkout.session.completed":
        cs = event["data"]["object"]
        sub_id = cs.get("metadata", {}).get("subscriber_id")
        if sub_id:
            sub = db.query(Subscriber).filter_by(id=int(sub_id)).first()
            if sub:
                amount = cs.get("amount_total", 0) / 100
                new_plan = cs.get("metadata", {}).get("plan")
                if new_plan:
                    try:
                        sub.plan = PlanCode(new_plan)
                    except ValueError:
                        pass
                period_start = date.today()
                period_end   = (sub.expiration_date or date.today()).replace(
                    year=(sub.expiration_date or date.today()).year + 1)
                sub.expiration_date = period_end
                sub.status          = SubscriberStatus.ACTIVE
                sub.payment_method  = PaymentMethod.CREDIT_CARD
                sub.stripe_customer_id = cs.get("customer")
                _reset_reminder_flags(sub)

                pmt = Payment(
                    subscriber_id=sub.id, amount=amount,
                    payment_method=PaymentMethod.CREDIT_CARD,
                    stripe_payment_intent_id=cs.get("payment_intent"),
                    period_start=period_start, period_end=period_end,
                    entered_by="Stripe (subscriber)",
                    paid_at=datetime.utcnow(),
                )
                db.add(pmt)
                db.flush()
                db.add(PaymentAuditLog(
                    action="CREATED", payment_id=pmt.id,
                    subscriber_id=sub.id, subscriber_name=sub.full_name,
                    amount=amount, payment_method="CREDIT_CARD",
                    period_start=period_start, period_end=period_end,
                    entered_by="Stripe (subscriber)",
                ))
                db.commit()

    elif event["type"] == "invoice.payment_failed":
        invoice     = event["data"]["object"]
        customer_id = invoice.get("customer")
        sub = db.query(Subscriber).filter_by(stripe_customer_id=customer_id).first()
        if sub and sub.email:
            try:
                import resend as r
                r.api_key = os.environ.get("RESEND_API_KEY","")
                from_email = os.environ.get("FROM_EMAIL","subscribe@duxburyclipper.com")
                r.Emails.send({
                    "from": f"Duxbury Clipper <{from_email}>",
                    "to": sub.email,
                    "subject": "Problem with your Duxbury Clipper renewal",
                    "html": (f"<p>Dear {sub.full_name},</p>"
                             f"<p>We were unable to process your renewal. "
                             f"<a href='{BASE_URL}/renew'>Click here to renew</a>.</p>"
                             f"<p>Questions? Call 781-934-2811.</p>"),
                })
            except Exception:
                pass

    db.close()
    return jsonify(success=True)


# ── PayPal ─────────────────────────────────────────────────────────────────────

@app.route("/create-paypal-order", methods=["POST"])
def create_paypal_order():
    sub = current_subscriber()
    if not sub:
        return jsonify(error="Not logged in"), 401
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=sub.id).first()
    db.close()

    import requests as req
    base = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE=="sandbox" else "https://api-m.paypal.com"
    r = req.post(f"{base}/v1/oauth2/token",
                 auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                 data={"grant_type":"client_credentials"})
    access_token = r.json().get("access_token","")

    selected_plan = request.form.get("plan", sub.plan.value)
    try:
        plan_code = PlanCode(selected_plan)
    except ValueError:
        plan_code = sub.plan
    session["pending_plan"] = plan_code.value
    price = f"{PLAN_PRICES[plan_code]:.2f}"
    r2 = req.post(f"{base}/v2/checkout/orders",
        headers={"Authorization":f"Bearer {access_token}","Content-Type":"application/json"},
        json={"intent":"CAPTURE",
              "purchase_units":[{"amount":{"currency_code":"USD","value":price},
                                 "description":f"Duxbury Clipper — {PLAN_LABELS[plan_code]}"}],
              "application_context":{
                  "return_url":f"{BASE_URL}/paypal-success",
                  "cancel_url":f"{BASE_URL}/renew"}})
    order = r2.json()
    approve_url = next((l["href"] for l in order.get("links",[]) if l["rel"]=="approve"), None)
    if approve_url:
        return redirect(approve_url)
    return jsonify(error="Could not create PayPal order"), 500


@app.route("/paypal-success")
def paypal_success():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    import requests as req
    token = request.args.get("token")
    if not token:
        flash("PayPal payment not completed.")
        return redirect(url_for("renew_self"))

    base = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE=="sandbox" else "https://api-m.paypal.com"
    r = req.post(f"{base}/v1/oauth2/token",
                 auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                 data={"grant_type":"client_credentials"})
    access_token = r.json().get("access_token","")
    r2 = req.post(f"{base}/v2/checkout/orders/{token}/capture",
                  headers={"Authorization":f"Bearer {access_token}","Content-Type":"application/json"})
    order = r2.json()

    if order.get("status") == "COMPLETED":
        amount = float(order["purchase_units"][0]["payments"]["captures"][0]["amount"]["value"])
        db = SessionLocal()
        s = db.query(Subscriber).filter_by(id=sub.id).first()
        pending_plan = session.pop("pending_plan", None)
        if pending_plan:
            try:
                s.plan = PlanCode(pending_plan)
            except ValueError:
                pass
        period_start = date.today()
        period_end   = (s.expiration_date or date.today()).replace(
            year=(s.expiration_date or date.today()).year + 1)
        s.expiration_date = period_end
        s.status          = SubscriberStatus.ACTIVE
        s.payment_method  = PaymentMethod.PAYPAL
        _reset_reminder_flags(s)
        pmt = Payment(subscriber_id=s.id, amount=amount,
                      payment_method=PaymentMethod.PAYPAL,
                      notes=f"PayPal order {token}",
                      period_start=period_start, period_end=period_end,
                      entered_by="PayPal (subscriber)", paid_at=datetime.utcnow())
        db.add(pmt)
        db.flush()
        db.add(PaymentAuditLog(
            action="CREATED", payment_id=pmt.id,
            subscriber_id=s.id, subscriber_name=s.full_name,
            amount=amount, payment_method="PAYPAL",
            period_start=period_start, period_end=period_end,
            entered_by="PayPal (subscriber)"))
        db.commit()
        db.close()
        return render_template("renewal_success.html", subscriber=s)

    flash("PayPal payment was not completed. Please try again.")
    return redirect(url_for("renew_self"))


@app.route("/paypal-cancel")
def paypal_cancel():
    flash("PayPal payment was cancelled.")
    return redirect(url_for("renew_self"))


if __name__ == "__main__":
    app.run(debug=True, port=5001)
