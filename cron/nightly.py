#!/usr/bin/env python3
"""
Clipper Circulation — nightly renewal cron job.

Run once per day (e.g. 6 AM):
    /path/to/venv/bin/python /path/to/clipper-circ/cron/nightly.py

Set RESEND_API_KEY and FROM_EMAIL in environment (or .env file).
If RESEND_API_KEY is not set, the script runs in DRY RUN mode —
it logs what it would send without actually sending anything.
"""

import sys
import os
import secrets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timedelta
from database import SessionLocal
from models import Subscriber, SubscriberStatus, DeliveryHold

# ── Config ─────────────────────────────────────────────────────────────────────

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except ImportError:
    pass

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.com")
FROM_NAME      = os.environ.get("FROM_NAME",  "Duxbury Clipper")
BASE_URL       = os.environ.get("BASE_URL",   "https://www.duxburyclipper.com")
PORTAL_URL     = os.environ.get("PORTAL_URL", BASE_URL)
TOKEN_TTL_DAYS = 7
DRY_RUN        = not bool(RESEND_API_KEY)

# ── Load settings.json (email templates + schedule) ───────────────────────────

import json as _json
_SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

def _load_settings() -> dict:
    if os.path.exists(_SETTINGS_FILE):
        with open(_SETTINGS_FILE) as f:
            return _json.load(f)
    return {}

_settings = _load_settings()

_SCHEDULE = {
    "reminder_35_days": 35,
    "reminder_21_days": 21,
    "reminder_14_days": 14,
    "grace_14_days":    14,
    "grace_final_days": 27,
    **_settings.get("email_schedule", {}),
}

_DEFAULT_TEMPLATES = {
    "reminder_35": {
        "subject": "Your Duxbury Clipper subscription — time to renew soon",
        "body": "Your subscription expires on {expiration_date} — about 5 issues from now.\n\nTo keep your weekly Clipper coming without any interruption, please renew at your convenience.\n\nThank you for supporting your hometown paper!",
    },
    "reminder_21": {
        "subject": "Reminder: Your Duxbury Clipper subscription expires soon",
        "body": "Just a friendly reminder — your subscription expires on {expiration_date}, about 3 issues from now.\n\nRenew now to make sure you don't miss a single edition.",
    },
    "reminder_14": {
        "subject": "Only 2 issues left on your Duxbury Clipper subscription",
        "body": "Your Duxbury Clipper subscription expires on {expiration_date} — you have about 2 issues remaining.\n\nPlease renew today to avoid any interruption in your home delivery.",
    },
    "expire_day": {
        "subject": "Your Duxbury Clipper subscription expires today",
        "body": "Your Duxbury Clipper subscription expires today.\n\nThe good news — we'll continue delivering your paper for up to 4 more weeks as a courtesy while you renew. Please don't let it lapse!",
    },
    "grace_14": {
        "subject": "Action needed: Your Duxbury Clipper subscription is past due",
        "body": "Your Duxbury Clipper subscription expired on {expiration_date}.\n\nWe've continued delivering your paper as a courtesy, but home delivery will stop in about 2 weeks if we don't hear from you.\n\nWe'd love to keep you on our list — please renew when you get a chance.",
    },
    "grace_final": {
        "subject": "Final notice — Duxbury Clipper delivery stopping this week",
        "body": "We're sorry to say that your Duxbury Clipper home delivery will stop this week unless you renew.\n\nYour subscription expired on {expiration_date} and we haven't yet received a renewal. We truly value your readership and hope to keep you on our list.\n\nThank you for being a loyal reader of your hometown paper.",
    },
}

def _get_template(key: str) -> dict:
    saved = _settings.get("email_templates", {}).get(key, {})
    default = _DEFAULT_TEMPLATES[key]
    return {
        "subject": saved.get("subject") or default["subject"],
        "body":    saved.get("body")    or default["body"],
    }

