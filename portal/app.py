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


# ── Admin MOTO card charge ──────────────────────────────────────────────────────

@app.route("/charge-card", methods=["POST", "OPTIONS"])
def charge_card():
    # CORS preflight
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
        return resp

    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))

    # Simple shared-secret auth so random internet can't hit this
    token = request.headers.get("X-Admin-Token", "")
    if token != os.environ.get("ADMIN_CHARGE_TOKEN", ""):
        return _cors(jsonify({"error": "Unauthorized"})), 401

    data          = request.json or {}
    pm_id         = data.get("payment_method_id", "").strip()
    amount_str    = data.get("amount", "0")
    subscriber_id = data.get("subscriber_id")
    notes         = data.get("notes", "")
    entered_by    = data.get("entered_by", "Staff")
    new_exp_str   = data.get("new_expiration", "")

    if not pm_id.startswith("pm_"):
        return _cors(jsonify({"error": "Invalid payment method ID"})), 400

    try:
        amount_cents = int(float(amount_str) * 100)
        db = SessionLocal()
        sub = db.query(Subscriber).filter_by(id=subscriber_id).first()
        if not sub:
            db.close()
            return _cors(jsonify({"error": "Subscriber not found"})), 404

        # Create or retrieve Stripe customer
        if sub.stripe_customer_id:
            cust_id = sub.stripe_customer_id
        else:
            cust = stripe.Customer.create(name=sub.full_name, email=sub.email or None)
            cust_id = cust.id
            sub.stripe_customer_id = cust_id
            db.commit()

        # Confirm PaymentIntent
        pi = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            customer=cust_id,
            payment_method=pm_id,
            confirm=True,
            payment_method_types=["card"],
            description=f"Clipper subscription — {sub.full_name}",
        )

        if pi.status != "succeeded":
            db.close()
            return _cors(jsonify({"error": f"Payment status: {pi.status}"})), 400

        actual_amount = pi.amount_received / 100
        pmt = Payment(
            subscriber_id=sub.id,
            amount=actual_amount,
            payment_method=PaymentMethod.CREDIT_CARD,
            stripe_payment_intent_id=pi.id,
            notes=notes or None,
            entered_by=entered_by,
            paid_at=datetime.utcnow(),
        )
        db.add(pmt)
        db.flush()
        # Record payment period but do not change expiration (admin controls that separately)
        base = sub.expiration_date if (sub.expiration_date and sub.expiration_date >= date.today()) else date.today()
        pmt.period_start = base
        pmt.period_end   = base
        sub.status = SubscriberStatus.ACTIVE
        for flag in ["reminder_35_sent","reminder_21_sent","reminder_14_sent",
                     "reminder_expire_sent","grace_14_sent","grace_final_sent"]:
            setattr(sub, flag, False)
        db.commit()
        db.close()
        return _cors(jsonify({
            "success": True,
            "pi_id": pi.id,
            "amount": actual_amount,
            "new_expiration": new_exp.isoformat(),
        }))
    except stripe.error.CardError as e:
        return _cors(jsonify({"error": e.user_message or str(e)})), 400
    except Exception as e:
        return _cors(jsonify({"error": str(e)})), 500


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


# ── Obituary Notice Form ───────────────────────────────────────────────────────

OBIT_BASE_FEE      = 100.00   # includes photo + up to 300 words
OBIT_WORD_LIMIT    = 300
OBIT_OVERAGE_RATE  = 0.50     # per word over limit
OBIT_NOTIFY_EMAIL  = "josh@joshcutler.com"

