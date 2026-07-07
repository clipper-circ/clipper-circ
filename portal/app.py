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

from database import SessionLocal, engine
from models import (Subscriber, Payment, DeliveryHold, PaymentAuditLog,
                    SubscriberEventLog, SubscriberStatus, PaymentMethod, PlanCode,
                    PLAN_LABELS, PLAN_PRICES, PLAN_DESCRIPTIONS, ObituarySubmission, Setting, DiscountCode)
from models import Base
Base.metadata.create_all(bind=engine)  # ensure new tables exist on Railway

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
OBIT_STRIPE_SECRET_KEY     = os.environ.get("OBIT_STRIPE_SECRET_KEY", "")
OBIT_STRIPE_PUBLISHABLE_KEY = os.environ.get("OBIT_STRIPE_PUBLISHABLE_KEY", "")
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
    if "portal.duxburyclipper" in request.host:
        return redirect(url_for("login"))
    return redirect(url_for("obituary_form"))


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
        price=PLAN_PRICES[sub.plan],
        all_plans=[(k,v,PLAN_PRICES[k]) for k,v in PLAN_LABELS.items()
                   if k.value not in ("COMPLIMENTARY","GIFT")],
        paypal_client_id=PAYPAL_CLIENT_ID,
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
    if "auto_renew" in request.form:
        s.auto_renew = True
        if s.stripe_subscription_id:
            try:
                stripe.Subscription.modify(s.stripe_subscription_id, cancel_at_period_end=False)
            except Exception:
                pass
        db.add(SubscriberEventLog(
            subscriber_id=s.id, event_type="AUTO_RENEW_ENABLED",
            description="Auto-renew re-enabled by subscriber.", performed_by="subscriber",
        ))
        flash("Auto-renew enabled.")
    else:
        s.auto_renew = False
        if s.stripe_subscription_id:
            try:
                stripe.Subscription.modify(s.stripe_subscription_id, cancel_at_period_end=True)
            except Exception:
                pass
        db.add(SubscriberEventLog(
            subscriber_id=s.id, event_type="AUTO_RENEW_CANCELLED",
            description="Auto-renew cancelled by subscriber.", performed_by="subscriber",
        ))
        flash("Auto-renew disabled. Your subscription will not renew automatically.")
    db.commit()
    db.close()
    return redirect(url_for("account") + "?tab=renew")


@app.route("/request-email-change", methods=["POST"])
def request_email_change():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    new_email = request.form.get("new_email","").strip().lower()
    if not new_email:
        flash("Please enter a new email address.")
        return redirect(url_for("account") + "?tab=contact")

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
        return redirect(url_for("account") + "?tab=contact")

    token = secrets.token_urlsafe(32)
    s.pending_email               = new_email
    s.pending_email_token         = token
    s.pending_email_token_expires = datetime.utcnow() + timedelta(hours=24)
    first_name = s.full_name.split()[0]
    old_email  = s.email
    db.commit()
    db.close()

    verify_link = f"{BASE_URL}/verify-email/{token}"
    from_email  = os.environ.get("FROM_EMAIL","subscribe@duxburyclipper.net")

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
                f"<p>Hi {first_name},</p>"
                f"<p>We received a request to change the email address on your Clipper home delivery account to this address.</p>"
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
        if old_email:
            resend.Emails.send({
                "from": f"Duxbury Clipper <{from_email}>",
                "to": old_email,
                "subject": "Email change requested on your Duxbury Clipper account",
                "html": (
                    f"<div style='font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:20px;'>"
                    f"<h2 style='color:#1a3a1a;'>Duxbury Clipper</h2>"
                    f"<p>Hi {first_name},</p>"
                    f"<p>A request was made to change the email address on your Duxbury Clipper home delivery account "
                    f"from this address to <strong>{new_email}</strong>.</p>"
                    f"<p>A confirmation link has been sent to the new address. "
                    f"If you did not make this request, please contact us immediately.</p>"
                    f"<p style='font-size:0.85em;color:#888;'>Questions? Call 781-934-2811 or reply to this email.</p>"
                    f"</div>"
                ),
            })
    except Exception as e:
        sys.stderr.write(f"[EMAIL ERROR] {e}\n")
        sys.stderr.flush()

    flash(f"A confirmation link has been sent to {new_email}. Click it to complete the change.")
    return redirect(url_for("account") + "?tab=contact")


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
    return redirect(url_for("account") + "?tab=contact")


@app.route("/update-name", methods=["POST"])
def update_name():
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    db = SessionLocal()
    s = db.query(Subscriber).filter_by(id=sub.id).first()
    name = request.form.get("full_name","").strip()
    if name and name != s.full_name:
        old_name = s.full_name
        s.full_name = name
        db.add(SubscriberEventLog(
            subscriber_id=s.id,
            event_type="NAME_UPDATED",
            description=f"Name changed from: {old_name} → {name}",
            performed_by="subscriber",
        ))
        db.commit()
        flash("Name updated.")
    db.close()
    return redirect(url_for("account") + "?tab=contact")


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
    sub = current_subscriber()
    if not sub:
        return redirect(url_for("login"))
    return redirect(url_for("account") + "?tab=renew")


@app.route("/apply-discount", methods=["POST"])
def apply_discount():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "").strip().upper()
    plan_val = data.get("plan", "LOCAL")
    try:
        plan_code = PlanCode(plan_val)
    except ValueError:
        plan_code = PlanCode.LOCAL
    db = SessionLocal()
    dc = db.query(DiscountCode).filter_by(code=code, active=True).first()
    db.close()
    if not dc:
        return jsonify(valid=False, error="Invalid or inactive discount code.")
    if dc.expires_at and dc.expires_at < date.today():
        return jsonify(valid=False, error="This discount code has expired.")
    if dc.max_uses is not None and dc.use_count >= dc.max_uses:
        return jsonify(valid=False, error="This discount code has reached its maximum uses.")
    original = PLAN_PRICES[plan_code]
    discounted = round(original * (1 - dc.discount_percent / 100), 2)
    return jsonify(valid=True, discount_percent=dc.discount_percent,
                   original=original, discounted=discounted)


@app.route("/admin/discount-codes", methods=["GET", "POST"])
def admin_discount_codes():
    return redirect("https://admin.duxburyclipper.net")
    db = SessionLocal()
    msg = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            code = request.form.get("code", "").strip().upper()
            pct  = int(request.form.get("discount_percent", 10))
            note = request.form.get("note", "").strip()
            max_uses = request.form.get("max_uses", "").strip()
            expires  = request.form.get("expires_at", "").strip()
            from datetime import date as ddate
            dc = DiscountCode(
                code=code, discount_percent=pct, note=note or None,
                max_uses=int(max_uses) if max_uses else None,
                expires_at=ddate.fromisoformat(expires) if expires else None,
            )
            db.add(dc)
            db.commit()
            msg = f"Code {code} created."
        elif action == "toggle":
            dc_id = int(request.form.get("dc_id"))
            dc = db.query(DiscountCode).get(dc_id)
            if dc:
                dc.active = not dc.active
                db.commit()
                msg = f"Code {dc.code} {'activated' if dc.active else 'deactivated'}."
        elif action == "delete":
            dc_id = int(request.form.get("dc_id"))
            dc = db.query(DiscountCode).get(dc_id)
            if dc:
                db.delete(dc)
                db.commit()
                msg = "Code deleted."
    codes = db.query(DiscountCode).order_by(DiscountCode.id.desc()).all()
    db.close()
    return render_template("discount_codes.html", codes=codes, msg=msg)