def _fill(template_str: str, sub: "Subscriber") -> str:
    from models import PLAN_PRICES
    exp = sub.expiration_date.strftime("%B %d, %Y") if sub.expiration_date else "—"
    parts = sub.full_name.split() if sub.full_name else []
    first = sub.full_name if (not parts or parts[0].lower() in ("the", "estate", "family")) else parts[0]
    price = f"${PLAN_PRICES.get(sub.plan, 0):.2f}"
    return (template_str
            .replace("{full_name}", sub.full_name)
            .replace("{first_name}", first)
            .replace("{expiration_date}", exp)
            .replace("{price}", price))

if DRY_RUN:
    print("⚠️  No RESEND_API_KEY set — running in DRY RUN mode (no emails will be sent).")

if RESEND_API_KEY:
    import resend
    resend.api_key = RESEND_API_KEY


# ── Email helper ───────────────────────────────────────────────────────────────

def make_portal_link(sub: Subscriber, db) -> str:
    token = secrets.token_urlsafe(32)
    sub.portal_token = token
    sub.portal_token_expires = datetime.utcnow() + timedelta(days=TOKEN_TTL_DAYS)
    db.commit()
    return f"{PORTAL_URL}/t/{token}"


def send_email(to_email: str, subject: str, html: str, subscriber_name: str) -> bool:
    if not to_email:
        return False
    if DRY_RUN:
        print(f"  [DRY RUN] Would email {subscriber_name} <{to_email}>: {subject}")
        return True
    try:
        resend.Emails.send({
            "from": f"{FROM_NAME} <{FROM_EMAIL}>",
            "to": to_email,
            "subject": subject,
            "html": html,
        })
        return True
    except Exception as e:
        print(f"  ✗ Failed to send to {to_email}: {e}")
        return False


# ── Email templates ────────────────────────────────────────────────────────────