OBIT_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Place an Obituary Notice — Duxbury Clipper</title>
<script src="https://js.stripe.com/v3/"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { font-family: Georgia, serif; background: #f9f9f7; color: #222; margin: 0; padding: 0; }
  .header { background: #1a3a1a; padding: 14px 24px; }
  .header h1 { color: white; margin: 0; font-size: 1.4em; letter-spacing: 1px; }
  .wrap { max-width: 780px; margin: 32px auto; padding: 0 20px 60px; }
  h2 { font-size: 1.5em; border-bottom: 2px solid #1a3a1a; padding-bottom: 6px; margin-top: 36px; }
  h3 { font-size: 1.1em; color: #1a3a1a; margin-top: 28px; margin-bottom: 6px; }
  p.intro { line-height: 1.7; color: #444; }
  label { display: block; font-size: 0.88em; font-weight: 700; margin-bottom: 4px; color: #333; }
  input[type=text], input[type=email], input[type=tel], input[type=number],
  select, textarea {
    width: 100%; padding: 9px 11px; border: 1px solid #ccc; border-radius: 4px;
    font-size: 0.95em; font-family: Georgia, serif; background: white;
  }
  textarea { resize: vertical; }
  /* Step indicator */
  .steps { display: flex; gap: 0; margin-bottom: 28px; }
  .step { flex: 1; text-align: center; padding: 10px 4px; font-size: 0.82em; font-weight: 700;
          background: #ddd; color: #888; border-right: 2px solid white; }
  .step:last-child { border-right: none; }
  .step.active { background: #1a3a1a; color: white; }
  .step.done { background: #4caf50; color: white; }
  .row { display: flex; gap: 16px; margin-bottom: 16px; }
  .row .field { flex: 1; }
  .field { margin-bottom: 16px; }
  .hint { font-size: 0.78em; color: #777; margin-top: 3px; }
  .price-box { background: #1a3a1a; color: white; border-radius: 6px;
               padding: 14px 18px; margin: 16px 0; font-size: 1.05em; }
  .price-box span { font-size: 1.4em; font-weight: 700; }
  .word-bar { font-size: 0.85em; color: #555; margin-top: 4px; }
  .word-bar.over { color: #c62828; font-weight: 700; }
  .radio-group label { font-weight: normal; display: flex; align-items: flex-start; gap: 8px;
                       margin-bottom: 8px; cursor: pointer; }
  .radio-group input[type=radio] { margin-top: 3px; flex-shrink: 0; }
  .consent-row { display: flex; gap: 10px; align-items: flex-start; margin-top: 8px; }
  .consent-row input { margin-top: 3px; flex-shrink: 0; }
  .consent-text { font-size: 0.83em; color: #555; line-height: 1.5; }
  #card-element { border: 1px solid #ccc; border-radius: 4px; padding: 10px 12px;
                  background: white; margin-bottom: 6px; }
  #submit-btn { width: 100%; padding: 14px; background: #1a3a1a; color: white; border: none;
                border-radius: 6px; font-size: 1.1em; font-weight: 700; cursor: pointer;
                margin-top: 12px; font-family: Georgia, serif; }
  #submit-btn:disabled { background: #888; cursor: not-allowed; }
  #review-btn { width: 100%; padding: 14px; background: #1a3a1a; color: white; border: none;
                border-radius: 6px; font-size: 1.1em; font-weight: 700; cursor: pointer;
                margin-top: 16px; font-family: Georgia, serif; }
  #edit-btn { padding: 10px 24px; background: white; color: #1a3a1a; border: 2px solid #1a3a1a;
              border-radius: 6px; font-size: 0.95em; font-weight: 700; cursor: pointer;
              font-family: Georgia, serif; margin-bottom: 20px; }
  #msg { margin-top: 16px; padding: 14px 16px; border-radius: 6px; display: none; font-size: 0.95em; }
  .msg-err { background: #fde8e8; color: #c62828; border: 1px solid #f5c6c6; }
  .section-note { background: #fff8e1; border-left: 3px solid #f0c040; padding: 10px 14px;
                  font-size: 0.87em; color: #555; border-radius: 0 4px 4px 0; margin-bottom: 16px; }
  /* Review panel */
  .review-section { margin-bottom: 20px; }
  .review-section h3 { margin-top: 0; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
  .review-row { display: flex; gap: 12px; padding: 6px 0; border-bottom: 1px solid #f0f0f0;
                font-size: 0.92em; }
  .review-label { font-weight: 700; color: #555; min-width: 140px; flex-shrink: 0; }
  .review-val { color: #222; }
  .obit-proof { background: white; border: 1px solid #ccc; border-radius: 6px;
                padding: 20px 24px; font-family: Georgia, serif; line-height: 1.8;
                font-size: 0.97em; white-space: pre-wrap; margin-top: 8px; }
  .obit-proof-name { font-weight: 700; font-size: 1.1em; margin-bottom: 8px; }
  .proof-box { background: #f5f5f5; border: 2px solid #1a3a1a; border-radius: 8px;
               padding: 20px; margin-bottom: 20px; }
  .proof-box h3 { color: #1a3a1a; margin-top: 0; font-size: 1em; text-transform: uppercase;
                  letter-spacing: 1px; }
  .photo-thumb { width: 80px; height: 80px; object-fit: cover; border-radius: 4px;
                 border: 1px solid #ccc; margin-right: 8px; }
  @media (max-width: 560px) { .row { flex-direction: column; gap: 0; } }
</style>
</head>
<body>
<div class="header"><h1>&#9654; Duxbury Clipper</h1></div>
<div class="wrap">
  <div class="steps">
    <div class="step active" id="step1-tab">1 &nbsp; Obituary Details</div>
    <div class="step" id="step2-tab">2 &nbsp; Review &amp; Pay</div>
    <div class="step" id="step3-tab">3 &nbsp; Confirmed</div>
  </div>
  <h2>Place an Obituary Notice</h2>
  <p class="intro">Please use this form to place an obituary notice in the Duxbury Clipper.
  The deadline to submit for Wednesday's Clipper is the Friday preceding publication.
  The base fee of <strong>$100</strong> includes a photo and up to 300 words.
  Longer notices are welcome — there is an additional fee of <strong>50¢ per word</strong> over 300.</p>

  <div id="main-form">
    <h3>Obituary Details</h3>
    <p class="hint" style="margin-top:-4px;margin-bottom:12px;">This information will be published as part of the notice.</p>

    <div class="field">
      <label>Full name of deceased *</label>
      <input type="text" id="deceased_name" placeholder="As you would like it to appear" required>
    </div>

    <div class="row">
      <div class="field">
        <label>Age at death *</label>
        <input type="number" id="age" min="0" max="130" required>
      </div>
      <div class="field">
        <label>Date of death *</label>
        <input type="text" id="dod" placeholder="MM/DD/YYYY" required>
      </div>
    </div>

    <div class="field">
      <label>Full text of obituary *</label>
      <textarea id="obit_text" rows="12" placeholder="Type or paste your obituary here..." required></textarea>
      <div class="word-bar" id="word_bar">0 words — base fee covers up to 300</div>
    </div>

    <div class="price-box">
      Obituary Notice &nbsp;|&nbsp; Price: <span id="price_display">$100.00</span>
    </div>

    <div class="section-note">First 300 words included in base $100 fee. Each additional word is 50¢.</div>

    <h3>Photo of Loved One</h3>
    <div class="field">
      <label>Upload photo (optional, up to 2 images)</label>
      <input type="file" id="photo_upload" accept=".jpg,.jpeg,.png" multiple>
      <div class="hint">JPG or PNG. A clear headshot works best. Avoid group photos or pictures of pictures.</div>
    </div>

    <h3>Your Contact Info</h3>
    <p class="hint" style="margin-top:-4px;margin-bottom:12px;">This will not be published.</p>

    <div class="row">
      <div class="field">
        <label>First name *</label>
        <input type="text" id="first_name" required>
      </div>
      <div class="field">
        <label>Last name *</label>
        <input type="text" id="last_name" required>
      </div>
    </div>

    <div class="row">
      <div class="field">
        <label>Phone *</label>
        <input type="tel" id="phone" required>
      </div>
      <div class="field">
        <label>Email *</label>
        <input type="email" id="email" required>
      </div>
      <div class="field">
        <label>Confirm email *</label>
        <input type="email" id="email_confirm" required>
      </div>
    </div>

    <div class="row">
      <div class="field">
        <label>Relation to deceased *</label>
        <div class="radio-group">
          <label><input type="radio" name="relation" value="Family member" checked> Family member</label>
          <label><input type="radio" name="relation" value="Funeral home"> Funeral home</label>
          <label><input type="radio" name="relation" value="Other"> Other</label>
        </div>
        <input type="text" id="relation_other" placeholder="Please specify" style="display:none;margin-top:6px;">
      </div>
      <div class="field">
        <label>Publication instructions *</label>
        <div class="radio-group">
          <label><input type="radio" name="pub_timing" value="Print first, then online" checked>
            Publish in the next available Clipper issue and then online</label>
          <label><input type="radio" name="pub_timing" value="Online first, then print">
            Publish online as soon as approved, then in the next Clipper issue</label>
        </div>
        <div class="hint">Deadline for Wednesday's Clipper is Friday at noon. Submissions after that will run the following week.</div>
      </div>
    </div>

    <div class="consent-row">
      <input type="checkbox" id="consent" required>
      <div class="consent-text">I confirm that I'm a family member or funeral home representative with permission to submit this obituary.
      I've done my best to ensure all information is accurate and take responsibility for the details provided.
      I understand the newspaper may decline to publish notices that don't meet its guidelines.</div>
    </div>

    <div id="form-err" class="msg-err" style="display:none;padding:12px 16px;border-radius:6px;margin-top:12px;"></div>
    <button id="review-btn" type="button">Review My Submission &rarr;</button>
  </div>

  <!-- Step 2: Review & Pay -->
  <div id="review-panel" style="display:none;">
    <button id="edit-btn" type="button">&larr; Edit My Submission</button>

    <div class="proof-box">
      <h3>&#128260; Obituary Proof — Review Carefully Before Paying</h3>
      <div class="obit-proof">
        <div class="obit-proof-name" id="proof-name"></div>
        <div id="proof-body"></div>
      </div>
    </div>

    <div class="review-section">
      <h3>Submission Details</h3>
      <div class="review-row"><div class="review-label">Deceased</div><div class="review-val" id="rv-deceased"></div></div>
      <div class="review-row"><div class="review-label">Age</div><div class="review-val" id="rv-age"></div></div>
      <div class="review-row"><div class="review-label">Date of Death</div><div class="review-val" id="rv-dod"></div></div>
      <div class="review-row"><div class="review-label">Word Count</div><div class="review-val" id="rv-words"></div></div>
      <div class="review-row"><div class="review-label">Photo(s)</div><div class="review-val" id="rv-photos"></div></div>
    </div>

    <div class="review-section">
      <h3>Your Contact Info</h3>
      <div class="review-row"><div class="review-label">Name</div><div class="review-val" id="rv-name"></div></div>
      <div class="review-row"><div class="review-label">Phone</div><div class="review-val" id="rv-phone"></div></div>
      <div class="review-row"><div class="review-label">Email</div><div class="review-val" id="rv-email"></div></div>
      <div class="review-row"><div class="review-label">Relation</div><div class="review-val" id="rv-relation"></div></div>
      <div class="review-row"><div class="review-label">Publication</div><div class="review-val" id="rv-pub"></div></div>
    </div>

    <div class="review-section">
      <h3>Payment</h3>
      <div class="price-box" style="margin-bottom:12px;">
        Total due: <span id="rv-price">$100.00</span>
      </div>
      <div class="section-note" id="rv-price-breakdown"></div>
      <label>Card details *</label>
      <div id="card-element"></div>
      <div id="msg"></div>
      <button id="submit-btn" type="button">Submit &amp; Pay <span id="btn-price">$100.00</span></button>
    </div>
  </div>

  <!-- Step 3: Success -->
  <div id="success-panel" style="display:none;text-align:center;padding:40px 20px;">
    <div style="font-size:3em;">&#10003;</div>
    <h2 style="border:none;color:#1b5e20;">Submission Received</h2>
    <p id="success-msg" style="font-size:1.05em;line-height:1.7;color:#333;"></p>
    <p style="color:#777;font-size:0.9em;">Questions? Call us at 781-934-2811.</p>
  </div>
</div>

<script>
const STRIPE_PK = "STRIPE_PK_PLACEHOLDER";
const BASE_FEE = 100.00;
const WORD_LIMIT = 300;
const OVERAGE_RATE = 0.50;

const stripe = Stripe(STRIPE_PK);
const elements = stripe.elements();
const card = elements.create('card', {style: {base: {fontSize: '15px', fontFamily: 'Georgia, serif'}}});
card.mount('#card-element');

function countWords(text) {
  return text.trim() === '' ? 0 : text.trim().split(/\\s+/).length;
}
function calcPrice(words) {
  return BASE_FEE + Math.max(0, words - WORD_LIMIT) * OVERAGE_RATE;
}
function fmt(n) { return '$' + n.toFixed(2); }

function updatePrice() {
  const words = countWords(document.getElementById('obit_text').value);
  const price = calcPrice(words);
  const bar = document.getElementById('word_bar');
  const extra = Math.max(0, words - WORD_LIMIT);
  if (extra > 0) {
    bar.textContent = words + ' words — ' + extra + ' over limit × $0.50 = $' + (extra * 0.50).toFixed(2) + ' extra';
    bar.className = 'word-bar over';
  } else {
    bar.textContent = words + ' words — base fee covers up to 300';
    bar.className = 'word-bar';
  }
  document.getElementById('price_display').textContent = fmt(price);
}
document.getElementById('obit_text').addEventListener('input', updatePrice);

document.querySelectorAll('input[name="relation"]').forEach(r => {
  r.addEventListener('change', function() {
    document.getElementById('relation_other').style.display =
      this.value === 'Other' ? 'block' : 'none';
  });
});

// ── Step 1 → Step 2: Review ────────────────────────────────────────────────
document.getElementById('review-btn').addEventListener('click', function() {
  const errBox = document.getElementById('form-err');
  errBox.style.display = 'none';

  const deceased_name  = document.getElementById('deceased_name').value.trim();
  const age            = document.getElementById('age').value.trim();
  const dod            = document.getElementById('dod').value.trim();
  const obit_text      = document.getElementById('obit_text').value.trim();
  const first_name     = document.getElementById('first_name').value.trim();
  const last_name      = document.getElementById('last_name').value.trim();
  const phone          = document.getElementById('phone').value.trim();
  const email          = document.getElementById('email').value.trim();
  const email_confirm  = document.getElementById('email_confirm').value.trim();
  const consent        = document.getElementById('consent').checked;
  const relation       = document.querySelector('input[name="relation"]:checked').value;
  const relation_other = document.getElementById('relation_other').value.trim();
  const pub_timing     = document.querySelector('input[name="pub_timing"]:checked').value;

  if (!deceased_name || !age || !dod || !obit_text || !first_name || !last_name || !phone || !email) {
    errBox.textContent = 'Please fill in all required fields.';
    errBox.style.display = 'block'; errBox.scrollIntoView({behavior:'smooth'}); return;
  }
  if (email !== email_confirm) {
    errBox.textContent = 'Email addresses do not match.';
    errBox.style.display = 'block'; return;
  }
  if (!consent) {
    errBox.textContent = 'Please check the consent box to continue.';
    errBox.style.display = 'block'; return;
  }

  const words = countWords(obit_text);
  const price = calcPrice(words);
  const extra = Math.max(0, words - WORD_LIMIT);
  const relation_display = relation === 'Other' ? 'Other: ' + relation_other : relation;

  // Populate review panel
  document.getElementById('proof-name').textContent = deceased_name + ', age ' + age;
  document.getElementById('proof-body').textContent = obit_text;
  document.getElementById('rv-deceased').textContent = deceased_name;
  document.getElementById('rv-age').textContent = age;
  document.getElementById('rv-dod').textContent = dod;
  document.getElementById('rv-words').textContent = words + ' words' + (extra > 0 ? ' (' + extra + ' over 300-word limit)' : ' (within 300-word base)');
  document.getElementById('rv-name').textContent = first_name + ' ' + last_name;
  document.getElementById('rv-phone').textContent = phone;
  document.getElementById('rv-email').textContent = email;
  document.getElementById('rv-relation').textContent = relation_display;
  document.getElementById('rv-pub').textContent = pub_timing;

  // Photo thumbnails
  const photoFiles = document.getElementById('photo_upload').files;
  const photoEl = document.getElementById('rv-photos');
  if (photoFiles.length === 0) {
    photoEl.textContent = 'No photo uploaded';
  } else {
    photoEl.innerHTML = '';
    Array.from(photoFiles).slice(0,2).forEach(f => {
      const img = document.createElement('img');
      img.className = 'photo-thumb';
      img.src = URL.createObjectURL(f);
      photoEl.appendChild(img);
      const lbl = document.createElement('span');
      lbl.textContent = f.name;
      lbl.style.fontSize = '0.85em';
      photoEl.appendChild(lbl);
    });
  }

  const priceStr = fmt(price);
  document.getElementById('rv-price').textContent = priceStr;
  document.getElementById('btn-price').textContent = priceStr;
  document.getElementById('rv-price-breakdown').textContent =
    extra > 0
      ? '$100.00 base + ' + extra + ' extra words × $0.50 = ' + priceStr
      : '$100.00 base fee (notice is within 300 words)';

  // Switch panels
  document.getElementById('main-form').style.display = 'none';
  document.getElementById('review-panel').style.display = 'block';
  document.getElementById('step1-tab').className = 'step done';
  document.getElementById('step2-tab').className = 'step active';
  window.scrollTo({top: 0, behavior: 'smooth'});
});

// ── Step 2 → Step 1: Edit ─────────────────────────────────────────────────
document.getElementById('edit-btn').addEventListener('click', function() {
  document.getElementById('review-panel').style.display = 'none';
  document.getElementById('main-form').style.display = 'block';
  document.getElementById('step1-tab').className = 'step active';
  document.getElementById('step2-tab').className = 'step';
  window.scrollTo({top: 0, behavior: 'smooth'});
});

// ── Step 2: Submit & Pay ───────────────────────────────────────────────────
document.getElementById('submit-btn').addEventListener('click', async function() {
  const btn = this;
  const msg = document.getElementById('msg');
  msg.style.display = 'none';

  const deceased_name  = document.getElementById('deceased_name').value.trim();
  const age            = document.getElementById('age').value.trim();
  const dod            = document.getElementById('dod').value.trim();
  const obit_text      = document.getElementById('obit_text').value.trim();
  const first_name     = document.getElementById('first_name').value.trim();
  const last_name      = document.getElementById('last_name').value.trim();
  const phone          = document.getElementById('phone').value.trim();
  const email          = document.getElementById('email').value.trim();
  const relation       = document.querySelector('input[name="relation"]:checked').value;
  const relation_other = document.getElementById('relation_other').value.trim();
  const pub_timing     = document.querySelector('input[name="pub_timing"]:checked').value;
  const words          = countWords(obit_text);
  const price          = calcPrice(words);

  btn.disabled = true;
  btn.textContent = 'Processing…';

  const {paymentMethod, error} = await stripe.createPaymentMethod({type: 'card', card: card});
  if (error) {
    show_err(error.message);
    btn.disabled = false; btn.textContent = 'Submit & Pay ' + fmt(price); return;
  }

  const formData = new FormData();
  formData.append('deceased_name', deceased_name);
  formData.append('age', age);
  formData.append('dod', dod);
  formData.append('obit_text', obit_text);
  formData.append('first_name', first_name);
  formData.append('last_name', last_name);
  formData.append('phone', phone);
  formData.append('email', email);
  formData.append('relation', relation === 'Other' ? 'Other: ' + relation_other : relation);
  formData.append('pub_timing', pub_timing);
  formData.append('words', words);
  formData.append('amount_cents', Math.round(price * 100));
  formData.append('payment_method_id', paymentMethod.id);
  const photos = document.getElementById('photo_upload').files;
  for (let i = 0; i < Math.min(photos.length, 2); i++) {
    formData.append('photos', photos[i]);
  }

  try {
    const resp = await fetch('/obituary/submit', {method: 'POST', body: formData});
    const data = await resp.json();
    if (data.success) {
      document.getElementById('review-panel').style.display = 'none';
      document.getElementById('success-panel').style.display = 'block';
      document.getElementById('step2-tab').className = 'step done';
      document.getElementById('step3-tab').className = 'step active';
      document.getElementById('success-msg').innerHTML =
        'Thank you, <strong>' + first_name + '</strong>. Your obituary notice for <strong>' +
        deceased_name + '</strong> has been received and payment of <strong>' + fmt(price) +
        '</strong> has been processed.<br><br>' +
        'A confirmation has been sent to <strong>' + email + '</strong>. ' +
        'We will be in touch if we have any questions before publication.';
      window.scrollTo({top: 0, behavior: 'smooth'});
    } else {
      show_err(data.error || 'Submission failed. Please try again or call 781-934-2811.');
      btn.disabled = false; btn.textContent = 'Submit & Pay ' + fmt(price);
    }
  } catch(e) {
    show_err('Network error. Please try again or call 781-934-2811.');
    btn.disabled = false; btn.textContent = 'Submit & Pay ' + fmt(price);
  }

  function show_err(m) {
    msg.className = 'msg-err'; msg.textContent = m; msg.style.display = 'block';
  }
});
</script>
</body>
</html>"""


@app.route("/obituary")
def obituary_form():
    pk = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    page = OBIT_PAGE.replace("STRIPE_PK_PLACEHOLDER", pk)
    return page, 200, {"Content-Type": "text/html"}


@app.route("/obituary/submit", methods=["POST"])
def obituary_submit():
    import base64

    deceased_name = request.form.get("deceased_name", "").strip()
    age           = request.form.get("age", "").strip()
    dod           = request.form.get("dod", "").strip()
    obit_text     = request.form.get("obit_text", "").strip()
    first_name    = request.form.get("first_name", "").strip()
    last_name     = request.form.get("last_name", "").strip()
    phone         = request.form.get("phone", "").strip()
    email         = request.form.get("email", "").strip()
    relation      = request.form.get("relation", "").strip()
    pub_timing    = request.form.get("pub_timing", "").strip()
    words         = int(request.form.get("words", "0"))
    amount_cents  = int(request.form.get("amount_cents", "10000"))
    pm_id         = request.form.get("payment_method_id", "").strip()

    if not pm_id.startswith("pm_"):
        return jsonify({"error": "Invalid payment method."}), 400

    # Charge card
    try:
        pi = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            payment_method=pm_id,
            confirm=True,
            payment_method_types=["card"],
            description=f"Obituary notice — {deceased_name}",
            receipt_email=email,
        )
        if pi.status != "succeeded":
            return jsonify({"error": f"Payment status: {pi.status}"}), 400
    except stripe.error.CardError as e:
        return jsonify({"error": e.user_message or str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    amount_paid = amount_cents / 100

    # Build email body
    extra_words = max(0, words - OBIT_WORD_LIMIT)
    pricing_line = f"$100.00 base"
    if extra_words > 0:
        pricing_line += f" + {extra_words} extra words × $0.50 = ${amount_paid:.2f} total"

    body_html = f"""
<h2>New Obituary Notice Submission</h2>
<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;">
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;width:180px;">Deceased</td>
      <td style="padding:6px 12px;">{deceased_name}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Age</td>
      <td style="padding:6px 12px;">{age}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Date of Death</td>
      <td style="padding:6px 12px;">{dod}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Word Count</td>
      <td style="padding:6px 12px;">{words} words</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Pricing</td>
      <td style="padding:6px 12px;">{pricing_line}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Amount Charged</td>
      <td style="padding:6px 12px;"><strong>${amount_paid:.2f}</strong> (Stripe PI: {pi.id})</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Submitter</td>
      <td style="padding:6px 12px;">{first_name} {last_name}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Phone</td>
      <td style="padding:6px 12px;">{phone}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Email</td>
      <td style="padding:6px 12px;">{email}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Relation</td>
      <td style="padding:6px 12px;">{relation}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Publication</td>
      <td style="padding:6px 12px;">{pub_timing}</td></tr>
</table>
<h3 style="margin-top:24px;">Obituary Text ({words} words)</h3>
<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:4px;padding:16px;
            font-family:Georgia,serif;font-size:14px;line-height:1.7;white-space:pre-wrap;">{obit_text}</div>
"""

    # Attach photos
    attachments = []
    for photo in request.files.getlist("photos"):
        if photo and photo.filename:
            data = photo.read()
            attachments.append({
                "filename": photo.filename,
                "content": list(data),
            })

    try:
        resend.Emails.send({
            "from": "Duxbury Clipper <noreply@duxburyclipper.net>",
            "to": [OBIT_NOTIFY_EMAIL],
            "subject": f"Obituary Notice: {deceased_name} — ${amount_paid:.2f} paid",
            "html": body_html,
            "attachments": attachments,
        })
    except Exception:
        pass  # payment already succeeded — don't fail the submission over email

    # Confirmation email to submitter
    try:
        resend.Emails.send({
            "from": "Duxbury Clipper <noreply@duxburyclipper.net>",
            "to": [email],
            "subject": f"Obituary Notice Received — {deceased_name}",
            "html": f"""
<p>Dear {first_name},</p>
<p>Thank you for submitting an obituary notice for <strong>{deceased_name}</strong> to the Duxbury Clipper.</p>
<p>We have received your submission and processed payment of <strong>${amount_paid:.2f}</strong>.</p>
<p><strong>Publication preference:</strong> {pub_timing}</p>
<p>We will be in touch if we have any questions before publication. If you need to make changes or have questions, please call us at <strong>781-934-2811</strong>.</p>
<p style="color:#777;font-size:0.9em;">The Duxbury Clipper &mdash; duxburyclipper.com</p>
""",
        })
    except Exception:
        pass

    return jsonify({"success": True, "amount": amount_paid})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