@app.route("/subscribe", methods=["GET", "POST"])
def subscribe_new():
    plans = [(k, v, PLAN_PRICES[k], PLAN_DESCRIPTIONS.get(k, "")) for k, v in PLAN_LABELS.items()
             if k.value not in ("COMPLIMENTARY", "GIFT")]

    if request.method == "GET":
        return render_template("subscribe.html", plans=plans, form={},
                               paypal_client_id=PAYPAL_CLIENT_ID)

    # POST — validate and create subscriber
    first_name = request.form.get("first_name", "").strip()
    last_name  = request.form.get("last_name", "").strip()
    email      = request.form.get("email", "").strip()
    phone      = request.form.get("phone", "").strip()
    address1   = request.form.get("address1", "").strip()
    address2   = request.form.get("address2", "").strip()
    city       = request.form.get("city", "").strip()
    state      = request.form.get("state", "").strip().upper()
    zipcode    = request.form.get("zipcode", "").strip()
    plan_val     = request.form.get("plan", "LOCAL")
    pay_via      = request.form.get("pay_via", "stripe")
    sys.stderr.write(f"[SUBSCRIBE] plan_val={plan_val!r} pay_via={pay_via!r} form_keys={sorted(request.form.keys())}\n")
    sys.stderr.flush()
    is_gift          = bool(request.form.get("is_gift"))
    gifter_name      = request.form.get("gifter_name", "").strip()
    gifter_email     = request.form.get("gifter_email", "").strip()
    gift_renewal     = request.form.get("gift_renewal", "auto")
    gift_auto_renew  = (gift_renewal == "auto")
    discount_code    = (request.form.get("discount_code", "").strip().upper() or
                        request.form.get("discount_code_typed", "").strip().upper())

    if not all([first_name, last_name, address1, city, state, zipcode]) or (not is_gift and not email):
        flash("Please fill in all required fields.")
        form = request.form.to_dict()
        return render_template("subscribe.html", plans=plans, form=form,
                               paypal_client_id=PAYPAL_CLIENT_ID)

    if is_gift and not (gifter_name and gifter_email):
        flash("Please fill in all required fields.")
        form = request.form.to_dict()
        return render_template("subscribe.html", plans=plans, form=form,
                               paypal_client_id=PAYPAL_CLIENT_ID)

    try:
        plan_code = PlanCode(plan_val)
    except ValueError:
        plan_code = PlanCode.LOCAL

    MA_ONLY_PLANS = {PlanCode.LOCAL, PlanCode.SENIOR, PlanCode.SNOWBIRD}
    if plan_code in MA_ONLY_PLANS and state != "MA":
        flash(f"The '{PLAN_LABELS[plan_code]}' plan is only available for Massachusetts mailing addresses. Please select the Out-of-County plan or correct your state.")
        form = request.form.to_dict()
        return render_template("subscribe.html", plans=plans, form=form,
                               paypal_client_id=PAYPAL_CLIENT_ID)

    if pay_via == "paypal" and PAYPAL_CLIENT_ID:
        paypal_price = PLAN_PRICES[plan_code]
        if discount_code:
            db_dc = SessionLocal()
            dc = db_dc.query(DiscountCode).filter_by(code=discount_code, active=True).first()
            if dc and (not dc.expires_at or dc.expires_at >= date.today()) and \
                      (dc.max_uses is None or dc.use_count < dc.max_uses):
                paypal_price = round(paypal_price * (1 - dc.discount_percent / 100), 2)
            db_dc.close()
        db = SessionLocal()
        sub = Subscriber(
            full_name=f"{first_name} {last_name}",
            email=email,
            phone=phone or None,
            address1=address1,
            address2=address2 or None,
            city=city,
            state=state,
            zipcode=zipcode,
            plan=plan_code,
            status=SubscriberStatus.EXPIRED,
            start_date=date.today(),
            auto_renew=True,
            payment_method=PaymentMethod.CREDIT_CARD,
        )
        db.add(sub)
        db.commit()
        sub_id = sub.id
        db.close()
        sys.stderr.write(f"[PAYPAL] plan={plan_code.value} price={paypal_price} discount={discount_code} sub_id={sub_id}\n")
        sys.stderr.flush()
        return redirect(url_for("subscribe_paypal", subscriber_id=sub_id,
                                amount=f"{paypal_price:.2f}", code=discount_code))

    db = SessionLocal()
    sub = Subscriber(
        full_name=f"{first_name} {last_name}",
        email=email,
        phone=phone or None,
        address1=address1,
        address2=address2 or None,
        city=city,
        state=state,
        zipcode=zipcode,
        plan=plan_code,
        status=SubscriberStatus.EXPIRED,
        start_date=date.today(),
        auto_renew=True,
        payment_method=PaymentMethod.CREDIT_CARD,
    )
    db.add(sub)
    db.commit()
    sub_id = sub.id
    db.close()

    # Stripe checkout — always full price; discount applied via Stripe coupon (duration=once)
    price_cents = int(PLAN_PRICES[plan_code] * 100)
    sub_key = os.environ.get("STRIPE_SECRET_KEY", "")
    checkout_params = {
        "mode": "subscription",
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "unit_amount": price_cents,
                "recurring": {"interval": "year"},
                "product_data": {"name": f"Duxbury Clipper — {PLAN_LABELS[plan_code]}"},
            },
            "quantity": 1,
        }],
        "metadata": {"subscriber_id": str(sub_id), "plan": plan_code.value, "is_new_subscriber": "true",
                     "gifter_name": gifter_name, "gifter_email": gifter_email,
                     "gift_renewal": gift_renewal},
        "subscription_data": {"metadata": {"subscriber_id": str(sub_id), "plan": plan_code.value, "is_new_subscriber": "true",
                                           "gifter_name": gifter_name, "gifter_email": gifter_email,
                                           "gift_renewal": gift_renewal}},
        "customer_email": gifter_email if is_gift else email,
        "success_url": f"{BASE_URL}/subscribe/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL}/subscribe",
    }
    if discount_code:
        db2 = SessionLocal()
        dc = db2.query(DiscountCode).filter_by(code=discount_code, active=True).first()
        if dc and (not dc.expires_at or dc.expires_at >= date.today()) and \
                  (dc.max_uses is None or dc.use_count < dc.max_uses):
            try:
                stripe.Coupon.retrieve(discount_code, api_key=sub_key)
            except stripe.error.InvalidRequestError:
                stripe.Coupon.create(
                    id=discount_code,
                    percent_off=dc.discount_percent,
                    duration="once",
                    api_key=sub_key,
                )
            checkout_params["discounts"] = [{"coupon": discount_code}]
            dc.use_count += 1
            db2.commit()
        db2.close()
    checkout = stripe.checkout.Session.create(**checkout_params, api_key=sub_key)
    return redirect(checkout.url)


@app.route("/subscribe/success")
def subscribe_success():
    session_id = request.args.get("session_id", "")
    if session_id and not session.get("subscriber_id"):
        try:
            sub_key = os.environ.get("STRIPE_SECRET_KEY", "")
            cs = stripe.checkout.Session.retrieve(session_id, api_key=sub_key)
            sub_id = int(cs.get("metadata", {}).get("subscriber_id", 0))
            if sub_id:
                session["subscriber_id"] = sub_id
        except Exception:
            pass
    return render_template("subscribe_success.html")