def _base(first_name: str, body_html: str, btn_html: str, price: str = "", portal_link: str = "#",
          plan_label: str = "") -> str:
    plan_box = ""
    if plan_label or price:
        try:
            cents = float(price.replace('$','')) / 52 * 100
            if cents < 100:
                per_issue = f"Just {int(round(cents))} cents a week!"
            else:
                per_issue = f"Just ${cents/100:.2f} a week!"
        except Exception:
            per_issue = ""
        plan_box = (
            f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px;">'
            f'<tr><td style="background:#f0f7f0;border-left:4px solid #2e7d32;border-radius:4px;padding:12px 16px;">'
            f'<p style="margin:0;font-size:13px;color:#555;line-height:1.8;">'
            + (f'<strong>Plan:</strong> {plan_label}<br>' if plan_label else '')
            + (f'<strong>Renewal rate:</strong> {price}/year' + (f' &nbsp;•&nbsp; {per_issue}' if per_issue else '') if price else '')
            + '</p></td></tr></table>'
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Georgia,serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:30px 0;">
  <tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

      <!-- Header banner with logo -->
      <tr><td style="background:#1a3a1a;padding:24px 36px;text-align:center;">
        <div style="display:inline-block;background:#ffffff;border-radius:10px;padding:8px 20px;">
          <img src="https://www.duxburyclipper.com/wp-content/uploads/2019/01/logo-1-2.png"
               alt="The Duxbury Clipper"
               style="max-width:200px;height:auto;display:block;">
        </div>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:32px 36px;">
        <p style="margin:0 0 18px;font-size:16px;color:#222;">Dear {first_name},</p>
        {body_html}
        {plan_box}
        {btn_html}
      </td></tr>

      <!-- Account link -->
      <tr><td style="padding:0 36px 20px;">
        <p style="margin:0;font-size:13px;color:#666;line-height:1.6;text-align:center;">
          You can also <a href="{portal_link}" style="color:#2e7d32;">log into your account</a> to update your mailing address, change your subscription type, or place your delivery on hold.
        </p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#f9f9f9;border-top:1px solid #eee;padding:18px 36px;">
        <p style="margin:0;font-size:12px;color:#999;line-height:1.9;text-align:center;">
          The Duxbury Clipper &nbsp;•&nbsp; P.O. Box 1656, Duxbury, MA 02331 &nbsp;•&nbsp; <a href="https://www.duxburyclipper.com" style="color:#2e7d32;">www.duxburyclipper.com</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body></html>"""


def _renew_btn(link: str, label: str = "Renew My Subscription", color: str = "#2e7d32") -> str:
    return (f'<table width="100%" cellpadding="0" cellspacing="0" style="margin:28px 0 20px;">'
            f'<tr><td align="center">'
            f'<a href="{link}" style="background:{color};color:#ffffff;padding:14px 36px;'
            f'border-radius:6px;text-decoration:none;font-weight:bold;font-size:16px;'
            f'font-family:Arial,sans-serif;display:inline-block;">{label}</a>'
            f'</td></tr></table>'
            f'<p style="text-align:center;font-size:11px;color:#bbb;margin:0 0 20px;">'
            f'Button not working? Copy this link into your browser:<br>{link}</p>')


def _body_paragraphs(text: str) -> str:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    return "".join(f'<p style="margin:0 0 16px;font-size:15px;color:#333;line-height:1.7;">{p}</p>' for p in paras)


def _make_email(key: str, sub: Subscriber, portal_link: str,
                btn_label: str = "Renew My Subscription",
                btn_color: str = "#2e7d32") -> tuple[str, str]:
    from models import PLAN_PRICES, PLAN_LABELS
    tmpl = _get_template(key)
    subject = _fill(tmpl["subject"], sub)
    body_text = _fill(tmpl["body"], sub)
    parts = sub.full_name.split() if sub.full_name else []
    first = sub.full_name if (not parts or parts[0].lower() in ("the", "estate", "family")) else parts[0]
    price = f"${PLAN_PRICES.get(sub.plan, 0):.2f}"
    plan_label = PLAN_LABELS.get(sub.plan, "")
    html = _base(first, _body_paragraphs(body_text), _renew_btn(portal_link, btn_label, btn_color),
                 price=price, portal_link=portal_link, plan_label=plan_label)
    return subject, html


def email_35_days(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("reminder_35", sub, portal_link)

def email_21_days(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("reminder_21", sub, portal_link, "Renew Now")

def email_14_days(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("reminder_14", sub, portal_link)

def email_expire_day(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("expire_day", sub, portal_link, "Renew Before Delivery Stops", "#c62828")

def email_grace_14(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("grace_14", sub, portal_link, "Renew Now — Keep My Paper Coming", "#c62828")

def email_grace_final(sub: Subscriber, portal_link: str) -> tuple[str, str]:
    return _make_email("grace_final", sub, portal_link, "Renew and Keep My Subscription", "#c62828")


# ── Main logic ─────────────────────────────────────────────────────────────────

def run():
    db = SessionLocal()
    today = date.today()
    counts = {"status_advanced": 0, "emails_sent": 0, "emails_skipped": 0}

    print(f"\n{'='*60}")
    print(f"Clipper nightly job — {today}")
    print(f"{'='*60}")

    # ── 1. Advance statuses ────────────────────────────────────────────────────
    print("\n▶ Advancing statuses...")

    # ── Hold status flips ──────────────────────────────────────────────────────
    # Holds starting today → ON_HOLD
    holds_starting = db.query(DeliveryHold).filter(
        DeliveryHold.hold_start == today
    ).all()
    for h in holds_starting:
        s = db.query(Subscriber).filter_by(id=h.subscriber_id).first()
        if s and s.status not in (SubscriberStatus.ON_HOLD, SubscriberStatus.CANCELLED):
            print(f"  {s.full_name}: → ON_HOLD (hold starts today)")
            s.status = SubscriberStatus.ON_HOLD
            counts["status_advanced"] += 1

    # Holds ending yesterday → restore ACTIVE (if no other active holds)
    holds_ended = db.query(DeliveryHold).filter(
        DeliveryHold.hold_end == today
    ).all()
    for h in holds_ended:
        s = db.query(Subscriber).filter_by(id=h.subscriber_id).first()
        if s and s.status == SubscriberStatus.ON_HOLD:
            still_on_hold = db.query(DeliveryHold).filter(
                DeliveryHold.subscriber_id == s.id,
                DeliveryHold.hold_start <= today,
                DeliveryHold.hold_end > today,
            ).count()
            if still_on_hold == 0:
                print(f"  {s.full_name}: ON_HOLD → ACTIVE (hold ended today)")
                s.status = SubscriberStatus.ACTIVE
                counts["status_advanced"] += 1

    db.commit()

    # ACTIVE → GRACE when past expiry
    newly_grace = db.query(Subscriber).filter(
        Subscriber.status == SubscriberStatus.ACTIVE,
        Subscriber.expiration_date < today,
        Subscriber.expiration_date.isnot(None),
    ).all()
    for s in newly_grace:
        print(f"  {s.full_name}: ACTIVE → GRACE (expired {s.expiration_date})")
        s.status = SubscriberStatus.GRACE
        counts["status_advanced"] += 1

    # GRACE → EXPIRED after 28 days past expiry
    grace_cutoff = today - timedelta(days=28)
    newly_expired = db.query(Subscriber).filter(
        Subscriber.status == SubscriberStatus.GRACE,
        Subscriber.expiration_date < grace_cutoff,
        Subscriber.expiration_date.isnot(None),
    ).all()
    for s in newly_expired:
        print(f"  {s.full_name}: GRACE → EXPIRED (expired {s.expiration_date}, 28-day window passed)")
        s.status = SubscriberStatus.EXPIRED
        counts["status_advanced"] += 1

    db.commit()
    print(f"  {counts['status_advanced']} status changes committed.")

    # ── 2. Send renewal emails ─────────────────────────────────────────────────
    print("\n▶ Checking renewal emails...")

    active_subs = db.query(Subscriber).filter(
        Subscriber.status.in_([SubscriberStatus.ACTIVE, SubscriberStatus.GRACE]),
        Subscriber.expiration_date.isnot(None),
        Subscriber.email.isnot(None),
        Subscriber.email != "",
    ).all()

    for s in active_subs:
        exp = s.expiration_date
        days_to_exp  = (exp - today).days          # negative means past expiry
        days_past_exp = (today - exp).days          # positive means past expiry

        tasks = []  # list of (condition, flag_attr, email_fn)

        # Pre-expiry (thresholds from settings.json)
        if days_to_exp <= _SCHEDULE["reminder_35_days"] and not s.reminder_35_sent:
            tasks.append(("reminder_35_sent",  email_35_days))
        if days_to_exp <= _SCHEDULE["reminder_21_days"] and not s.reminder_21_sent:
            tasks.append(("reminder_21_sent",  email_21_days))
        if days_to_exp <= _SCHEDULE["reminder_14_days"] and not s.reminder_14_sent:
            tasks.append(("reminder_14_sent",  email_14_days))
        if days_to_exp <= 0 and not s.reminder_expire_sent:
            tasks.append(("reminder_expire_sent", email_expire_day))

        # Grace period (post-expiry)
        if days_past_exp >= _SCHEDULE["grace_14_days"] and not s.grace_14_sent:
            tasks.append(("grace_14_sent",     email_grace_14))
        if days_past_exp >= _SCHEDULE["grace_final_days"] and not s.grace_final_sent:
            tasks.append(("grace_final_sent",  email_grace_final))

        # Send only the most urgent unsent email per run to avoid flooding
        if tasks:
            # Take the last one in the list (most advanced / most urgent)
            flag_attr, email_fn = tasks[-1]
            portal_link = make_portal_link(s, db)
            subject, html = email_fn(s, portal_link)
            ok = send_email(s.email, subject, html, s.full_name)
            if ok:
                setattr(s, flag_attr, True)
                counts["emails_sent"] += 1
                if not DRY_RUN:
                    print(f"  ✓ Sent '{subject[:50]}...' to {s.full_name}")
            else:
                counts["emails_skipped"] += 1

    db.commit()

    # ── Summary ────────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print(f"  Status changes : {counts['status_advanced']}")
    print(f"  Emails sent    : {counts['emails_sent']}")
    print(f"  Emails skipped : {counts['emails_skipped']}")
    if DRY_RUN:
        print("  (DRY RUN — no actual emails sent)")
    print(f"{'─'*60}\n")

    db.close()


if __name__ == "__main__":
    run()