@app.route("/subscribe-paypal/<int:subscriber_id>")
def subscribe_paypal(subscriber_id):
    import requests as req
    db = SessionLocal()
    sub = db.query(Subscriber).filter_by(id=subscriber_id).first()
    if not sub:
        db.close()
        return redirect(url_for("subscribe_new"))
    plan_code = sub.plan
    discount_code = request.args.get("code", "").strip().upper()
    try:
        base_price = float(request.args.get("amount", "0"))
        if base_price <= 0:
            raise ValueError("non-positive amount")
    except (ValueError, TypeError):
        base_price = PLAN_PRICES[plan_code]
    sys.stderr.write(f"[PAYPAL_ORDER] sub={subscriber_id} plan={plan_code.value} amount_arg={request.args.get('amount')!r} price={base_price} discount={discount_code}\n")
    sys.stderr.flush()
    price = f"{base_price:.2f}"
    db.close()

    custom_id = f"{subscriber_id}:{discount_code}" if discount_code else str(subscriber_id)

    base = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
    r = req.post(f"{base}/v1/oauth2/token",
                 auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                 data={"grant_type": "client_credentials"})
    access_token = r.json().get("access_token", "")
    r2 = req.post(f"{base}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": "USD",
                    "value": price,
                    "breakdown": {"item_total": {"currency_code": "USD", "value": price}}
                },
                "items": [{
                    "name": f"Duxbury Clipper — {PLAN_LABELS[plan_code]}",
                    "description": f"Annual subscription · ${price}/yr",
                    "unit_amount": {"currency_code": "USD", "value": price},
                    "quantity": "1",
                    "category": "DIGITAL_GOODS"
                }],
                "custom_id": custom_id
            }],
            "application_context": {
                "return_url": f"{BASE_URL}/subscribe-paypal-success",
                "cancel_url": f"{BASE_URL}/subscribe",
            }
        })
    order = r2.json()
    approve_url = next((l["href"] for l in order.get("links", []) if l["rel"] == "approve"), None)
    if approve_url:
        return redirect(approve_url)
    return redirect(url_for("subscribe_new"))


@app.route("/subscribe-paypal-success")
def subscribe_paypal_success():
    import requests as req
    token = request.args.get("token")
    if not token:
        return redirect(url_for("subscribe_new"))

    base = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
    r = req.post(f"{base}/v1/oauth2/token",
                 auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                 data={"grant_type": "client_credentials"})
    access_token = r.json().get("access_token", "")
    r2 = req.post(f"{base}/v2/checkout/orders/{token}/capture",
                  headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    order = r2.json()

    if order.get("status") == "COMPLETED":
        custom_id_val = order["purchase_units"][0].get("custom_id", "")
        parts = custom_id_val.split(":", 1)
        sub_id = int(parts[0]) if parts[0].isdigit() else 0
        discount_code_used = parts[1].strip().upper() if len(parts) > 1 else ""
        amount = float(order["purchase_units"][0]["payments"]["captures"][0]["amount"]["value"])
        db = SessionLocal()
        sub = db.query(Subscriber).filter_by(id=sub_id).first()
        if sub:
            sub.status = SubscriberStatus.ACTIVE
            sub.payment_method = PaymentMethod.PAYPAL
            sub.start_date = date.today()
            new_exp = date.today().replace(year=date.today().year + 1)
            sub.expiration_date = new_exp
            pmt = Payment(subscriber_id=sub.id, amount=amount,
                          payment_method=PaymentMethod.PAYPAL,
                          notes=f"PayPal order {token} — new subscription",
                          period_start=date.today(), period_end=new_exp,
                          entered_by="PayPal (subscriber)", paid_at=datetime.utcnow())
            db.add(pmt)
            db.flush()
            db.add(SubscriberEventLog(
                subscriber_id=sub.id, event_type="SUBSCRIPTION_STARTED",
                description=f"New subscription started via PayPal. Expiration set to {new_exp}.",
                performed_by="PayPal",
            ))
            if discount_code_used:
                dc = db.query(DiscountCode).filter_by(code=discount_code_used, active=True).first()
                if dc:
                    dc.use_count += 1
            if sub.notes and sub.notes.startswith("paypal_pending:"):
                sub.notes = None
            db.commit()
            session["subscriber_id"] = sub.id
            if sub.email:
                try:
                    login_url = make_portal_link(sub, db)
                    from_email = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.net")
                    resend.Emails.send({
                        "from": f"Duxbury Clipper <{from_email}>",
                        "to": sub.email,
                        "subject": "Welcome to the Duxbury Clipper!",
                        "html": _welcome_email_html(sub.full_name.split()[0], new_exp, BASE_URL, login_url),
                    })
                except Exception as e:
                    sys.stderr.write(f"[EMAIL ERROR] new subscriber welcome: {e}\n")
            db.close()
        return render_template("subscribe_success.html")

    flash("PayPal payment was not completed.")
    return redirect(url_for("subscribe_new"))


def _welcome_email_html(first_name, expiration, base_url, login_url=None):
    first_name = first_name.capitalize()
    login_btn = (
        f"<p style='margin:24px 0;'>"
        f"<a href='{login_url}' style='background:#2e7d32;color:#fff;padding:12px 24px;"
        f"border-radius:4px;text-decoration:none;font-weight:bold;display:inline-block;'>"
        f"Log In to My Account</a></p>"
        f"<p style='font-size:0.85em;color:#888;'>This login link expires in 7 days. After that, please "
        f"visit <a href='{base_url}/login'>{base_url}/login</a> to request a new one.</p>"
        f"<p style='font-size:0.85em;color:#888;'>You can log in using your email address or the account number on your Clipper mailing label.</p>"
    ) if login_url else (
        f"<p>You can manage your account at: <a href='{base_url}/login'>Log In</a></p>"
        f"<p style='font-size:0.85em;color:#888;'>You can log in using your email address or the account number on your Clipper mailing label.</p>"
    )
    return (
        f"<p>Dear {first_name},</p>"
        f"<p>Welcome to the Duxbury Clipper! Your home delivery subscription is now active.</p>"
        f"<p>It usually takes about a week for your first issue to arrive in your mailbox, depending on the day you subscribed. "
        f"Your subscription is active through <strong>{expiration.strftime('%B %d, %Y')}</strong>.</p>"
        f"<p>You can manage your account, update your mailing address, or pause delivery anytime:</p>"
        f"{login_btn}"
        f"<p>Questions? Call 781-934-2811 or email <a href='mailto:subscribe@duxburyclipper.com'>subscribe@duxburyclipper.com</a> and talk to a real person. Please do not reply to this message — it is not monitored!</p>"
        f"<p>Thank you for subscribing to the Duxbury Clipper!</p>"
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
    test_amount = request.form.get("test_amount")
    price_cents = 100 if test_amount else int(PLAN_PRICES[plan_code] * 100)
    checkout_params = {
        "mode": "subscription",
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "unit_amount": price_cents,
                "recurring": {"interval": "year"},
                "product_data": {"name": f"Duxbury Clipper — {PLAN_LABELS[plan_code]}"},
            },
            "quantity": 1,
        }],
        "metadata": {"subscriber_id": str(sub.id), "plan": plan_code.value},
        "subscription_data": {"metadata": {"subscriber_id": str(sub.id), "plan": plan_code.value}},
        "success_url": f"{BASE_URL}/renewal-success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{BASE_URL}/account?tab=renew",
    }
    if sub.stripe_customer_id:
        checkout_params["customer"] = sub.stripe_customer_id
    else:
        checkout_params["customer_email"] = sub.email
    sub_key = os.environ.get("STRIPE_SECRET_KEY", "")
    sys.stderr.write(f"[CHECKOUT] using key prefix: {sub_key[:14]}\n")
    sys.stderr.flush()
    checkout = stripe.checkout.Session.create(**checkout_params, api_key=sub_key)
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
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    sys.stderr.write(f"[WEBHOOK] received event, sig present: {bool(sig_header)}, secret set: {bool(webhook_secret)}\n")
    sys.stderr.flush()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as e:
        sys.stderr.write(f"[WEBHOOK] signature failed: {e}\n")
        sys.stderr.flush()
        return jsonify(error="Invalid signature"), 400
    sys.stderr.write(f"[WEBHOOK] event type: {event['type']}\n")
    sys.stderr.flush()

    db = SessionLocal()

    if event["type"] == "checkout.session.completed":
        cs = event["data"]["object"]
        sub_id = cs.get("metadata", {}).get("subscriber_id")
        if sub_id:
            sub = db.query(Subscriber).filter_by(id=int(sub_id)).first()
            if sub:
                new_plan = cs.get("metadata", {}).get("plan")
                if new_plan:
                    try:
                        sub.plan = PlanCode(new_plan)
                    except ValueError:
                        pass
                sub.stripe_customer_id = cs.get("customer")
                sub.payment_method     = PaymentMethod.CREDIT_CARD
                sub.status             = SubscriberStatus.ACTIVE

                if cs.get("mode") == "subscription":
                    sub.stripe_subscription_id = cs.get("subscription")
                    sub.auto_renew = True
                    base = sub.expiration_date if (sub.expiration_date and sub.expiration_date >= date.today()) else date.today()
                    new_exp = base.replace(year=base.year + 1)
                    sub.expiration_date = new_exp
                    _reset_reminder_flags(sub)
                    amount = cs.get("amount_total", 0) / 100
                    pmt = Payment(
                        subscriber_id=sub.id, amount=amount,
                        payment_method=PaymentMethod.CREDIT_CARD,
                        stripe_payment_intent_id=cs.get("payment_intent"),
                        period_start=date.today(), period_end=new_exp,
                        entered_by="Stripe (subscriber)",
                        paid_at=datetime.utcnow(),
                    )
                    db.add(pmt)
                    db.flush()
                    db.add(SubscriberEventLog(
                        subscriber_id=sub.id, event_type="SUBSCRIPTION_STARTED",
                        description=f"Auto-renew subscription started. Expiration set to {new_exp}.",
                        performed_by="Stripe",
                    ))
                    db.commit()
                    meta = cs.get("metadata", {})
                    gifter_email    = meta.get("gifter_email", "")
                    gifter_name     = meta.get("gifter_name", "")
                    gift_auto_renew = meta.get("gift_renewal", "auto") == "auto"
                    if gifter_email and not gift_auto_renew:
                        stripe_sub_id = cs.get("subscription")
                        if stripe_sub_id:
                            try:
                                sub_key = os.environ.get("STRIPE_SECRET_KEY", "")
                                stripe.Subscription.modify(stripe_sub_id, cancel_at_period_end=True, api_key=sub_key)
                                sub.auto_renew = False
                                db.commit()
                            except Exception as e:
                                sys.stderr.write(f"[WEBHOOK] failed to set cancel_at_period_end: {e}\n")
                                sys.stderr.flush()
                    if sub.email:
                        try:
                            from_email = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.net")
                            first_name = sub.full_name.split()[0]
                            is_new = meta.get("is_new_subscriber") == "true"
                            if is_new:
                                subject = "Welcome to the Duxbury Clipper!"
                                login_url = make_portal_link(sub, db)
                                html = _welcome_email_html(first_name, new_exp, BASE_URL, login_url)
                                resend.Emails.send({
                                    "from": f"Duxbury Clipper <{from_email}>",
                                    "to": sub.email,
                                    "subject": subject,
                                    "html": html,
                                })
                                if gifter_email:
                                    gifter_first = gifter_name.split()[0] if gifter_name else "there"
                                    renew_note = (
                                        "This subscription will renew automatically each year and your card will be charged. "
                                        "You can cancel auto-renew anytime by calling 781-934-2811."
                                        if gift_auto_renew else
                                        "This is a one-year gift — it will not renew automatically. "
                                        "You're welcome to renew it next year as another gift!"
                                    )
                                    resend.Emails.send({
                                        "from": f"Duxbury Clipper <{from_email}>",
                                        "to": gifter_email,
                                        "subject": "Your gift subscription to the Duxbury Clipper is confirmed!",
                                        "html": (
                                            f"<p>Dear {gifter_first},</p>"
                                            f"<p>Thank you for gifting a Duxbury Clipper subscription to <strong>{sub.full_name}</strong>!</p>"
                                            f"<p>Their home delivery subscription is now active through <strong>{new_exp.strftime('%B %d, %Y')}</strong>. "
                                            f"They will receive the Clipper every Wednesday.</p>"
                                            f"<p>{renew_note}</p>"
                                            f"<p>Questions? Call 781-934-2811 or reply to this email.</p>"
                                            f"<p>Thank you for supporting the Duxbury Clipper!</p>"
                                        ),
                                    })
                            else:
                                subject = "Your Duxbury Clipper subscription has been renewed"
                                html = (
                                    f"<p>Dear {first_name},</p>"
                                    f"<p>Thank you! Your Duxbury Clipper subscription has been renewed and will now renew automatically each year.</p>"
                                    f"<p>Your subscription is active through <strong>{new_exp.strftime('%B %d, %Y')}</strong>.</p>"
                                    f"<p>You can view your account or manage your subscription at any time: "
                                    f"<a href='{BASE_URL}/account'>My Account</a>.</p>"
                                    f"<p>Questions? Call 781-934-2811 or reply to this email.</p>"
                                    f"<p>Thank you for being a loyal subscriber to the Duxbury Clipper!</p>"
                                )
                                resend.Emails.send({
                                    "from": f"Duxbury Clipper <{from_email}>",
                                    "to": sub.email,
                                    "subject": subject,
                                    "html": html,
                                })
                        except Exception as e:
                            sys.stderr.write(f"[EMAIL ERROR] subscription confirmation: {e}\n")
                            sys.stderr.flush()
                else:
                    # One-time payment
                    amount = cs.get("amount_total", 0) / 100
                    period_start = date.today()
                    base = sub.expiration_date if (sub.expiration_date and sub.expiration_date >= date.today()) else date.today()
                    period_end = base.replace(year=base.year + 1)
                    sub.expiration_date = period_end
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

    elif event["type"] == "invoice.paid":
        invoice        = event["data"]["object"]
        billing_reason = invoice.get("billing_reason", "")
        subscription_id = invoice.get("subscription")
        # Only process automatic renewals (not the first invoice, handled in checkout.session.completed)
        if subscription_id and billing_reason == "subscription_cycle":
            sub = db.query(Subscriber).filter_by(stripe_subscription_id=subscription_id).first()
            if sub:
                amount = invoice.get("amount_paid", 0) / 100
                pi_id  = invoice.get("payment_intent")
                period_start = date.today()
                base = sub.expiration_date if (sub.expiration_date and sub.expiration_date >= date.today()) else date.today()
                period_end = base.replace(year=base.year + 1)
                sub.expiration_date = period_end
                sub.status = SubscriberStatus.ACTIVE
                sub.auto_renew = True
                _reset_reminder_flags(sub)
                sub_name = sub.full_name
                sub_db_id = sub.id
                pmt = Payment(
                    subscriber_id=sub_db_id, amount=amount,
                    payment_method=PaymentMethod.CREDIT_CARD,
                    stripe_payment_intent_id=pi_id,
                    period_start=period_start, period_end=period_end,
                    entered_by="Stripe (auto-renew)",
                    paid_at=datetime.utcnow(),
                )
                db.add(pmt)
                db.flush()
                db.add(PaymentAuditLog(
                    action="CREATED", payment_id=pmt.id,
                    subscriber_id=sub_db_id, subscriber_name=sub_name,
                    amount=amount, payment_method="CREDIT_CARD",
                    period_start=period_start, period_end=period_end,
                    entered_by="Stripe (auto-renew)",
                ))
                db.add(SubscriberEventLog(
                    subscriber_id=sub_db_id, event_type="SUBSCRIPTION_RENEWED",
                    description=f"Auto-renew payment of ${amount:.2f} processed. Expiration extended to {period_end}.",
                    performed_by="Stripe",
                ))
                db.commit()

    elif event["type"] == "customer.subscription.deleted":
        subscription = event["data"]["object"]
        sub = db.query(Subscriber).filter_by(stripe_subscription_id=subscription.get("id")).first()
        if sub:
            sub.auto_renew = False
            sub.stripe_subscription_id = None
            db.commit()

    elif event["type"] == "invoice.payment_failed":
        invoice     = event["data"]["object"]
        customer_id = invoice.get("customer")
        sub = db.query(Subscriber).filter_by(stripe_customer_id=customer_id).first()
        if sub and sub.email:
            try:
                from_email = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.net")
                resend.Emails.send({
                    "from": f"Duxbury Clipper <{from_email}>",
                    "to": sub.email,
                    "subject": "Problem with your Duxbury Clipper renewal",
                    "html": (f"<p>Dear {sub.full_name},</p>"
                             f"<p>We were unable to process your automatic renewal. "
                             f"<a href='{BASE_URL}/account?tab=renew'>Click here to update your payment and renew</a>.</p>"
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

OBIT_BASE_FEE      = 100.00
OBIT_WORD_LIMIT    = 300
OBIT_OVERAGE_RATE  = 0.50     # per word over limit
OBIT_NOTIFY_EMAIL  = "josh@joshcutler.com"  # fallback if no DB setting

def get_obit_settings():
    """Return (notify_email, cc_list) from DB settings, falling back to defaults."""
    db = SessionLocal()
    try:
        to_row = db.query(Setting).filter_by(key="obit_notify_email").first()
        cc_row = db.query(Setting).filter_by(key="obit_notify_cc").first()
        to_addr = to_row.value if (to_row and to_row.value) else OBIT_NOTIFY_EMAIL
        cc_list = [e.strip() for e in cc_row.value.split(",") if e.strip()] if (cc_row and cc_row.value) else []
        return to_addr, cc_list
    finally:
        db.close()

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
  .masthead { background: white; text-align: center; padding: 20px 24px 12px; border-bottom: 1px solid #ddd; }
  .masthead img { max-width: 520px; width: 90%; height: auto; }
  .header { background: #1a3a1a; padding: 0; display: flex; align-items: stretch; justify-content: center; flex-wrap: wrap; }
  .header a { color: white; font-size: 0.85em; text-decoration: none; padding: 10px 14px; display: inline-flex; align-items: center; letter-spacing: 0.03em; font-family: Georgia, serif; font-weight: normal; }
  .header a:hover { background: rgba(255,255,255,0.15); }
  .header a.active { background: rgba(255,255,255,0.1); }
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
  .word-bar.over { color: #2e7d32; font-weight: 700; }
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
  .tips-toggle { background: none; border: none; color: #1a3a1a; font-family: Georgia, serif;
                 font-size: 0.9em; cursor: pointer; padding: 0; text-decoration: underline;
                 margin-bottom: 6px; display: inline-block; }
  .tips-box { display: none; background: #f0f4ee; border-left: 3px solid #1a3a1a; border-radius: 4px;
              padding: 14px 16px; margin-bottom: 12px; font-size: 0.88em; line-height: 1.6; color: #333; }
  .tips-box ol { margin: 8px 0 0 16px; padding: 0; }
  .tips-box li { margin-bottom: 6px; }
  .photo-item { display: flex; align-items: center; gap: 10px; padding: 6px 0; font-size: 0.9em; }
  .photo-item img { width: 40px; height: 40px; object-fit: cover; border: 1px solid #ccc; border-radius: 3px; }
  .photo-remove { color: #c62828; cursor: pointer; font-weight: 700; text-decoration: underline; font-size: 0.85em; background: none; border: none; padding: 0; font-family: Georgia, serif; }
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
                font-size: 0.97em; margin-top: 8px; }
  .obit-proof-name { font-weight: 700; font-size: 1.1em; margin-bottom: 10px; }
  .obit-proof-body { white-space: pre-wrap; }
  .obit-proof-photo { float: left; margin: 0 16px 8px 0; max-width: 140px; border: 1px solid #ccc; }
  .obit-proof-photo img { display: block; width: 100%; }
  .obit-proof-photo-caption { font-size: 0.75em; color: #777; text-align: center; padding: 3px 0; }
  .clearfix::after { content: ''; display: table; clear: both; }
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
<div class="masthead"><a href="https://www.duxburyclipper.com/" rel="noreferrer"><img src="https://www.duxburyclipper.com/wp-content/uploads/2019/01/logo-1-2.png" alt="Duxbury Clipper"></a></div>
<div class="header">
  <a href="https://www.duxburyclipper.com/" rel="noreferrer">&#8962; Home</a>
  <a href="https://www.duxburyclipper.com/category/news/" rel="noreferrer">News</a>
  <a href="https://www.duxburyclipper.com/category/features/" rel="noreferrer">Features</a>
  <a href="https://www.duxburyclipper.com/category/sports/" rel="noreferrer">Sports</a>
  <a href="https://www.duxburyclipper.com/category/opinion/" rel="noreferrer">Opinion</a>
  <a href="https://www.duxburyclipper.com/obituaries/" class="active" rel="noreferrer">Obituaries</a>
  <a href="https://www.duxburyclipper.com/category/classifieds/" rel="noreferrer">Classifieds</a>
  <a href="https://www.duxburyclipper.com/marketplace/" rel="noreferrer">Marketplace</a>
  <a href="https://www.duxburyclipper.com/subscribe/" rel="noreferrer">Subscribe</a>
</div>
<div class="wrap">
  <div class="steps">
    <div class="step active" id="step1-tab">1 &nbsp; Obituary Details</div>
    <div class="step" id="step2-tab">2 &nbsp; Review &amp; Pay</div>
    <div class="step" id="step3-tab">3 &nbsp; Confirmed</div>
  </div>
  <div id="page-intro">
  <h2>Place an Obituary Notice</h2>
  <p class="intro">Please use this form to place an obituary notice in the Duxbury Clipper.
  The base fee of <strong>$100</strong> includes a photo and up to 300 words.
  Longer notices are welcome — there is an additional fee of <strong>50¢ per word</strong> over 300.
  Your notice will be published on our website as soon as it is approved and then in the next available print edition of the Clipper.</p>
  </div>

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
        <label>Date of death</label>
        <input type="date" id="dod">
      </div>
    </div>

    <div class="field">
      <label>Full text of obituary *</label>
      <button type="button" class="tips-toggle" onclick="var b=document.getElementById('obit-tips');var open=b.style.display==='block';b.style.display=open?'none':'block';this.textContent=open?'▶ Tips for writing your obituary':'▼ Tips for writing your obituary';">▶ Tips for writing your obituary</button>
      <div class="tips-box" id="obit-tips">
        <p style="margin:0 0 6px;">There is no one right or wrong way to write an obituary notice, but here is a typical format:</p>
        <ol>
          <li><strong>Full Name and Age:</strong> Start with the loved one's full name (including any nicknames, maiden names, or suffixes) and their age at the time of death.</li>
          <li><strong>Date and Place of Death:</strong> Include the date and location of death. Mentioning the cause is optional and based on the family's wishes.</li>
          <li><strong>Brief Summary of Life:</strong> Share the birth date and place, parents' names, and key life details — education, career, military service, marriage, hobbies, and accomplishments. Personal touches are always welcome.</li>
          <li><strong>Survived By:</strong> List immediate surviving family members (spouse, children, grandchildren, siblings, etc.) and note anyone who predeceased them.</li>
          <li><strong>Funeral or Memorial Details:</strong> Include the date, time, and location of services, and whether they are public or private. Mention viewing hours or burial if applicable.</li>
          <li><strong>Donations or Tributes:</strong> Optionally suggest a charity for donations in lieu of flowers.</li>
        </ol>
        <p style="margin:8px 0 0;">If you are unsure what to write, we encourage you to <a href="https://www.duxburyclipper.com/obituaries/" style="color:#1a3a1a;">review previously published obituary notices</a> on our website.</p>
      </div>
      <textarea id="obit_text" rows="12" placeholder="Type or paste your obituary here..." required></textarea>
      <div class="word-bar" id="word_bar">0 words — base fee covers up to 300</div>
    </div>

    <div class="price-box">
      Obituary Notice &nbsp;|&nbsp; Price: <span id="price_display">$100.00</span>
    </div>

    <div class="section-note">First 300 words included in base $100 fee. Each additional word is 50¢.</div>

    <h3>Photo of Loved One</h3>
    <div class="field">
      <label>Upload photo (optional)</label>
      <button type="button" class="tips-toggle" onclick="var b=document.getElementById('photo-tips');var open=b.style.display==='block';b.style.display=open?'none':'block';this.textContent=open?'▶ Photo upload guidelines':'▼ Photo upload guidelines';">▶ Photo upload guidelines</button>
      <div class="tips-box" id="photo-tips">
        <ul style="margin:4px 0 0 16px;padding:0;">
          <li>A clear headshot is best; avoid using a group photo.</li>
          <li>If there are multiple people in your photo make sure it's easy to identify your loved one.</li>
          <li>Photos can be old or new. This is your choice.</li>
          <li>Avoid taking a picture of a picture; scans or digital originals should be at least 150 dpi resolution.</li>
          <li>The best quality photo is a digital original — a jpeg file downloaded directly from a camera, smartphone, or online gallery.</li>
          <li>Please note that while your photo will be in color on our website, it may be published in black and white in the print edition of the Clipper.</li>
        </ul>
      </div>
      <input type="file" id="photo_upload" accept=".jpg,.jpeg,.png">
      <div id="photo_preview" style="margin-top:10px;"></div>
    </div>

    <h3>Your Contact Info</h3>
    <p class="hint" style="margin-top:-4px;margin-bottom:12px;">This will <span style="text-decoration:underline;">not</span> be published.</p>

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
    </div>

    <div class="consent-row">
      <input type="checkbox" id="consent" required>
      <div class="consent-text">I confirm that I'm a family member or funeral home representative with permission to submit this obituary.
      I've done my best to ensure all information is accurate and take responsibility for the details provided.
      I understand the newspaper may decline to publish notices that don't meet its guidelines.</div>
    </div>

    <div id="form-err" class="msg-err" style="display:none;padding:12px 16px;border-radius:6px;margin-top:12px;"></div>
    <div id="no-photo-warn" style="display:none;color:#e65100;font-weight:700;margin-top:12px;">⚠️ No photo has been uploaded. Consider going back to add one.</div>
    <button id="review-btn" type="button">Review My Submission &rarr;</button>
  </div>

  <!-- Step 2: Review & Pay -->
  <div id="review-panel" style="display:none;">
    <button id="edit-btn" type="button">&larr; Edit My Submission</button>

    <div class="proof-box">
      <h3>&#128260; Obituary Proof — Review Carefully Before Paying</h3>
      <div class="obit-proof clearfix">
        <div id="proof-photo-wrap"></div>
        <div class="obit-proof-name" id="proof-name"></div>
        <div class="obit-proof-body" id="proof-body"></div>
      </div>
      <p style="font-size:0.78em;color:#777;margin:10px 0 0;font-style:italic;">Please note this proof is for general review purposes only. It is not an exact replica and the formatting may vary when published.</p>
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
      <div class="review-row"><div class="review-label">Your Name</div><div class="review-val" id="rv-name"></div></div>
      <div class="review-row"><div class="review-label">Phone</div><div class="review-val" id="rv-phone"></div></div>
      <div class="review-row"><div class="review-label">Email</div><div class="review-val" id="rv-email"></div></div>
      <div class="review-row"><div class="review-label">Relation</div><div class="review-val" id="rv-relation"></div></div>
      <div class="review-row"><div class="review-label">Date Submitted</div><div class="review-val" id="rv-submitted"></div></div>
    </div>

    <p style="font-size:0.82em;color:#555;font-style:italic;margin:0 0 20px;">Your notice will be published on our website as soon as it is approved and then in the next available print edition of the Clipper. Our print deadline is generally Friday by noon for the following Wednesday's paper.</p>
    <div class="review-section">
      <h3>Payment</h3>
      <div class="price-box" style="margin-bottom:12px;">
        Total due: <span id="rv-price">$100.00</span>
      </div>
      <div class="section-note" id="rv-price-breakdown"></div>
      <div id="payment-request-btn" style="display:none;margin-bottom:12px;"></div>
      <div id="pr-divider" style="display:none;text-align:center;margin:10px 0;font-size:0.85em;color:#888;">— or pay by card —</div>
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
    <p style="color:#777;font-size:0.9em;">Questions? Email <a href="mailto:obits@duxburyclipper.com">obits@duxburyclipper.com</a> or call us at 781-934-2811.</p>
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

// ── Apple Pay / Google Pay ─────────────────────────────────────────────────
let prPaymentMethod = null;
const pr = stripe.paymentRequest({
  country: 'US',
  currency: 'usd',
  total: { label: 'Obituary Notice — Duxbury Clipper', amount: 10000 },
  requestPayerName: false,
  requestPayerEmail: false,
});
pr.canMakePayment().then(result => {
  if (result) {
    const prButton = elements.create('paymentRequestButton', { paymentRequest: pr, style: { paymentRequestButton: { height: '44px' } } });
    prButton.mount('#payment-request-btn');
    document.getElementById('payment-request-btn').style.display = 'block';
    document.getElementById('pr-divider').style.display = 'block';
  }
});
pr.on('paymentmethod', async (ev) => {
  prPaymentMethod = ev.paymentMethod.id;
  ev.complete('success');
  document.getElementById('submit-btn').click();
});

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
    bar.textContent = words + ' words — ' + extra + ' additional words × $0.50 = $' + (extra * 0.50).toFixed(2);
    bar.className = 'word-bar over';
  } else {
    bar.textContent = words + ' words — base fee covers up to 300 words';
    bar.className = 'word-bar';
  }
  document.getElementById('price_display').textContent = fmt(price);
}
document.getElementById('obit_text').addEventListener('input', updatePrice);

// ── Photo selection with remove/replace support ────────────────────────────
let selectedPhotos = [];
const photoInput = document.getElementById('photo_upload');

function syncPhotoInput() {
  const dt = new DataTransfer();
  selectedPhotos.forEach(f => dt.items.add(f));
  photoInput.files = dt.files;
}

function renderPhotoPreview() {
  const preview = document.getElementById('photo_preview');
  preview.innerHTML = '';
  selectedPhotos.forEach((f, idx) => {
    const url = URL.createObjectURL(f);
    const item = document.createElement('div');
    item.className = 'photo-item';
    const img = document.createElement('img');
    img.src = url;
    const lbl = document.createElement('span');
    lbl.textContent = f.name;
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'photo-remove';
    removeBtn.textContent = 'Remove';
    removeBtn.addEventListener('click', function() {
      selectedPhotos.splice(idx, 1);
      syncPhotoInput();
      renderPhotoPreview();
    });
    item.appendChild(img);
    item.appendChild(lbl);
    item.appendChild(removeBtn);
    preview.appendChild(item);
  });
  if (selectedPhotos.length > 0) document.getElementById('no-photo-warn').style.display = 'none';
}

photoInput.addEventListener('change', function() {
  const newFiles = Array.from(this.files);
  selectedPhotos = newFiles.slice(0, 1);
  syncPhotoInput();
  renderPhotoPreview();
});

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
  const dod_raw        = document.getElementById('dod').value;
  const dod            = dod_raw ? new Date(dod_raw + 'T12:00:00').toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'}) : '';
  const obit_text      = document.getElementById('obit_text').value.trim();
  const first_name     = document.getElementById('first_name').value.trim();
  const last_name      = document.getElementById('last_name').value.trim();
  const phone          = document.getElementById('phone').value.trim();
  const email          = document.getElementById('email').value.trim();
  const email_confirm  = document.getElementById('email_confirm').value.trim();
  const consent        = document.getElementById('consent').checked;
  const relation       = document.querySelector('input[name="relation"]:checked').value;
  const relation_other = document.getElementById('relation_other').value.trim();
  const missing = [];
  if (!deceased_name) missing.push('Full name of deceased');
  if (!age)           missing.push('Age at death');
  if (!obit_text)     missing.push('Obituary text');
  if (!first_name)    missing.push('Your first name');
  if (!last_name)     missing.push('Your last name');
  if (!phone)         missing.push('Phone number');
  if (!email)         missing.push('Email address');
  if (missing.length > 0) {
    errBox.textContent = 'Please fill in the following required fields: ' + missing.join(', ') + '.';
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
  if (parseInt(age, 10) > 110) {
    errBox.textContent = 'Age at death (' + age + ') seems unusually high — please double-check this is correct.';
    errBox.style.display = 'block'; errBox.scrollIntoView({behavior:'smooth'}); return;
  }
  if (deceased_name.replace(/\\s/g, '').length < 4 || deceased_name.indexOf(' ') === -1) {
    errBox.textContent = 'Please enter the deceased\\'s full name (first and last).';
    errBox.style.display = 'block'; errBox.scrollIntoView({behavior:'smooth'}); return;
  }

  const noPhotoWarn = document.getElementById('no-photo-warn');
  const hasPhoto = document.getElementById('photo_upload').files.length > 0;
  if (!hasPhoto && noPhotoWarn.style.display !== 'block') {
    noPhotoWarn.style.display = 'block';
    noPhotoWarn.scrollIntoView({behavior:'smooth'});
    return;
  }
  noPhotoWarn.style.display = 'none';

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
  const rvWords = document.getElementById('rv-words');
  rvWords.textContent = words + ' words';
  rvWords.style.color = words < 100 ? '#c62828' : '';
  document.getElementById('rv-name').textContent = first_name + ' ' + last_name;
  document.getElementById('rv-phone').textContent = phone;
  document.getElementById('rv-email').textContent = email;
  document.getElementById('rv-relation').textContent = relation_display;
  document.getElementById('rv-submitted').textContent = new Date().toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit'});

  // Photo thumbnails in review row + photo in proof
  const photoFiles = document.getElementById('photo_upload').files;
  const photoEl = document.getElementById('rv-photos');
  const proofPhotoWrap = document.getElementById('proof-photo-wrap');
  proofPhotoWrap.innerHTML = '';
  proofPhotoWrap.className = '';
  if (photoFiles.length === 0) {
    photoEl.innerHTML = '<span style="color:#c62828;font-weight:700;">No photo uploaded</span>';
  } else {
    photoEl.innerHTML = '';
    Array.from(photoFiles).slice(0,1).forEach((f, i) => {
      const url = URL.createObjectURL(f);
      const sizeKB = Math.round(f.size / 1024);
      const sizeTxt = sizeKB >= 1024 ? (sizeKB/1024).toFixed(1) + ' MB' : sizeKB + ' KB';
      const tooSmall = f.size < 102400; // warn under 100 KB
      // Thumbnail in review table
      const img = document.createElement('img');
      img.className = 'photo-thumb';
      img.src = url;
      photoEl.appendChild(img);
      const lbl = document.createElement('span');
      lbl.style.fontSize = '0.85em';
      lbl.textContent = ' ' + f.name + ' (' + sizeTxt + ')';
      photoEl.appendChild(lbl);
      if (tooSmall) {
        const warn = document.createElement('div');
        warn.style.cssText = 'color:#c62828;font-size:0.82em;font-weight:700;margin-top:3px;';
        warn.textContent = '⚠️ This photo may be too small for quality print reproduction. We recommend at least 100 KB. You may want to go back and upload a higher-resolution image.';
        photoEl.appendChild(warn);
      }
      // First photo floated directly in proof box
      if (i === 0) {
        proofPhotoWrap.className = 'obit-proof-photo';
        const pi = document.createElement('img');
        pi.src = url;
        proofPhotoWrap.appendChild(pi);
      }
    });
  }

  const priceStr = fmt(price);
  document.getElementById('rv-price').textContent = priceStr;
  document.getElementById('btn-price').textContent = priceStr;
  pr.update({total: {label: 'Obituary Notice — Duxbury Clipper', amount: Math.round(price * 100)}});
  document.getElementById('rv-price-breakdown').textContent =
    extra > 0
      ? '$' + BASE_FEE.toFixed(2) + ' base + ' + extra + ' extra words × $0.50 = ' + priceStr
      : '$' + BASE_FEE.toFixed(2) + ' base fee';

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
  const dod_raw        = document.getElementById('dod').value;
  const dod            = dod_raw ? new Date(dod_raw + 'T12:00:00').toLocaleDateString('en-US', {month:'long', day:'numeric', year:'numeric'}) : '';
  const obit_text      = document.getElementById('obit_text').value.trim();
  const first_name     = document.getElementById('first_name').value.trim();
  const last_name      = document.getElementById('last_name').value.trim();
  const phone          = document.getElementById('phone').value.trim();
  const email          = document.getElementById('email').value.trim();
  const relation       = document.querySelector('input[name="relation"]:checked').value;
  const relation_other = document.getElementById('relation_other').value.trim();
  const words          = countWords(obit_text);
  const price          = calcPrice(words);

  btn.disabled = true;
  btn.textContent = 'Processing…';

  let pmId = prPaymentMethod;
  if (!pmId) {
    const {paymentMethod, error} = await stripe.createPaymentMethod({type: 'card', card: card});
    if (error) {
      show_err(error.message);
      btn.disabled = false; btn.textContent = 'Submit & Pay ' + fmt(price); return;
    }
    pmId = paymentMethod.id;
  }
  prPaymentMethod = null;

  // Update payment request amount in case word count changed
  pr.update({total: {label: 'Obituary Notice — Duxbury Clipper', amount: Math.round(price * 100)}});

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
  formData.append('words', words);
  formData.append('amount_cents', Math.round(price * 100));
  formData.append('payment_method_id', pmId);
  const photos = document.getElementById('photo_upload').files;
  for (let i = 0; i < Math.min(photos.length, 1); i++) {
    formData.append('photos', photos[i]);
  }

  try {
    const resp = await fetch('/obituary/submit', {method: 'POST', body: formData});
    const data = await resp.json();
    if (data.success) {
      document.getElementById('review-panel').style.display = 'none';
      document.getElementById('page-intro').style.display = 'none';
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
    pk = OBIT_STRIPE_PUBLISHABLE_KEY or STRIPE_PUBLISHABLE_KEY
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
    pub_timing    = "Online as soon as processed, then next available print edition"
    words         = int(request.form.get("words", "0"))
    amount_cents  = int(request.form.get("amount_cents", "10000"))
    pm_id         = request.form.get("payment_method_id", "").strip()
    user_agent    = request.headers.get("User-Agent", "Unknown")
    ip_address    = request.headers.get("X-Forwarded-For", request.remote_addr or "Unknown").split(",")[0].strip()

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
            api_key=OBIT_STRIPE_SECRET_KEY or None,
        )
        if pi.status != "succeeded":
            return jsonify({"error": f"Payment status: {pi.status}"}), 400
    except stripe.error.CardError as e:
        return jsonify({"error": e.user_message or str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    amount_paid = amount_cents / 100
    confirmation_code = pi.id
    try:
        pm_obj   = stripe.PaymentMethod.retrieve(pm_id, api_key=OBIT_STRIPE_SECRET_KEY or None)
        card_brand = pm_obj.card.brand.capitalize()
        card_last4 = pm_obj.card.last4
        card_desc  = f"{card_brand} ending in {card_last4}"
    except Exception:
        card_desc = "Card"

    # Attach photos (built before email body so we can reference attachment status)
    attachments = []
    for photo in request.files.getlist("photos"):
        if photo and photo.filename:
            data = photo.read()
            attachments.append({
                "filename": photo.filename,
                "content": list(data),
            })

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
      <td style="padding:6px 12px;"><strong>${amount_paid:.2f}</strong></td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Card</td>
      <td style="padding:6px 12px;">{card_desc}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Confirmation #</td>
      <td style="padding:6px 12px;">{confirmation_code}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">IP Address</td>
      <td style="padding:6px 12px;">{ip_address}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Browser / Device</td>
      <td style="padding:6px 12px;font-size:12px;">{user_agent}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Submitter</td>
      <td style="padding:6px 12px;">{first_name} {last_name}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Phone</td>
      <td style="padding:6px 12px;">{phone}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Email</td>
      <td style="padding:6px 12px;">{email}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Relation</td>
      <td style="padding:6px 12px;">{relation}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Photo</td>
      <td style="padding:6px 12px;">{"Attached" if attachments else "Not Submitted"}</td></tr>
</table>
<h3 style="margin-top:24px;">Obituary Text ({words} words)</h3>
<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:4px;padding:16px;
            font-family:Georgia,serif;font-size:14px;line-height:1.7;white-space:pre-wrap;"><strong>{deceased_name}, age {age}{(", " + dod) if dod else ""}</strong>

{obit_text}</div>
"""

    # Save submission to database
    try:
        db = SessionLocal()
        submission = ObituarySubmission(
            deceased_name        = deceased_name,
            age                  = age,
            date_of_death        = dod,
            obit_text            = obit_text,
            word_count           = words,
            submitter_first_name = first_name,
            submitter_last_name  = last_name,
            submitter_email      = email,
            submitter_phone      = phone,
            relation             = relation,
            pub_timing           = pub_timing,
            photo_submitted      = bool(attachments),
            amount_paid          = amount_paid,
            card_description     = card_desc,
            stripe_pi_id         = confirmation_code,
            ip_address           = ip_address,
            user_agent           = user_agent,
        )
        db.add(submission)
        db.commit()
        db.close()
    except Exception:
        pass  # don't fail over a DB write error

    # Send staff notification
    notify_to, notify_cc = get_obit_settings()
    staff_email = {
        "from": "Duxbury Clipper <noreply@duxburyclipper.net>",
        "to": [notify_to],
        "subject": f"Obituary Notice: {deceased_name} — ${amount_paid:.2f} paid",
        "html": body_html,
        "attachments": attachments,
    }
    if notify_cc:
        staff_email["cc"] = notify_cc
    try:
        resend.Emails.send(staff_email)
    except Exception as e:
        sys.stderr.write(f"[OBIT-STAFF-EMAIL-ERROR] to={notify_to} error={e}\n")

    # Confirmation email to submitter
    try:
        resend.Emails.send({
            "from": "Duxbury Clipper <noreply@duxburyclipper.net>",
            "to": [email],
            "subject": f"Obituary Notice Received — {deceased_name}",
            "html": f"""
<p>Dear {first_name},</p>
<p>Thank you for submitting an obituary notice for <strong>{deceased_name}</strong> to the Duxbury Clipper.</p>
<p>We have received your submission and processed payment of <strong>${amount_paid:.2f}</strong> on your {card_desc}.</p>
<p><strong>Confirmation number:</strong> {confirmation_code}</p>
<p>We will be in touch if we have any questions before publication. If you need to make changes or have questions, please email <a href="mailto:obits@duxburyclipper.com">obits@duxburyclipper.com</a> or call us at <strong>781-934-2811</strong>.</p>
<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">
<h3 style="font-family:Georgia,serif;color:#1a3a1a;margin-bottom:12px;">Your Submission</h3>
<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:14px;margin-bottom:20px;">
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;width:160px;">Deceased</td>
      <td style="padding:6px 12px;">{deceased_name}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Age</td>
      <td style="padding:6px 12px;">{age}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Date of Death</td>
      <td style="padding:6px 12px;">{dod if dod else "—"}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Word Count</td>
      <td style="padding:6px 12px;">{words} words</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Amount Charged</td>
      <td style="padding:6px 12px;">${amount_paid:.2f}</td></tr>
  <tr><td style="padding:6px 12px;background:#f5f5f5;font-weight:700;">Photo</td>
      <td style="padding:6px 12px;">{"Submitted" if attachments else "Not submitted"}</td></tr>
</table>
<h4 style="font-family:Georgia,serif;color:#1a3a1a;margin-bottom:8px;">Obituary Text</h4>
<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:4px;padding:16px;
            font-family:Georgia,serif;font-size:14px;line-height:1.7;white-space:pre-wrap;"><strong>{deceased_name}, age {age}</strong>

{obit_text}</div>
<p style="color:#777;font-size:0.9em;margin-top:24px;">The Duxbury Clipper &mdash; duxburyclipper.com</p>
""",
        })
    except Exception:
        pass

    return jsonify({"success": True, "amount": amount_paid})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
