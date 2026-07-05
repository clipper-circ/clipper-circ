import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv("/Users/joshcutler/clipper-circ/.env", override=True)
except ImportError:
    pass

import streamlit as st
import pandas as pd
import json
from datetime import date, datetime, timedelta
from sqlalchemy import or_

from database import SessionLocal
from models import (
    Subscriber, Payment, DeliveryHold, StaffUser, PaymentAuditLog, HoldAuditLog,
    SubscriberEventLog, AdminLoginLog, SubscriberStatus, PlanCode, PaymentMethod,
    PLAN_LABELS, PLAN_PRICES, ObituarySubmission, Setting, DiscountCode
)
import bcrypt
import hashlib
import extra_streamlit_components as stx
import streamlit.components.v1 as components

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

DEFAULT_SETTINGS = {
    "prices": {
        "LOCAL": 50.00,
        "SENIOR": 40.00,
        "OUT_OF_COUNTY": 90.00,
        "SNOWBIRD": 55.00,
        "GIFT": 50.00,
        "COMPLIMENTARY": 0.00,
    },
    "durations_weeks": {
        "LOCAL": 52,
        "SENIOR": 52,
        "OUT_OF_COUNTY": 52,
        "SNOWBIRD": 26,
        "GIFT": 52,
        "COMPLIMENTARY": 52,
    },
    "grace_period_days": 28,
    "reminder_days": [60, 30],
    "email_schedule": {
        "reminder_35_days": 35,
        "reminder_21_days": 21,
        "reminder_14_days": 14,
        "grace_14_days":    14,
        "grace_final_days": 27,
    },
    "email_templates": {
        "reminder_35": {
            "subject": "Your Duxbury Clipper subscription — time to renew soon",
            "body": "Your subscription expires on {expiration_date} — about 5 issues from now.\n\nTo keep your weekly Clipper coming, please renew at your convenience.\n\nOr mail a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331\n\nThank you for supporting your hometown paper!",
            "btn_color": "#2e7d32", "box_color": "#2e7d32",
        },
        "reminder_21": {
            "subject": "Reminder: Your Duxbury Clipper subscription expires soon",
            "body": "Just a reminder — your subscription expires on {expiration_date} (about 3 issues away).\n\nDon't miss a single issue. Renew now.\n\nOr send a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331",
            "btn_color": "#2e7d32", "box_color": "#2e7d32",
        },
        "reminder_14": {
            "subject": "2 issues left — please renew your Duxbury Clipper subscription",
            "body": "You have about 2 issues remaining on your Duxbury Clipper subscription (expires {expiration_date}).\n\nRenew today to avoid any interruption in delivery.\n\nOr mail a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331",
            "btn_color": "#2e7d32", "box_color": "#2e7d32",
        },
        "expire_day": {
            "subject": "Your Duxbury Clipper subscription expires today",
            "body": "Your Duxbury Clipper subscription expires today.\n\nGood news — we'll keep delivering your paper for up to 4 more weeks while you renew. Please don't let it lapse!\n\nOr mail a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331",
            "btn_color": "#e65100", "box_color": "#e65100",
        },
        "grace_14": {
            "subject": "Action needed: Your Duxbury Clipper subscription is past due",
            "body": "Your Duxbury Clipper subscription expired on {expiration_date}.\n\nWe've continued delivering your paper as a courtesy, but delivery will stop in about 2 weeks if we don't hear from you.\n\nOr mail a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331",
            "btn_color": "#c62828", "box_color": "#c62828",
        },
        "grace_final": {
            "subject": "Final notice: Duxbury Clipper delivery stopping this week",
            "body": "We're sorry to say that your Duxbury Clipper home delivery will stop this week unless you renew.\n\nYour subscription expired on {expiration_date} and we haven't received a renewal. We'd love to keep you on our list!\n\nOr call us at 781-934-2811 or mail a check to: The Duxbury Clipper, P.O. Box 1656, Duxbury, MA 02331\n\nWe hope to hear from you — thank you for being a loyal reader.",
            "btn_color": "#c62828", "box_color": "#c62828",
        },
    },
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        # merge so new keys get defaults
        for k, v in DEFAULT_SETTINGS.items():
            if k not in data:
                data[k] = v
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    if kk not in data[k]:
                        data[k][kk] = vv
        return data
    return dict(DEFAULT_SETTINGS)

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

def write_audit(db, action, payment, sub, entered_by):
    db.add(PaymentAuditLog(
        action=action,
        payment_id=payment.id,
        subscriber_id=sub.id,
        subscriber_name=sub.full_name,
        amount=payment.amount,
        payment_method=str(payment.payment_method),
        check_number=payment.check_number,
        period_start=payment.period_start,
        period_end=payment.period_end,
        notes=payment.notes,
        entered_by=entered_by,
    ))

STATUS_COLORS = {
    "ACTIVE": "#3a6b1a",
    "GRACE": "#d4870a",
    "ON_HOLD": "#2980b9",
    "EXPIRED": "#c0392b",
    "CANCELLED": "#888888",
}

def status_badge(status_val):
    color = STATUS_COLORS.get(status_val, "#888")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:10px;font-size:0.8em;font-weight:600;">{status_val}</span>'

st.set_page_config(page_title="Clipper Circulation Admin", layout="wide", page_icon="🗞️")

st.markdown("""
<style>
/* ── Greens: dark forest ── */
:root {
  --forest:      #1a3a1a;
  --forest-mid:  #245c24;
  --forest-lite: #2e7d32;
  --forest-pale: #e8f5e9;
  --border:      #a5d6a7;
}

/* Top bar */
[data-testid="stHeader"] { background-color: var(--forest); }
[data-testid="stHeader"] button, [data-testid="stHeader"] svg { color: white !important; fill: white !important; }
[data-testid="stSidebarCollapsedControl"] button, [data-testid="stSidebarCollapsedControl"] svg { color: white !important; fill: white !important; }

/* Sidebar background */
[data-testid="stSidebar"] { background-color: var(--forest) !important; }
[data-testid="stSidebar"] * { color: #ffffff !important; }
[data-testid="stSidebar"] hr { border-color: var(--forest-mid) !important; }

/* Collapse Streamlit's default element spacing in sidebar */
[data-testid="stSidebar"] .stButton {
    margin-bottom: -16px !important;
}
/* Logo border */
[data-testid="stSidebar"] img {
    border: 2px solid white !important;
    border-radius: 8px !important;
    padding: 4px !important;
    background: white !important;
}

/* Sidebar nav buttons */
[data-testid="stSidebar"] .stButton > button {
    background: transparent !important;
    color: #c8e6c9 !important;
    border: none !important;
    border-radius: 6px !important;
    font-size: 1em !important;
    font-weight: 500 !important;
    text-align: left !important;
    padding: 5px 14px !important;
    margin-bottom: 0px !important;
    width: 100% !important;
}
[data-testid="stSidebar"] .stButton > button:hover {
    background: rgba(255,255,255,0.12) !important;
    color: #ffffff !important;
}
[data-testid="stSidebar"] .nav-inactive .stButton > button {
    border-left: 3px solid transparent !important;
}
[data-testid="stSidebar"] .nav-active .stButton > button {
    background: rgba(255,255,255,0.18) !important;
    color: #ffffff !important;
    font-weight: 700 !important;
    border-left: 3px solid #81c784 !important;
}

/* Prevent Streamlit column negative-margin overflow inside styled containers */
[data-testid="stVerticalBlock"] [data-testid="stHorizontalBlock"] {
    overflow: visible;
}

/* Collapse zero-height component iframes (used for JS injection) */
iframe[height="0"] {
    display: block !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    overflow: hidden !important;
}


/* Main buttons */
.stButton > button {
    background-color: var(--forest-lite);
    color: white;
    border: none;
    border-radius: 5px;
    font-weight: 600;
}
.stButton > button:hover {
    background-color: var(--forest-mid);
    color: white;
}

/* Add New Subscriber button — light blue */
.add-sub-btn > button {
    background-color: #1565c0 !important;
    color: white !important;
    border: none !important;
}
.add-sub-btn > button:hover {
    background-color: #0d47a1 !important;
    color: white !important;
}

/* Save New Subscriber button inside add form — light blue */
.save-new-btn > button {
    background-color: #1976d2 !important;
    color: white !important;
    border: none !important;
}
.save-new-btn > button:hover {
    background-color: #1565c0 !important;
    color: white !important;
}

/* Metric cards */
[data-testid="metric-container"] {
    background-color: var(--forest-pale);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    border-left: 4px solid var(--forest-lite);
}

/* Page titles */
h1 { color: var(--forest) !important; border-bottom: 2px solid var(--forest-lite); padding-bottom: 8px; }
h2 { color: var(--forest-lite) !important; }
h3 { color: var(--forest-lite) !important; }

/* Divider */
hr { border-color: var(--border) !important; }

/* Dataframe: suppress edit cursor so table feels read-only */
[data-testid="stDataFrame"] canvas { cursor: default !important; }
[data-testid="stDataFrame"] [class*="cell"] { cursor: default !important; }
</style>
""", unsafe_allow_html=True)


# ── Auth ──────────────────────────────────────────────────────────────────────

cookie_manager = stx.CookieManager(key="clipper_cookies")

COOKIE_NAME = "clipper_auth"
COOKIE_DAYS = 30

def _token_for(user):
    """Deterministic token tied to user id + password hash — invalidates on password change."""
    raw = f"{user.id}:{user.password_hash}:{os.environ.get('SECRET_KEY','')}"
    return hashlib.sha256(raw.encode()).hexdigest()

SESSION_TIMEOUT_HOURS = 3
LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 30

def _get_browser():
    try:
        ua = st.context.headers.get("User-Agent", "")
        if "iPhone" in ua or "iPad" in ua:
            device = "iOS"
        elif "Android" in ua:
            device = "Android"
        elif "Mac" in ua:
            device = "Mac"
        elif "Windows" in ua:
            device = "Windows"
        else:
            device = "Unknown"
        if "Chrome" in ua and "Edg" not in ua and "OPR" not in ua:
            browser = "Chrome"
        elif "Safari" in ua and "Chrome" not in ua:
            browser = "Safari"
        elif "Firefox" in ua:
            browser = "Firefox"
        elif "Edg" in ua:
            browser = "Edge"
        else:
            browser = "Other"
        return f"{browser} / {device}"
    except Exception:
        return None

def is_ip_locked(db, ip):
    if not ip:
        return False
    cutoff = datetime.utcnow() - timedelta(minutes=LOCKOUT_MINUTES)
    recent_failures = db.query(AdminLoginLog).filter(
        AdminLoginLog.ip_address == ip,
        AdminLoginLog.success == False,
        AdminLoginLog.event_at >= cutoff,
    ).count()
    return recent_failures >= LOCKOUT_ATTEMPTS

def check_login(email, password, pin, ip=None, browser=None):
    db = SessionLocal()
    if is_ip_locked(db, ip):
        db.add(AdminLoginLog(email=email, success=False, reason="ip_locked", ip_address=ip, browser=browser))
        db.commit()
        db.close()
        return "locked"
    if pin != ADMIN_PIN:
        db.add(AdminLoginLog(email=email, success=False, reason="bad_pin", ip_address=ip, browser=browser))
        db.commit()
        db.close()
        return None
    user = db.query(StaffUser).filter_by(email=email.lower(), is_active=True).first()
    if not user:
        db.add(AdminLoginLog(email=email, success=False, reason="not_found", ip_address=ip, browser=browser))
        db.commit()
        db.close()
        return None
    if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
        db.add(AdminLoginLog(email=email, success=False, reason="bad_password", ip_address=ip, browser=browser))
        db.commit()
        db.close()
        return None
    log = AdminLoginLog(email=email, success=True, reason="ok", ip_address=ip, browser=browser)
    db.add(log)
    db.commit()
    user_data = {"id": user.id, "name": user.name, "is_admin": user.is_admin, "log_id": log.id}
    db.close()
    return user_data

def user_from_cookie(token):
    db = SessionLocal()
    for user in db.query(StaffUser).filter_by(is_active=True).all():
        if _token_for(user) == token:
            db.close()
            return user
    db.close()
    return None

ADMIN_PIN = os.environ.get("ADMIN_PIN", "1656")

def login_screen():
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        _logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
        st.image(_logo, width=400)
        st.markdown("### Circulation Admin")
        with st.form("login"):
            email = st.text_input("Username")
            pc1, pc2 = st.columns([3, 1])
            password = pc1.text_input("Password", type="password")
            pin = pc2.text_input("PIN", type="password")
            remember = st.checkbox("Remember me for 30 days", value=True)
            if st.form_submit_button("Log In", use_container_width=True):
                result = check_login(email, password, pin, browser=_get_browser())
                if result == "locked":
                    st.error(f"Too many failed attempts. This IP is blocked for {LOCKOUT_MINUTES} minutes.")
                elif result:
                    st.session_state.user = result
                    st.session_state["login_time"] = datetime.now().isoformat()
                    st.session_state["login_log_id"] = result.get("log_id")
                    if remember:
                        db2 = SessionLocal()
                        u = db2.query(StaffUser).filter_by(id=result["id"]).first()
                        cookie_manager.set(
                            COOKIE_NAME,
                            _token_for(u),
                            expires_at=datetime.now() + timedelta(days=COOKIE_DAYS),
                        )
                        db2.close()
                    st.rerun()
                else:
                    st.error("Invalid username, password, or PIN.")


# ── Check cookie before showing login ─────────────────────────────────────────
if "user" not in st.session_state:
    token = cookie_manager.get(COOKIE_NAME)
    if token:
        remembered_user = user_from_cookie(token)
        if remembered_user:
            st.session_state.user = {
                "id": remembered_user.id,
                "name": remembered_user.name,
                "is_admin": remembered_user.is_admin,
            }

if "user" not in st.session_state:
    login_screen()
    st.stop()

# ── Session timeout (3 hours) ─────────────────────────────────────────────────
if "login_time" in st.session_state:
    login_dt = datetime.fromisoformat(st.session_state["login_time"])
    if datetime.now() - login_dt > timedelta(hours=SESSION_TIMEOUT_HOURS):
        del st.session_state["user"]
        st.session_state.pop("login_time", None)
        try:
            cookie_manager.delete(COOKIE_NAME)
        except Exception:
            pass
        st.warning("Your session has expired. Please log in again.")
        st.stop()

# ── Sidebar nav ───────────────────────────────────────────────────────────────

_logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
if os.path.exists(_logo_path):
    st.sidebar.image(_logo_path, width=160)
st.sidebar.title("Clipper Circulation")
st.sidebar.write(f"Logged in as **{st.session_state.user['name']}**")
st.sidebar.divider()

nav_options = [
    "📋 Dashboard",
    "🔍 Subscribers",
    "🔔 Renewals",
    "📦 Delivery List",
    "🔁 Duplicates",
    "📰 Obituaries",
    "⚙️ Settings",
]

# Allow programmatic navigation
_nav_to = st.session_state.pop("_nav_to", None)
if _nav_to and _nav_to in nav_options:
    st.session_state["_current_page"] = _nav_to

if "_current_page" not in st.session_state:
    st.session_state["_current_page"] = nav_options[0]

for _nav_item in nav_options:
    _is_active = st.session_state["_current_page"] == _nav_item
    _cls = "nav-active" if _is_active else "nav-inactive"
    st.sidebar.markdown(f'<div class="{_cls}">', unsafe_allow_html=True)
    if st.sidebar.button(_nav_item, key=f"nav_{_nav_item}", use_container_width=True):
        st.session_state["_current_page"] = _nav_item
        st.rerun()
    st.sidebar.markdown('</div>', unsafe_allow_html=True)

page = st.session_state["_current_page"]

st.sidebar.divider()
if st.sidebar.button("🚪 Log Out", use_container_width=True):
    try:
        cookie_manager.delete(COOKIE_NAME)
    except Exception:
        pass
    log_id = st.session_state.get("login_log_id")
    if log_id:
        _db = SessionLocal()
        _log = _db.query(AdminLoginLog).filter_by(id=log_id).first()
        if _log:
            _log.logout_at = datetime.utcnow()
            _db.commit()
        _db.close()
    del st.session_state.user
    st.session_state.pop("login_log_id", None)
    st.rerun()

db = SessionLocal()

# Preserve scroll position across reruns (tabs, selections, etc.)
components.html("""
<script>
(function() {
    var key = 'clipper_scroll';
    var p = window.parent;
    function getMain() {
        return p.document.querySelector('.main');
    }
    var saved = parseInt(p.sessionStorage.getItem(key) || '0');
    if (saved > 0) {
        setTimeout(function() {
            var m = getMain();
            if (m) m.scrollTop = saved;
        }, 60);
    }
    setInterval(function() {
        var m = getMain();
        if (m && m.scrollTop > 0) p.sessionStorage.setItem(key, m.scrollTop);
    }, 300);
})();
</script>
""", height=0)

# ── Dashboard ─────────────────────────────────────────────────────────────────

if page == "📋 Dashboard":
    st.title("📋 Dashboard")

    today = date.today()
    in_30 = today + timedelta(days=30)
    in_60 = today + timedelta(days=60)
    this_month_start = today.replace(day=1)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

    from sqlalchemy import func

    active      = db.query(Subscriber).filter(Subscriber.status == SubscriberStatus.ACTIVE).count()
    grace       = db.query(Subscriber).filter(Subscriber.status == SubscriberStatus.GRACE).count()
    expired     = db.query(Subscriber).filter(Subscriber.status == SubscriberStatus.EXPIRED).count()
    cancelled   = db.query(Subscriber).filter(Subscriber.status == SubscriberStatus.CANCELLED).count()
    on_hold     = db.query(Subscriber).filter(Subscriber.status == SubscriberStatus.ON_HOLD).count()
    comp        = db.query(Subscriber).filter(Subscriber.plan == PlanCode.COMPLIMENTARY).count()
    exp_30      = db.query(Subscriber).filter(
                    Subscriber.status == SubscriberStatus.ACTIVE,
                    Subscriber.expiration_date <= in_30,
                    Subscriber.expiration_date >= today).count()
    exp_60      = db.query(Subscriber).filter(
                    Subscriber.status == SubscriberStatus.ACTIVE,
                    Subscriber.expiration_date <= in_60,
                    Subscriber.expiration_date > in_30).count()

    rev_month   = float(db.query(func.sum(Payment.amount)).filter(
                    Payment.paid_at >= this_month_start).scalar() or 0)
    rev_last    = float(db.query(func.sum(Payment.amount)).filter(
                    Payment.paid_at >= last_month_start,
                    Payment.paid_at < this_month_start).scalar() or 0)
    rev_ytd     = float(db.query(func.sum(Payment.amount)).filter(
                    Payment.paid_at >= today.replace(month=1, day=1)).scalar() or 0)
    new_this_month = db.query(Subscriber).filter(
                    Subscriber.created_at >= this_month_start).count()

    # ── Metric cards via custom HTML ──────────────────────────────────────────
    def card(icon, label, value, color, sub=None):
        sub_html = f'<div style="font-size:0.78em;color:#666;margin-top:2px;">{sub}</div>' if sub else ""
        return f"""
        <div style="background:#fff;border-radius:10px;padding:18px 16px;border-left:5px solid {color};
                    box-shadow:0 1px 4px rgba(0,0,0,0.08);height:100%;">
          <div style="font-size:1.9em;font-weight:700;color:{color};line-height:1.1;">{value}</div>
          <div style="font-size:0.85em;font-weight:600;color:#444;margin-top:4px;">{icon} {label}</div>
          {sub_html}
        </div>"""

    st.markdown("#### Circulation")
    c1,c2,c3,c4,c5 = st.columns(5)
    c1.markdown(card("✅","Active",f"{active:,}","#3a6b1a",f"+{new_this_month} new this month"), unsafe_allow_html=True)
    c2.markdown(card("🎁","Complimentary",comp,"#6c3483"), unsafe_allow_html=True)
    c3.markdown(card("⏳","Grace Period",grace,"#d4870a","Still receiving paper"), unsafe_allow_html=True)
    c4.markdown(card("🔴","Expired",expired,"#c0392b","Not renewed"), unsafe_allow_html=True)
    c5.markdown(card("⏸️","On Hold",on_hold,"#2980b9","Delivery paused"), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
    st.markdown("#### Renewals Needed")
    r1,r2 = st.columns(2)
    r1.markdown(card("⚠️","Expiring in 30 Days",exp_30,"#e67e22","Action recommended"), unsafe_allow_html=True)
    r2.markdown(card("📅","Expiring in 31–60 Days",exp_60,"#f0b429","Send reminder soon"), unsafe_allow_html=True)

    st.markdown("<div style='margin-top:18px'></div>", unsafe_allow_html=True)
    st.markdown("#### Revenue")
    rev_delta = rev_month - rev_last
    rev_delta_str = f"{'▲' if rev_delta >= 0 else '▼'} ${abs(rev_delta):,.0f} vs last month"
    v1,v2,v3 = st.columns(3)
    v1.markdown(card("💵","This Month",f"${rev_month:,.2f}","#3a6b1a", rev_delta_str), unsafe_allow_html=True)
    v2.markdown(card("📆","Last Month",f"${rev_last:,.2f}","#555"), unsafe_allow_html=True)
    v3.markdown(card("📈","Year to Date",f"${rev_ytd:,.2f}","#2980b9"), unsafe_allow_html=True)

    st.divider()

    # ── Status breakdown bar ──────────────────────────────────────────────────
    st.subheader("📊 Subscribers by Plan & Status")
    plan_rows = []
    for plan in PlanCode:
        a = db.query(Subscriber).filter(Subscriber.plan == plan, Subscriber.status == SubscriberStatus.ACTIVE).count()
        g = db.query(Subscriber).filter(Subscriber.plan == plan, Subscriber.status == SubscriberStatus.GRACE).count()
        h = db.query(Subscriber).filter(Subscriber.plan == plan, Subscriber.status == SubscriberStatus.ON_HOLD).count()
        e = db.query(Subscriber).filter(Subscriber.plan == plan, Subscriber.status == SubscriberStatus.EXPIRED).count()
        x = db.query(Subscriber).filter(Subscriber.plan == plan, Subscriber.status == SubscriberStatus.CANCELLED).count()
        total = a + g + h + e + x
        if total:
            plan_rows.append({"Plan": PLAN_LABELS[plan], "Active": a, "Grace": g, "On Hold": h, "Expired": e, "Cancelled": x, "Total": total})
    if plan_rows:
        df_plans = pd.DataFrame(plan_rows)
        def _ca(v): return "background-color:#d4edda;color:#155724;font-weight:600;" if v > 0 else ""
        def _cg(v): return "background-color:#fff3cd;color:#856404;font-weight:600;" if v > 0 else ""
        def _ce(v): return "background-color:#f8d7da;color:#721c24;font-weight:600;" if v > 0 else ""
        st.dataframe(
            df_plans.style.applymap(_ca, subset=["Active"]).applymap(_cg, subset=["Grace"]).applymap(_ce, subset=["Expired"]),
            use_container_width=True, hide_index=True
        )

    st.divider()
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("⚠️ Expiring in Next 30 Days")
        expiring_subs = db.query(Subscriber).filter(
            Subscriber.status == SubscriberStatus.ACTIVE,
            Subscriber.expiration_date <= in_30,
            Subscriber.expiration_date >= today,
        ).order_by(Subscriber.expiration_date).all()
        if expiring_subs:
            rows = [{
                "Name": s.full_name,
                "Plan": PLAN_LABELS[s.plan],
                "Expires": s.expiration_date,
                "Email": "✓" if s.email else "✗",
                "Auto-Renew": "✓" if s.auto_renew else "✗",
            } for s in expiring_subs]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=280)
        else:
            st.success("No subscribers expiring in the next 30 days.")

    with col_right:
        st.subheader("🔴 In Grace Period")
        grace_subs = db.query(Subscriber).filter(
            Subscriber.status == SubscriberStatus.GRACE,
        ).order_by(Subscriber.expiration_date).all()
        if grace_subs:
            rows = [{
                "Name": s.full_name,
                "Expired": s.expiration_date,
                "Email": "✓" if s.email else "✗",
                "Payment": s.payment_method.value,
            } for s in grace_subs]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=280)
        else:
            st.success("No subscribers currently in grace period.")


# ── Subscribers ───────────────────────────────────────────────────────────────

elif page == "🔍 Subscribers":
    st.title("Subscribers")

    # Reset counter: incrementing it changes widget keys, forcing fresh defaults
    if "filter_reset" not in st.session_state:
        st.session_state["filter_reset"] = 0
    rk = st.session_state["filter_reset"]

    # ── Search filters ────────────────────────────────────────────────────────
    status_opts = ["All", "Active", "Grace", "On Hold", "Expired", "Cancelled"]
    plan_opts = ["All"] + [PLAN_LABELS[p] for p in PlanCode]

    if "show_add_form" not in st.session_state:
        st.session_state["show_add_form"] = False

    col1, col2, col3, col4, col5 = st.columns([2, 1, 1, 1.1, 1.2])
    search = col1.text_input("Search", placeholder="Name, email, address, ID…", key=f"sub_search_{rk}")
    status_filter = col2.selectbox("Status", status_opts, key=f"sub_status_{rk}")
    plan_filter = col3.selectbox("Plan", plan_opts, key=f"sub_plan_{rk}")
    col4.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
    btn_col1, btn_col2 = col4.columns(2)
    btn_col1.button("Find", use_container_width=True)
    if btn_col2.button("Clear", use_container_width=True):
        st.session_state["filter_reset"] = rk + 1
        st.session_state.pop("sub_select", None)
        st.session_state.pop("selected_sub_id", None)
        st.rerun()
    col5.markdown("""<style>
    [data-testid="column"]:nth-of-type(5) [data-testid="stButton"] > button {
        background:#e65100;color:white;border:1px solid #e65100;
    }
    [data-testid="column"]:nth-of-type(5) [data-testid="stButton"] > button:hover {
        background:#bf360c;border-color:#bf360c;
    }
    </style><div style='margin-top:28px'></div>""", unsafe_allow_html=True)
    if col5.button("➕ Add New", use_container_width=True):
        st.session_state["show_add_form"] = True
        st.session_state.pop("selected_sub_id", None)
        st.rerun()

    query = db.query(Subscriber)

    if search:
        term = f"%{search}%"
        query = query.filter(or_(
            Subscriber.full_name.ilike(term),
            Subscriber.email.ilike(term),
            Subscriber.address1.ilike(term),
            Subscriber.city.ilike(term),
            Subscriber.simplecirc_id.ilike(term),
        ))

    status_map = {
        "Active":    SubscriberStatus.ACTIVE,
        "Grace":     SubscriberStatus.GRACE,
        "On Hold":   SubscriberStatus.ON_HOLD,
        "Expired":   SubscriberStatus.EXPIRED,
        "Cancelled": SubscriberStatus.CANCELLED,
    }
    if status_filter != "All":
        query = query.filter(Subscriber.status == status_map[status_filter])

    if plan_filter != "All":
        plan_code = next(k for k, v in PLAN_LABELS.items() if v == plan_filter)
        query = query.filter(Subscriber.plan == plan_code)

    subs = query.order_by(Subscriber.full_name).all()

    leg_l, leg_r = st.columns([1, 2])
    leg_l.caption(f"{len(subs):,} subscribers found — click a row to open")
    leg_r.markdown("""
    <div style="font-size:0.78em;display:flex;gap:10px;justify-content:flex-end;padding-top:4px;">
      <span style="background:#fde8e8;padding:2px 8px;border-radius:4px;">Expired</span>
      <span style="background:#fff8e1;padding:2px 8px;border-radius:4px;">Grace period</span>
      <span style="background:#fff3cd;padding:2px 8px;border-radius:4px;">Expiring ≤30d</span>
      <span style="background:#fffde7;padding:2px 8px;border-radius:4px;">Expiring ≤60d</span>
    </div>""", unsafe_allow_html=True)

    if subs:
        rows = [{
            "ID": s.id,
            "SimpleCirc ID": s.simplecirc_id or "—",
            "Name": s.full_name,
            "Address": s.address1,
            "City": s.city,
            "Plan": PLAN_LABELS[s.plan],
            "Status": s.status.value,
            "Expires": s.expiration_date,
            "Auto-Renew": "✓" if s.auto_renew else "✗",
        } for s in subs]
        df = pd.DataFrame(rows)

        STATUS_BG = {
            "ACTIVE": "#d4edda", "GRACE": "#fff3cd", "ON_HOLD": "#cce5ff",
            "EXPIRED": "#f8d7da", "CANCELLED": "#e2e3e5",
        }
        STATUS_FG = {
            "ACTIVE": "#155724", "GRACE": "#856404", "ON_HOLD": "#004085",
            "EXPIRED": "#721c24", "CANCELLED": "#383d41",
        }

        def style_status(val):
            bg = STATUS_BG.get(val, "#eee")
            fg = STATUS_FG.get(val, "#333")
            return f"background-color:{bg};color:{fg};font-weight:600;border-radius:4px;text-align:center;"

        def highlight_row(row):
            status = row["Status"]
            exp = row["Expires"]
            base = [""] * len(row)
            if status == "EXPIRED":
                return [f"background-color:#fde8e8"] * len(row)
            if status == "GRACE":
                return [f"background-color:#fff8e1"] * len(row)
            if status == "CANCELLED":
                return ["color:#aaa;font-style:italic"] * len(row)
            if exp and exp != "—":
                try:
                    days_left = (pd.to_datetime(exp).date() - date.today()).days
                    if days_left <= 30:
                        return ["background-color:#fff3cd"] * len(row)
                    if days_left <= 60:
                        return ["background-color:#fffde7"] * len(row)
                except Exception:
                    pass
            return base

        try:
            styled = (df.style
                .apply(highlight_row, axis=1)
                .applymap(style_status, subset=["Status"]))
            selection = st.dataframe(
                styled, use_container_width=True, hide_index=True,
                height=245, on_select="rerun", selection_mode="single-row", key="sub_table"
            )
        except Exception:
            selection = st.dataframe(
                df, use_container_width=True, hide_index=True,
                height=245, on_select="rerun", selection_mode="single-row", key="sub_table"
            )

        # Resolve selected row → subscriber ID
        selected_rows = selection.selection.rows if hasattr(selection, "selection") else []
        if selected_rows:
            row_idx = selected_rows[0]
            if row_idx < len(df):
                selected_id = int(df.iloc[row_idx]["ID"])
                st.session_state["selected_sub_id"] = selected_id
            else:
                st.session_state.pop("selected_sub_id", None)
        selected_id = st.session_state.get("selected_sub_id")
        # Discard stored selection if that subscriber isn't in the current result set
        if selected_id and selected_id not in df["ID"].values:
            st.session_state.pop("selected_sub_id", None)
            selected_id = None

    # ── Add New Subscriber (inline) ────────────────────────────────────────────
    if st.session_state["show_add_form"]:
      with st.container():
        # JS: add yellow box to this container (no padding — columns below handle margins)
        components.html("""
<script>
setTimeout(function() {
    try {
        var el = window.frameElement;
        if (!el) return;
        var block = el.parentElement;
        while (block && block.getAttribute('data-testid') !== 'stVerticalBlock') {
            block = block.parentElement;
        }
        if (block) {
            block.style.background = '#fffde7';
            block.style.border = '1px solid #f0c040';
            block.style.borderRadius = '10px';
            block.style.marginBottom = '12px';
        }
    } catch(e) {}
    // Red cancel button
    try {
        var pdoc = window.parent.document;
        pdoc.querySelectorAll('button').forEach(function(btn) {
            if (btn.innerText.trim().includes('Cancel')) {
                btn.style.backgroundColor = '#c62828';
                btn.style.color = 'white';
                btn.style.border = 'none';
            }
        });
    } catch(e) {}
}, 200);
</script>
""", height=0)
        # Use columns to create side margins (reliable vs CSS padding tricks)
        _gap, _form, _gap2 = st.columns([0.04, 0.92, 0.04])
        with _form:
          st.subheader("➕ Add New Subscriber")
          st.caption("Search above first to confirm this person doesn't already exist.")

          def _zip_lookup():
              z = st.session_state.get("add_zip", "").strip()
              if len(z) == 5 and z.isdigit():
                  try:
                      import requests as _req
                      r = _req.get(f"https://api.zippopotam.us/us/{z}", timeout=3)
                      if r.status_code == 200:
                          data = r.json()
                          place = data["places"][0]
                          st.session_state["add_city_auto"] = place["place name"].title()
                          st.session_state["add_state_auto"] = place["state abbreviation"]
                  except Exception:
                      pass

          ac1, ac2 = _form.columns([3, 1])
          new_full_name = ac1.text_input("Full Name *", key="add_full_name")
          new_phone = ac2.text_input("Phone", key="add_phone")

          ae1, ae2 = _form.columns(2)
          new_email = ae1.text_input("Email", key="add_email")
          new_backup_email = ae2.text_input("Backup Email", key="add_backup_email")

          new_address1 = _form.text_input("Mailing Address *", key="add_address1")
          new_address2 = _form.text_input("Mailing Address 2 (optional)", key="add_address2")

          # Zip first — triggers city/state auto-fill
          az1, az2, az3 = _form.columns([2, 3, 1])
          new_zipcode = az1.text_input("Zip *", key="add_zip", on_change=_zip_lookup)
          new_city = az2.text_input("City *", key="add_city",
              value=st.session_state.get("add_city_auto", ""))
          new_state = az3.text_input("ST *", key="add_state",
              value=st.session_state.get("add_state_auto", "MA"))

          plan_options = list(PLAN_LABELS.values())
          ad1, ad2, ad3 = _form.columns([2, 1.5, 1.5])
          new_plan_label = ad1.selectbox("Subscription Plan *", plan_options, key="add_plan")
          new_plan_code = next(k for k, v in PLAN_LABELS.items() if v == new_plan_label)
          new_start = ad2.date_input("Start Date", value=date.today(), key="add_start")
          new_expiry = ad3.date_input("Expiration", value=date.today().replace(year=date.today().year + 1), key="add_expiry")

          new_notes = _form.text_area("Notes (optional)", height=80, key="add_notes")

          _form.markdown("**Initial Payment**")
          pp1, pp2, pp3, pp4, pp5 = _form.columns([1.5, 2, 1, 2, 1])
          new_pay_amount = pp1.text_input("Amount Paid ($)", key="add_pay_amount")
          pay_method_opts = [p.value for p in PaymentMethod]
          new_pay_method = pp2.selectbox("Method", pay_method_opts, key="add_pay_method")
          new_check_num = pp3.text_input("Check #", key="add_check_num",
              disabled=(st.session_state.get("add_pay_method", "") != PaymentMethod.CHECK.value))
          new_pay_notes = pp4.text_input("Payment Notes", key="add_pay_notes")
          new_auto_renew = pp5.checkbox("Auto-Renew", value=True, key="add_auto_renew")
          new_is_gift = False

          fa, fb = _form.columns([4, 1])
          with fa:
              st.markdown('<div class="save-new-btn">', unsafe_allow_html=True)
              save_new = st.button("✅ Save New Subscriber", use_container_width=True, key="save_new_btn")
              st.markdown('</div>', unsafe_allow_html=True)
          with fb:
              st.markdown('<div class="cancel-btn">', unsafe_allow_html=True)
              cancel_new = st.button("✗ Cancel", use_container_width=True, key="cancel_new_btn")
              st.markdown('</div>', unsafe_allow_html=True)

        if cancel_new:
            for k in list(st.session_state.keys()):
                if k.startswith("add_"):
                    st.session_state.pop(k, None)
            st.session_state.pop("selected_sub_id", None)
            st.session_state["show_add_form"] = False
            st.rerun()

        if save_new:
            new_city_val = st.session_state.get("add_city", st.session_state.get("add_city_auto", ""))
            new_state_val = st.session_state.get("add_state", st.session_state.get("add_state_auto", "MA"))
            if not new_full_name or not new_address1 or not new_city_val or not new_state_val or not new_zipcode:
                st.error("Please fill in all required fields (*).")
            else:
                new_sub = Subscriber(
                    full_name=new_full_name,
                    email=new_email or None,
                    phone=new_phone or None,
                    address1=new_address1,
                    address2=new_address2 or None,
                    city=new_city_val,
                    state=new_state_val.strip()[:50],
                    zipcode=new_zipcode,
                    plan=new_plan_code,
                    status=SubscriberStatus.ACTIVE,
                    start_date=new_start,
                    expiration_date=new_expiry if new_plan_code != PlanCode.COMPLIMENTARY else None,
                    payment_method=PaymentMethod(new_pay_method),
                    auto_renew=new_auto_renew,
                    is_gift=False,
                    backup_email=new_backup_email or None,
                    notes=new_notes or None,
                )
                db.add(new_sub)
                db.flush()
                # Record initial payment if amount provided
                pay_amt_str = new_pay_amount.strip().lstrip("$").replace(",", "") if new_pay_amount else ""
                if pay_amt_str:
                    try:
                        pay_amt = float(pay_amt_str)
                        new_payment = Payment(
                            subscriber_id=new_sub.id,
                            amount=pay_amt,
                            payment_method=PaymentMethod(new_pay_method),
                            check_number=new_check_num or None,
                            notes=new_pay_notes or None,
                            entered_by=st.session_state.get("user_name", "Staff"),
                        )
                        db.add(new_payment)
                        db.flush()
                        write_audit(db, "CREATED", new_payment, new_sub,
                                    entered_by=st.session_state.get("user_name", "Staff"))
                    except ValueError:
                        pass
                db.commit()
                for k in list(st.session_state.keys()):
                    if k.startswith("add_"):
                        st.session_state.pop(k, None)
                st.session_state["show_add_form"] = False
                st.session_state["selected_sub_id"] = new_sub.id
                st.session_state["just_added_name"] = new_full_name
                st.session_state["just_added_paid"] = bool(pay_amt_str)
                st.rerun()

    # ── Subscriber record ─────────────────────────────────────────────────────
    if not st.session_state.get("show_add_form") and subs:
        selected_id = st.session_state.get("selected_sub_id")
        sub = db.query(Subscriber).filter_by(id=selected_id).first() if selected_id else None

        just_added_name = st.session_state.pop("just_added_name", None)
        just_added_paid = st.session_state.pop("just_added_paid", None)
        if just_added_name:
            if just_added_paid:
                st.success(f"✅ **{just_added_name}** added successfully — payment recorded.")
            else:
                plan_price_tmp = PLAN_PRICES.get(sub.plan, 0) if sub else 0
                st.warning(f"✅ **{just_added_name}** added. No payment recorded — **amount due: ${plan_price_tmp:,.2f}**.")

    if not st.session_state.get("show_add_form") and subs:
        if sub:
            # Amount due / credit only applies to native clipper-circ records
            total_paid = sum(float(p.amount) for p in sub.payments)
            plan_price = PLAN_PRICES.get(sub.plan, 0)
            is_imported = bool(sub.simplecirc_id)
            balance = (plan_price - total_paid) if (plan_price and not is_imported) else 0
            amount_due = balance if balance > 0 else 0
            credit_balance = abs(balance) if balance < 0 else 0

            STATUS_COLOR = STATUS_COLORS.get(sub.status.value, "#888")
            # Count Wednesdays (publication days) remaining before expiration
            if sub.expiration_date and sub.expiration_date >= date.today():
                delta = (sub.expiration_date - date.today()).days + 1
                issues_left = sum(
                    1 for i in range(delta)
                    if (date.today() + timedelta(days=i)).weekday() == 2
                )
            else:
                issues_left = 0
            # ── Subscriber header: two-column card layout ──────────────────────
            exp_str = sub.expiration_date.strftime('%m/%d/%Y') if sub.expiration_date else '—'
            ar_color = "#2e7d32" if sub.auto_renew else "#c62828"
            ar_label = "On" if sub.auto_renew else "Off"
            due_html = f'<div style="margin-top:10px;padding:8px 12px;background:#fff3cd;border:1px solid #ffc107;border-radius:4px;font-size:0.85em;font-weight:700;color:#856404;">💰 Amount Due: ${amount_due:,.2f}</div>' if amount_due > 0 else ''
            credit_html = f'<div style="margin-top:10px;padding:8px 12px;background:#e8f5e9;border:1px solid #81c784;border-radius:4px;font-size:0.85em;font-weight:700;color:#1b5e20;">💚 Credit on Account: ${credit_balance:,.2f}</div>' if credit_balance > 0 else ''
            notes_html = f'<div style="margin-top:10px;padding:8px 12px;background:#f5f5f5;border-left:3px solid #bbb;border-radius:4px;font-size:0.85em;color:#555;">📝 {sub.notes}</div>' if sub.notes else ''

            st.markdown("""
            <style>
            .badge-active  { background:#e8f5e9;color:#2e7d32;padding:3px 10px;border-radius:10px;font-size:0.78em;font-weight:700; }
            .badge-grace   { background:#fff8e1;color:#f57f17;padding:3px 10px;border-radius:10px;font-size:0.78em;font-weight:700; }
            .badge-expired { background:#fde8e8;color:#c62828;padding:3px 10px;border-radius:10px;font-size:0.78em;font-weight:700; }
            .badge-other   { background:#eeeeee;color:#555;padding:3px 10px;border-radius:10px;font-size:0.78em;font-weight:700; }
            /* Color-coded subscriber detail tabs */
            div[data-testid="stTabs"] button:nth-child(1) { border-bottom:3px solid #2e7d32 !important; }
            div[data-testid="stTabs"] button:nth-child(2) { border-bottom:3px solid #1565c0 !important; }
            div[data-testid="stTabs"] button:nth-child(3) { border-bottom:3px solid #6a1b9a !important; }
            div[data-testid="stTabs"] button:nth-child(4) { border-bottom:3px solid #e65100 !important; }
            div[data-testid="stTabs"] button:nth-child(5) { border-bottom:3px solid #ad1457 !important; }
            div[data-testid="stTabs"] button:nth-child(6) { border-bottom:3px solid #37474f !important; }
            div[data-testid="stTabs"] button:nth-child(1)[aria-selected="true"] { color:#2e7d32 !important; background:#e8f5e9 !important; }
            div[data-testid="stTabs"] button:nth-child(2)[aria-selected="true"] { color:#1565c0 !important; background:#e3f2fd !important; }
            div[data-testid="stTabs"] button:nth-child(3)[aria-selected="true"] { color:#6a1b9a !important; background:#f3e5f5 !important; }
            div[data-testid="stTabs"] button:nth-child(4)[aria-selected="true"] { color:#e65100 !important; background:#fff3e0 !important; }
            div[data-testid="stTabs"] button:nth-child(5)[aria-selected="true"] { color:#ad1457 !important; background:#fce4ec !important; }
            div[data-testid="stTabs"] button:nth-child(6)[aria-selected="true"] { color:#37474f !important; background:#eceff1 !important; }
            </style>
            """, unsafe_allow_html=True)

            badge_class = {"ACTIVE":"badge-active","GRACE":"badge-grace","EXPIRED":"badge-expired"}.get(sub.status.value,"badge-other")

            # Store a baseline snapshot when subscriber first loads (or changes)
            baseline_key = f"edit_baseline_{sub.id}"
            if baseline_key not in st.session_state:
                st.session_state[baseline_key] = {
                    "full_name": sub.full_name,
                    "email": sub.email or "",
                    "address1": sub.address1,
                    "address2": sub.address2 or "",
                    "city": sub.city,
                    "state": sub.state,
                    "zipcode": sub.zipcode,
                    "phone": sub.phone or "",
                    "plan": PLAN_LABELS[sub.plan],
                    "status": sub.status.value,
                    "expiration": sub.expiration_date or date.today(),
                    "auto_renew": sub.auto_renew,
                    "notes": sub.notes or "",
                    "backup_email": sub.backup_email or "",
                }
            bl = st.session_state[baseline_key]
            eid = sub.id

            # Dirty check from session_state (before widgets render)
            current = {
                "full_name":      st.session_state.get(f"e_name_{eid}",      bl["full_name"]),
                "email":          st.session_state.get(f"e_email_{eid}",     bl["email"]),
                "address1":       st.session_state.get(f"e_addr1_{eid}",     bl["address1"]),
                "address2":       st.session_state.get(f"e_addr2_{eid}",     bl["address2"]),
                "city":           st.session_state.get(f"e_city_{eid}",      bl["city"]),
                "state":          st.session_state.get(f"e_state_{eid}",     bl["state"]),
                "zipcode":        st.session_state.get(f"e_zip_{eid}",       bl["zipcode"]),
                "phone":          st.session_state.get(f"e_phone_{eid}",     bl["phone"]),
                "plan":           st.session_state.get(f"e_plan_{eid}",      bl["plan"]),
                "status":         st.session_state.get(f"e_status_{eid}",    bl["status"]),
                "expiration":     st.session_state.get(f"e_exp_{eid}",       bl["expiration"]),
                "auto_renew":     st.session_state.get(f"e_ar_{eid}",        bl["auto_renew"]),
                "notes":          st.session_state.get(f"e_notes_{eid}",     bl["notes"]),
                "backup_email":   st.session_state.get(f"e_backup_{eid}",   bl.get("backup_email", sub.backup_email or "")),
            }
            is_dirty = current != bl

            with st.container(border=True):
                # ── Panel header ───────────────────────────────────────────────
                hdr_l, hdr_r = st.columns([3, 1])
                with hdr_l:
                    acct_num = sub.simplecirc_id or sub.id
                    created_str = sub.created_at.strftime('%b %d, %Y') if sub.created_at else '—'
                    exp_hdr = sub.expiration_date.strftime('%m/%d/%Y') if sub.expiration_date else '—'
                    ar_hdr = '<span style="color:#2e7d32;font-weight:700;">On</span>' if sub.auto_renew else '<span style="color:#c62828;font-weight:700;">Off</span>'
                    addr_line = sub.address1
                    if sub.address2:
                        addr_line += f", {sub.address2}"
                    addr_line += f" &nbsp;·&nbsp; {sub.city}, {sub.state} {sub.zipcode}"
                    if sub.using_alt_address and sub.alt_address1:
                        alt_line = sub.alt_address1
                        if sub.alt_address2:
                            alt_line += f", {sub.alt_address2}"
                        alt_line += f" &nbsp;·&nbsp; {sub.alt_city}, {sub.alt_state} {sub.alt_zipcode}"
                        addr_line = f'<span style="color:#1565c0;">{alt_line} (alt)</span>'
                    sc_id_html = (f' &nbsp;·&nbsp; SimpleCirc# <strong>{sub.simplecirc_id}</strong>' if sub.simplecirc_id else '')
                    if amount_due > 0:
                        bal_html = f' &nbsp;·&nbsp; <span style="color:#c62828;font-weight:700;">Balance due: ${amount_due:,.2f}</span>'
                    elif credit_balance > 0:
                        bal_html = f' &nbsp;·&nbsp; <span style="color:#2e7d32;font-weight:700;">Credit: ${credit_balance:,.2f}</span>'
                    else:
                        bal_html = ''
                    st.markdown(
                        f'<h2 style="margin:0;font-size:1.4em;font-weight:800;line-height:1.2;">'
                        f'{sub.full_name} &nbsp;<span class="{badge_class}">{sub.status.value}</span></h2>'
                        f'<div style="font-size:0.8em;color:#666;margin-top:5px;line-height:1.9;">'
                        f'<span style="color:#888;">Acct #{acct_num} &nbsp;·&nbsp; Created {created_str} &nbsp;·&nbsp;</span>'
                        f'Expires <strong>{exp_hdr}</strong> &nbsp;·&nbsp; Auto-renew: {ar_hdr}{sc_id_html}{bal_html}<br>'
                        f'📬 {addr_line}'
                        f'</div>',
                        unsafe_allow_html=True
                    )
                with hdr_r:
                    sb1, sb2 = st.columns(2)
                    save_clicked   = sb1.button("💾 Save", use_container_width=True, type="primary", key=f"save_btn_{eid}", disabled=not is_dirty)
                    revert_clicked = sb2.button("↩️ Revert", use_container_width=True, key=f"revert_btn_{eid}", disabled=not is_dirty)

                # ── Handle save/revert ─────────────────────────────────────────
                if revert_clicked:
                    del st.session_state[baseline_key]
                    for k in [f"e_name_{eid}", f"e_email_{eid}", f"e_addr1_{eid}", f"e_addr2_{eid}",
                               f"e_city_{eid}", f"e_state_{eid}", f"e_zip_{eid}", f"e_phone_{eid}",
                               f"e_plan_{eid}", f"e_status_{eid}", f"e_exp_{eid}",
                               f"e_ar_{eid}", f"e_notes_{eid}", f"e_backup_{eid}"]:
                        st.session_state.pop(k, None)
                    st.rerun()

                if save_clicked:
                    plan_code_save = next(k for k, v in PLAN_LABELS.items() if v == current["plan"])
                    sub.full_name       = current["full_name"]
                    sub.email           = current["email"] or None
                    sub.address1        = current["address1"]
                    sub.address2        = current["address2"] or None
                    sub.city            = current["city"]
                    sub.state           = current["state"]
                    sub.zipcode         = current["zipcode"]
                    sub.phone           = current["phone"] or None
                    sub.plan            = plan_code_save
                    sub.status          = SubscriberStatus(current["status"])
                    sub.expiration_date = current["expiration"]
                    sub.auto_renew      = current["auto_renew"]
                    sub.notes           = current["notes"]
                    sub.backup_email    = current["backup_email"] or None
                    db.commit()
                    del st.session_state[baseline_key]
                    st.success("✅ Saved.")
                    st.rerun()

                # ── Tabs ───────────────────────────────────────────────────────
                plan_options    = list(PLAN_LABELS.values())
                status_options  = [s.value for s in SubscriberStatus]
                payment_options = [p.value for p in PaymentMethod]

                tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
                    "📋 Subscription", "📬 Address", "👤 Contact Info",
                    "💵 Payments", "✋ Delivery Hold", "🕓 History"
                ])

                # ── Tab 1: Subscription ────────────────────────────────────────
                with tab1:
                    ts1, ts2 = st.columns([1.4, 1])
                    with ts1:
                        st.markdown("#### Subscription Details")
                        er1, er2, er3 = st.columns(3)
                        er1.selectbox("Plan", plan_options,
                            index=plan_options.index(bl["plan"]), key=f"e_plan_{eid}")
                        er2.selectbox("Status", status_options,
                            index=status_options.index(bl["status"]), key=f"e_status_{eid}")
                        er3.date_input("Expiration Date", value=bl["expiration"], key=f"e_exp_{eid}")
                        # Last payment info
                        last_pmt = (db.query(Payment)
                            .filter_by(subscriber_id=sub.id)
                            .order_by(Payment.paid_at.desc()).first())
                        lp_amount = f"${last_pmt.amount:.2f}" if last_pmt else "—"
                        lp_date   = last_pmt.paid_at.strftime("%m/%d/%Y") if last_pmt else "—"
                        lp1, lp2, lp3 = st.columns(3)
                        lp1.text_input("Last Payment", value=lp_amount, disabled=True, key=f"e_lpa_{eid}")
                        lp2.text_input("Last Payment Date", value=lp_date, disabled=True, key=f"e_lpd_{eid}")
                        lp3.text_input("Amount Due", value=f"${amount_due:.2f}" if amount_due > 0 else "$0.00", disabled=True, key=f"e_due_{eid}")
                        st.checkbox("Auto-Renew", value=bl["auto_renew"], key=f"e_ar_{eid}")
                        st.text_area("Internal Notes", value=bl["notes"], key=f"e_notes_{eid}",
                            height=80, placeholder="Staff notes — not visible to subscriber")
                    with ts2:
                        info_html = (
                            f'<div style="background:#f9f9f9;border:1px solid #eee;border-radius:8px;'
                            f'padding:16px 18px;font-size:0.9em;line-height:2;color:#444;">'
                            f'<span class="{badge_class}">{sub.status.value}</span>'
                            f'&nbsp;<strong>{PLAN_LABELS[sub.plan]}</strong><br>'
                            f'Expires <strong>{exp_str}</strong><br>'
                            f'<strong>{issues_left}</strong> issue{"s" if issues_left != 1 else ""} remaining<br>'
                            f'Auto-renew: <strong style="color:{ar_color};">{ar_label}</strong>'
                            f'{due_html}{credit_html}</div>'
                        )
                        st.markdown(info_html, unsafe_allow_html=True)
                        if sub.notes:
                            st.markdown(
                                f'<div style="background:#f0f4ff;border-left:3px solid #90a4ae;border-radius:4px;padding:8px 12px;font-size:0.88em;color:#444;margin-top:8px;">📝 {sub.notes}</div>',
                                unsafe_allow_html=True)

                # ── Tab 2: Address ─────────────────────────────────────────────
                with tab2:
                    ac1, ac2 = st.columns(2)

                    # ── Left: Primary ──────────────────────────────────────────
                    with ac1:
                        active_primary = not sub.using_alt_address
                        p_border = "#2e7d32" if active_primary else "#ddd"
                        p_bg = "#e8f5e9" if active_primary else "#f9f9f9"
                        p_label = "✓ Active — Primary" if active_primary else "Primary Address"
                        p_color = "#2e7d32" if active_primary else "#999"
                        p_a2 = f"<br>{sub.address2}" if sub.address2 else ""
                        st.markdown(
                            f'<div style="padding:8px 12px;border-radius:6px;border:2px solid {p_border};background:{p_bg};margin-bottom:10px;">'
                            f'<div style="font-size:0.7em;font-weight:700;color:{p_color};text-transform:uppercase;letter-spacing:1px;">{p_label}</div>'
                            f'<div style="font-size:0.88em;line-height:1.6;color:#444;margin-top:2px;">{sub.address1}{p_a2}<br>{sub.city}, {sub.state} {sub.zipcode}</div>'
                            f'</div>', unsafe_allow_html=True)
                        st.text_input("Street", value=bl["address1"], key=f"e_addr1_{eid}", label_visibility="collapsed", placeholder="Street address")
                        st.text_input("Apt/Unit", value=bl["address2"], key=f"e_addr2_{eid}", label_visibility="collapsed", placeholder="Apt / Unit (optional)")
                        ea1, ea2, ea3 = st.columns([3, 1, 2])
                        ea1.text_input("City",  value=bl["city"],    key=f"e_city_{eid}",  label_visibility="collapsed", placeholder="City")
                        ea2.text_input("ST",    value=bl["state"],   key=f"e_state_{eid}", label_visibility="collapsed", placeholder="ST")
                        ea3.text_input("Zip",   value=bl["zipcode"], key=f"e_zip_{eid}",   label_visibility="collapsed", placeholder="Zip")
                        st.caption("Uses main Save button above.")

                    # ── Right: Alternate ───────────────────────────────────────
                    with ac2:
                        if sub.alt_address1:
                            active_alt = sub.using_alt_address
                            a_border = "#2e7d32" if active_alt else "#ddd"
                            a_bg = "#e8f5e9" if active_alt else "#f9f9f9"
                            a_label = "✓ Active — Alternate" if active_alt else "Alternate Address"
                            a_color = "#2e7d32" if active_alt else "#999"
                            a_a2 = f"<br>{sub.alt_address2}" if sub.alt_address2 else ""
                            st.markdown(
                                f'<div style="padding:8px 12px;border-radius:6px;border:2px solid {a_border};background:{a_bg};margin-bottom:10px;">'
                                f'<div style="font-size:0.7em;font-weight:700;color:{a_color};text-transform:uppercase;letter-spacing:1px;">{a_label}</div>'
                                f'<div style="font-size:0.88em;line-height:1.6;color:#444;margin-top:2px;">{sub.alt_address1}{a_a2}<br>{sub.alt_city}, {sub.alt_state} {sub.alt_zipcode}</div>'
                                f'</div>', unsafe_allow_html=True)
                        else:
                            st.markdown('<div style="padding:8px 12px;border-radius:6px;border:2px dashed #ddd;background:#fafafa;color:#bbb;font-size:0.85em;margin-bottom:10px;">No alternate address on file</div>', unsafe_allow_html=True)

                        with st.form(f"alt_addr_form_{eid}"):
                            alt_a1     = st.text_input("Street",   value=sub.alt_address1 or "", label_visibility="collapsed", placeholder="Street address")
                            alt_a2_val = st.text_input("Apt/Unit", value=sub.alt_address2 or "", label_visibility="collapsed", placeholder="Apt / Unit (optional)")
                            aa1, aa2, aa3 = st.columns([3, 1, 2])
                            alt_city  = aa1.text_input("City", value=sub.alt_city    or "", label_visibility="collapsed", placeholder="City")
                            alt_state = aa2.text_input("ST",   value=sub.alt_state   or "", label_visibility="collapsed", placeholder="ST", max_chars=2)
                            alt_zip   = aa3.text_input("Zip",  value=sub.alt_zipcode or "", label_visibility="collapsed", placeholder="Zip")
                            save_alt_col, switch_col = st.columns(2)
                            if save_alt_col.form_submit_button("💾 Save Alt", use_container_width=True):
                                sub.alt_address1 = alt_a1 or None
                                sub.alt_address2 = alt_a2_val or None
                                sub.alt_city     = alt_city  or None
                                sub.alt_state    = alt_state or None
                                sub.alt_zipcode  = alt_zip   or None
                                db.commit()
                                st.success("Alternate address saved.")
                                st.rerun()

                    if sub.alt_address1:
                        switch_label = ("↩ Use Primary" if sub.using_alt_address else "→ Use Alternate")
                        if st.button(switch_label, key=f"toggle_addr_{eid}"):
                            sub.using_alt_address = not sub.using_alt_address
                            db.add(SubscriberEventLog(
                                subscriber_id=sub.id,
                                event_type="ADDRESS_SWITCHED",
                                description=f"Staff switched to {'alternate' if sub.using_alt_address else 'primary'} address",
                                performed_by=st.session_state.user["name"],
                            ))
                            db.commit()
                            st.rerun()

                # ── Tab 3: Contact Info ────────────────────────────────────────
                with tab3:
                    ci1, ci2 = st.columns(2)
                    with ci1:
                        st.text_input("Full Name", value=bl["full_name"], key=f"e_name_{eid}")
                        st.text_input("Phone",     value=bl["phone"],     key=f"e_phone_{eid}")
                    with ci2:
                        st.text_input("Primary Email", value=bl["email"],            key=f"e_email_{eid}")
                        st.text_input("Backup Email",  value=bl.get("backup_email", sub.backup_email or ""), key=f"e_backup_{eid}",
                            placeholder="e.g. spouse@example.com",
                            help="Used if we can't reach the primary address")
                        if sub.pending_email:
                            st.caption(f"⏳ Email change pending: **{sub.pending_email}**")
                    st.caption("Click **Save** above to apply name / email / phone changes.")

                # ── Tab 4: Payments ────────────────────────────────────────────
                with tab4:
                    # Payment history
                    payments_list = (db.query(Payment)
                        .filter_by(subscriber_id=sub.id)
                        .order_by(Payment.paid_at.desc()).all())
                    if payments_list:
                        ph1, ph2, ph3, ph4 = st.columns([2, 2, 3, 2])
                        ph1.markdown("**Date**"); ph2.markdown("**Amount**")
                        ph3.markdown("**Method**"); ph4.markdown("**By**")
                        st.divider()
                        for p in payments_list:
                            pc1, pc2, pc3, pc4 = st.columns([2, 2, 3, 2])
                            pc1.write(p.paid_at.strftime("%b %d, %Y") if p.paid_at else "—")
                            pc2.write(f"**${p.amount:.2f}**")
                            pc3.write(p.payment_method.value.replace('_', ' ').title() +
                                (f" — Check #{p.check_number}" if p.check_number else ""))
                            pc4.caption(p.entered_by or "—")
                        st.divider()
                    else:
                        st.caption("No payments recorded.")

                    # Sub-tabs for payment entry
                    ptab1, ptab2 = st.tabs(["📋 Record Payment", "💳 Manual Charge Card"])

                    with ptab1:
                        if "pay_form_reset" not in st.session_state:
                            st.session_state["pay_form_reset"] = 0
                        prk = st.session_state["pay_form_reset"]
                        confirm_key = f"pay_confirm_{sub.id}"
                        _default_exp = sub.expiration_date.replace(year=sub.expiration_date.year + 1) if sub.expiration_date else date.today().replace(year=date.today().year + 1)

                        method_opts = ["Check", "Credit Card (manual)", "Cash", "Complimentary"]
                        with st.form(f"check_payment_{prk}"):
                            pc1, pc2, pc3 = st.columns(3)
                            check_amount = pc1.number_input("Amount ($)", value=float(PLAN_PRICES[sub.plan]), step=0.01)
                            pay_method   = pc2.selectbox("Payment Type", method_opts)
                            check_number = pc3.text_input("Check # (if check)")
                            pd1, pd2 = st.columns(2)
                            pay_new_exp  = pd1.date_input("New Expiration Date", value=_default_exp)
                            pay_notes    = pd2.text_input("Notes (optional)")
                            entered_by_name = st.session_state.user["name"]
                            st.caption(f"Recording as: **{entered_by_name}**")
                            if st.form_submit_button("Review Payment →", use_container_width=True):
                                st.session_state[confirm_key] = {
                                    "amount": check_amount, "method": pay_method,
                                    "check_number": check_number, "notes": pay_notes,
                                    "new_exp": pay_new_exp.isoformat(),
                                    "entered_by": entered_by_name,
                                }

                        if confirm_key in st.session_state:
                            pending = st.session_state[confirm_key]
                            st.warning(
                                f"⚠️ Confirm **${pending['amount']:.2f}** via **{pending['method']}**"
                                + (f" (Check #{pending['check_number']})" if pending['check_number'] else "")
                                + f" for **{sub.full_name}**? New expiry: **{pending['new_exp']}**"
                            )
                            conf1, conf2 = st.columns(2)
                            if conf1.button("✅ Yes, Record It", key=f"pay_yes_{sub.id}_{prk}", use_container_width=True):
                                method_map = {
                                    "Check": PaymentMethod.CHECK,
                                    "Credit Card (manual)": PaymentMethod.CREDIT_CARD,
                                    "Cash": PaymentMethod.CHECK,
                                    "Complimentary": PaymentMethod.COMPLIMENTARY,
                                }
                                new_exp = date.fromisoformat(pending["new_exp"])
                                pmt = Payment(
                                    subscriber_id=sub.id,
                                    amount=pending["amount"],
                                    payment_method=method_map[pending["method"]],
                                    check_number=pending["check_number"] or None,
                                    notes=pending["notes"] or None,
                                    entered_by=pending["entered_by"],
                                    paid_at=datetime.utcnow(),
                                    period_start=sub.expiration_date or date.today(),
                                    period_end=new_exp,
                                )
                                db.add(pmt)
                                db.flush()
                                write_audit(db, "CREATED", pmt, sub, pending["entered_by"])
                                sub.status = SubscriberStatus.ACTIVE
                                sub.expiration_date = new_exp
                                for _f in ["reminder_35_sent","reminder_21_sent","reminder_14_sent",
                                           "reminder_expire_sent","grace_14_sent","grace_final_sent"]:
                                    setattr(sub, _f, False)
                                db.commit()
                                del st.session_state[confirm_key]
                                st.session_state["pay_form_reset"] = prk + 1
                                st.success("✅ Payment recorded.")
                                st.rerun()
                            if conf2.button("✗ Cancel", key=f"pay_no_{sub.id}_{prk}", use_container_width=True):
                                del st.session_state[confirm_key]
                                st.rerun()

                    with ptab2:
                        # Check if a Stripe charge just completed
                        components.html("""
<script>
if (window.parent.sessionStorage.getItem('stripe_charge_success') === '1') {
    window.parent.sessionStorage.removeItem('stripe_charge_success');
    setTimeout(function() {
        var btns = window.parent.document.querySelectorAll('[data-testid="stSidebar"] button');
        btns.forEach(function(b) { if (b.innerText.includes('Subscribers')) b.click(); });
    }, 300);
}
</script>
""", height=0)
                        _stripe_pk  = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
                        _charge_tok = os.environ.get("ADMIN_CHARGE_TOKEN", "")
                        _portal_url = os.environ.get("PORTAL_URL", "https://portal-production-ddc4.up.railway.app")
                        _stripe_sk  = os.environ.get("STRIPE_SECRET_KEY", "")
                        if not _stripe_pk or not _charge_tok:
                            st.warning("Set STRIPE_PUBLISHABLE_KEY and ADMIN_CHARGE_TOKEN in Railway → Admin variables.")
                        else:
                            _test_mode   = _stripe_sk.startswith("sk_test_")
                            _default_amt = float(PLAN_PRICES[sub.plan])
                            _entered_by  = st.session_state.user["name"]
                            components.html(f"""
<!DOCTYPE html>
<html>
<head>
<script src="https://js.stripe.com/v3/"></script>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 14px; margin: 0; padding: 0; background: transparent; }}
  .row {{ display: flex; gap: 10px; margin-bottom: 10px; align-items: flex-end; }}
  .field {{ flex: 1; }}
  label {{ display: block; font-size: 12px; color: #555; margin-bottom: 3px; font-weight: 600; }}
  input {{ width: 100%; padding: 8px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; box-sizing: border-box; }}
  #card-element {{ border: 1px solid #ccc; border-radius: 4px; padding: 9px 10px; background: white; }}
  #charge-btn {{ width: 100%; padding: 10px; background: #2e7d32; color: white; border: none;
                 border-radius: 4px; font-size: 15px; font-weight: 700; cursor: pointer; margin-top: 8px; }}
  #charge-btn:disabled {{ background: #aaa; cursor: not-allowed; }}
  #msg {{ margin-top: 10px; padding: 10px; border-radius: 4px; display: none; font-weight: 600; }}
  .err {{ background: #fde8e8; color: #c62828; }}
  .ok  {{ background: #e8f5e9; color: #1b5e20; }}
</style>
</head>
<body>
{'<div style="background:#fff3cd;padding:6px 10px;border-radius:4px;font-size:12px;margin-bottom:8px;">🧪 Test mode — use card 4242 4242 4242 4242, any future date, any CVV</div>' if _test_mode else ''}
<div class="row">
  <div class="field" style="max-width:110px">
    <label>Amount ($)</label>
    <input type="number" id="amount" value="{_default_amt:.2f}" step="0.01" min="0.01">
  </div>
  <div class="field">
    <label>Notes (optional)</label>
    <input type="text" id="notes" placeholder="e.g. paper renewal form">
  </div>
</div>
<label>Card Details</label>
<div id="card-element"></div>
<div id="msg"></div>
<button id="charge-btn">💳 Charge Card</button>

<script>
const stripe = Stripe('{_stripe_pk}');
const elements = stripe.elements();
const card = elements.create('card', {{style: {{base: {{fontSize: '14px'}}}}}});
card.mount('#card-element');

document.getElementById('charge-btn').addEventListener('click', async function() {{
  const btn = this;
  const msg = document.getElementById('msg');
  btn.disabled = true;
  btn.textContent = 'Processing…';
  msg.style.display = 'none';

  const amount  = document.getElementById('amount').value;
  const notes   = document.getElementById('notes').value;

  const {{paymentMethod, error}} = await stripe.createPaymentMethod({{type: 'card', card: card}});
  if (error) {{
    msg.className = 'err'; msg.textContent = error.message;
    msg.style.display = 'block';
    btn.disabled = false; btn.textContent = '💳 Charge Card';
    return;
  }}

  try {{
    const resp = await fetch('{_portal_url}/charge-card', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Admin-Token': '{_charge_tok}'}},
      body: JSON.stringify({{
        payment_method_id: paymentMethod.id,
        amount: amount,
        subscriber_id: {sub.id},
        notes: notes,
        entered_by: '{_entered_by}',
      }}),
    }});
    const data = await resp.json();
    if (data.success) {{
      msg.className = 'ok';
      msg.textContent = '✅ Charged $' + parseFloat(data.amount).toFixed(2) +
        ' — expiration set to ' + data.new_expiration + '. Refreshing…';
      msg.style.display = 'block';
      btn.textContent = '✅ Done';
      window.parent.sessionStorage.setItem('stripe_charge_success', '1');
    }} else {{
      msg.className = 'err'; msg.textContent = data.error || 'Charge failed.';
      msg.style.display = 'block';
      btn.disabled = false; btn.textContent = '💳 Charge Card';
    }}
  }} catch(e) {{
    msg.className = 'err'; msg.textContent = 'Network error: ' + e.message;
    msg.style.display = 'block';
    btn.disabled = false; btn.textContent = '💳 Charge Card';
  }}
}});
</script>
</body>
</html>
""", height=250)

                # ── Tab 5: Delivery Hold ───────────────────────────────────────
                with tab5:
                    st.caption("Subscription does not run down on hold — the hold period is added back to expiration.")
                    holds = (db.query(DeliveryHold)
                        .filter_by(subscriber_id=sub.id)
                        .order_by(DeliveryHold.hold_start).all())
                    if holds:
                        hh1, hh2, hh3, hh4 = st.columns([2, 2, 3, 1])
                        hh1.markdown("**Start**"); hh2.markdown("**End**")
                        hh3.markdown("**Reason**"); hh4.markdown("")
                        for h in holds:
                            hc1, hc2, hc3, hc4 = st.columns([2, 2, 3, 1])
                            hc1.write(h.hold_start.strftime("%b %d, %Y"))
                            hc2.write(h.hold_end.strftime("%b %d, %Y"))
                            hc3.write(h.notes or "—")
                            if hc4.button("Remove", key=f"rmhold_{h.id}"):
                                hold_days = (h.hold_end - h.hold_start).days
                                db.add(HoldAuditLog(
                                    action="REMOVED", hold_id=h.id,
                                    subscriber_id=sub.id, subscriber_name=sub.full_name,
                                    hold_start=h.hold_start, hold_end=h.hold_end,
                                    notes=h.notes, entered_by=st.session_state.user["name"],
                                ))
                                db.delete(h)
                                db.flush()
                                # Subtract hold duration from expiration
                                if sub.expiration_date:
                                    sub.expiration_date -= timedelta(days=hold_days)
                                # Restore ACTIVE if no remaining active holds
                                remaining = db.query(DeliveryHold).filter(
                                    DeliveryHold.subscriber_id == sub.id,
                                    DeliveryHold.hold_start <= date.today(),
                                    DeliveryHold.hold_end >= date.today(),
                                ).count()
                                if remaining == 0 and sub.status == SubscriberStatus.ON_HOLD:
                                    sub.status = SubscriberStatus.ACTIVE
                                db.commit()
                                st.rerun()
                        st.divider()
                    else:
                        st.caption("No holds scheduled.")

                    st.markdown("#### Add a Hold")
                    with st.form("hold_form"):
                        hf1, hf2, hf3 = st.columns([2, 2, 3])
                        hold_start = hf1.date_input("Start", value=date.today())
                        hold_end   = hf2.date_input("End",   value=date.today() + timedelta(weeks=2))
                        hold_notes = hf3.text_input("Reason (optional)")
                        if st.form_submit_button("Add Hold", use_container_width=True):
                            if hold_end <= hold_start:
                                st.error("End date must be after start date.")
                            else:
                                hold_days = (hold_end - hold_start).days
                                new_hold = DeliveryHold(subscriber_id=sub.id, hold_start=hold_start,
                                                        hold_end=hold_end, notes=hold_notes or None)
                                db.add(new_hold)
                                db.flush()
                                # Extend expiration by hold duration
                                if sub.expiration_date:
                                    sub.expiration_date += timedelta(days=hold_days)
                                # Set ON_HOLD if hold has already started
                                if hold_start <= date.today():
                                    sub.status = SubscriberStatus.ON_HOLD
                                db.add(HoldAuditLog(
                                    action="ADDED", hold_id=new_hold.id,
                                    subscriber_id=sub.id, subscriber_name=sub.full_name,
                                    hold_start=hold_start, hold_end=hold_end,
                                    notes=hold_notes or None, entered_by=st.session_state.user["name"],
                                ))
                                db.commit()
                                st.success(f"Hold added — expiration extended by {hold_days} days.")
                                st.rerun()

                # ── Tab 6: History ─────────────────────────────────────────────
                with tab6:
                    EVENT_ICONS = {
                        "EMAIL_CHANGED":    "✉️",
                        "ADDRESS_SWITCHED": "🔄",
                        "ADDRESS_UPDATED":  "📬",
                    }
                    events_list = (db.query(SubscriberEventLog)
                        .filter_by(subscriber_id=sub.id)
                        .order_by(SubscriberEventLog.event_at.desc()).all())
                    payments_hist = (db.query(Payment)
                        .filter_by(subscriber_id=sub.id)
                        .order_by(Payment.paid_at.desc()).all())

                    rows = []
                    for p in payments_hist:
                        rows.append({"date": p.paid_at, "type": "payment", "obj": p})
                    for e in events_list:
                        rows.append({"date": e.event_at, "type": "event", "obj": e})
                    rows.sort(key=lambda r: r["date"] or datetime.min, reverse=True)

                    if rows:
                        for row in rows:
                            if row["type"] == "payment":
                                p = row["obj"]
                                c1, c2, c3, c4 = st.columns([2, 1, 3, 2])
                                c1.write(p.paid_at.strftime("%b %d, %Y  %I:%M %p") if p.paid_at else "—")
                                c2.write(f"**${p.amount:.2f}**")
                                c3.write(f"💳 {p.payment_method.value.replace('_',' ').title()}" +
                                    (f" — Check #{p.check_number}" if p.check_number else ""))
                                c4.caption(p.entered_by or "")
                            else:
                                e = row["obj"]
                                icon = EVENT_ICONS.get(e.event_type, "📋")
                                c1, c2, c3, c4 = st.columns([2, 1, 3, 2])
                                c1.write(e.event_at.strftime("%b %d, %Y  %I:%M %p"))
                                c2.write("")
                                c3.write(f"{icon} {e.event_type.replace('_', ' ').title()}")
                                c4.caption(e.description or "")
                            st.divider()
                    else:
                        st.caption("No account history yet.")


# ── Renewals ──────────────────────────────────────────────────────────────────

elif page == "🔔 Renewals":
    st.title("Renewal Management")

    import resend
    import os
    resend.api_key = os.environ.get("RESEND_API_KEY", "")

    today = date.today()
    in_30 = today + timedelta(days=30)
    in_60 = today + timedelta(days=60)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(["Expiring in 35 Days", "Expiring Soon (≤21d)", "Grace Period", "📬 Paper Renewals", "📧 Send Renewals"])

    def renewal_table(subs):
        if not subs:
            st.info("None.")
            return
        rows = [{
            "ID": s.id,
            "Name": s.full_name,
            "Email": s.email or "—",
            "Plan": PLAN_LABELS[s.plan],
            "Expires": s.expiration_date,
            "Payment": s.payment_method.value,
            "35d": "✓" if s.reminder_35_sent else "—",
            "21d": "✓" if s.reminder_21_sent else "—",
            "14d": "✓" if s.reminder_14_sent else "—",
            "Grace 14d": "✓" if s.grace_14_sent else "—",
        } for s in subs]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    with tab1:
        in_35 = today + timedelta(days=35)
        subs_35 = db.query(Subscriber).filter(
            Subscriber.status == SubscriberStatus.ACTIVE,
            Subscriber.expiration_date <= in_35,
            Subscriber.expiration_date > in_30,
            Subscriber.email.isnot(None),
        ).order_by(Subscriber.expiration_date).all()
        st.caption("Active subscribers expiring within 35 days. The nightly job emails these automatically.")
        renewal_table(subs_35)

    with tab2:
        subs_21 = db.query(Subscriber).filter(
            Subscriber.status == SubscriberStatus.ACTIVE,
            Subscriber.expiration_date <= in_30,
            Subscriber.expiration_date >= today,
            Subscriber.email.isnot(None),
        ).order_by(Subscriber.expiration_date).all()
        st.caption("Active subscribers expiring within 21 days. The nightly job emails these automatically.")
        renewal_table(subs_21)

    with tab3:
        grace_subs = db.query(Subscriber).filter(
            Subscriber.status == SubscriberStatus.GRACE,
        ).order_by(Subscriber.expiration_date).all()
        st.caption("Subscribers past their expiration date still receiving delivery during the grace period.")
        renewal_table(grace_subs)

    with tab4:
        import io as _io, csv as _csv
        st.subheader("📬 Paper Renewal Notices")
        st.caption("Generate printable renewal notices for subscribers — typically those without email or all expiring soon.")

        pr1, pr2, pr3 = st.columns([2, 2, 3])
        pr_from = pr1.date_input("Expiring from", value=today, key="pr_from")
        pr_to   = pr2.date_input("Expiring through", value=today + timedelta(days=90), key="pr_to")
        no_email_only = pr3.checkbox("No-email subscribers only", value=True, key="pr_no_email")

        paper_q = db.query(Subscriber).filter(
            Subscriber.status.in_([SubscriberStatus.ACTIVE, SubscriberStatus.GRACE]),
            Subscriber.expiration_date >= pr_from,
            Subscriber.expiration_date <= pr_to,
        )
        if no_email_only:
            paper_q = paper_q.filter(
                (Subscriber.email == None) | (Subscriber.email == "")
            )
        paper_subs = paper_q.order_by(Subscriber.zipcode, Subscriber.full_name).all()

        if not paper_subs:
            st.info("No subscribers match those criteria.")
        else:
            # ── Customize notice text ───────────────────────────────────────────
            with st.expander("✏️ Customize Notice Text", expanded=False):
                cx1, cx2 = st.columns(2)
                cx1.text_input("Organization name", value="The Duxbury Clipper", key="pdf_org")
                cx2.text_input("Check payable to", value="The Duxbury Clipper", key="pdf_payable")
                cx1.text_input("Mailing address (street/PO Box)", value="P.O. Box 1656", key="pdf_address")
                cx2.text_input("City, State, Zip", value="Duxbury, MA 02331", key="pdf_cityst")
                cx1.text_input("Phone", value="781-934-2811", key="pdf_phone")
                cx2.text_input("Contact email", value="subscribe@duxburyclipper.com", key="pdf_email")
                cx1.text_input("Website", value="www.duxburyclipper.com", key="pdf_website")
                st.text_input("Tagline (bold centered above rates table)",
                    value="Your hometown paper, delivered every Wednesday.", key="pdf_tagline")
                st.text_area("Fine print (optional — leave blank to omit)",
                    value="", height=80, key="pdf_fine")
                st.text_area("Return instructions",
                    value="Please return this form & your payment via courtesy envelope or mail to {org}, {address}, {cityst}.",
                    height=70, key="pdf_return",
                    help="Use {org}, {address}, {cityst} as placeholders.")
                st.text_input("Online / combo upsell line", key="pdf_online",
                    value="Want print + digital access? Visit www.duxburyclipper.com for combo packages.")
                st.text_input("Thank you line",
                    value="Thank you for subscribing to the Clipper and supporting your hometown newspaper!",
                    key="pdf_thanks")

            # ── Subscriber selection table ─────────────────────────────────────
            PLAN_SHORT = {
                PlanCode.LOCAL:         "Local",
                PlanCode.SENIOR:        "Senior",
                PlanCode.OUT_OF_COUNTY: "Out-of-County",
                PlanCode.SNOWBIRD:      "Snowbird",
                PlanCode.COMPLIMENTARY: "Comp",
                PlanCode.GIFT:          "Gift",
            }

            editor_rows = [{
                "Include":  True,
                "Name":     s.full_name,
                "Address":  s.address1 + (f", {s.address2}" if s.address2 else ""),
                "City":     s.city,
                "ST":       s.state,
                "Zip":      s.zipcode,
                "Status":   s.status.value,
                "Plan":     PLAN_SHORT.get(s.plan, s.plan.value),
                "Expires":  s.expiration_date,
                "Email":    s.email or "—",
                "_id":      s.id,
            } for s in paper_subs]

            edited = st.data_editor(
                pd.DataFrame(editor_rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Include": st.column_config.CheckboxColumn("✓", width="small"),
                    "Name":    st.column_config.TextColumn("Name", width="medium"),
                    "Address": st.column_config.TextColumn("Address", width="medium"),
                    "City":    st.column_config.TextColumn("City", width="small"),
                    "ST":      st.column_config.TextColumn("ST", width="small"),
                    "Zip":     st.column_config.TextColumn("Zip", width="small"),
                    "Status":  st.column_config.TextColumn("Status", width="small"),
                    "Plan":    st.column_config.TextColumn("Plan", width="small"),
                    "Expires": st.column_config.DateColumn("Expires", width="small"),
                    "Email":   st.column_config.TextColumn("Email", width="medium"),
                    "_id":     None,
                },
                disabled=["Name","Address","City","ST","Zip","Status","Plan","Expires","Email","_id"],
                key="paper_renewal_editor",
            )

            included_ids = set(edited.loc[edited["Include"] == True, "_id"].tolist())
            selected_subs = [s for s in paper_subs if s.id in included_ids]
            n = len(selected_subs)
            st.caption(f"**{n}** of {len(paper_subs)} subscribers selected for export.")

            st.divider()
            pc1, pc2 = st.columns(2)

            # ── CSV download ────────────────────────────────────────────────────
            if selected_subs:
                csv_rows = [{
                    "Name":     s.full_name,
                    "Address":  s.address1 + (f", {s.address2}" if s.address2 else ""),
                    "City":     s.city,
                    "ST":       s.state,
                    "Zip":      s.zipcode,
                    "Status":   s.status.value,
                    "Plan":     PLAN_LABELS[s.plan],
                    "Expires":  s.expiration_date,
                    "Email":    s.email or "",
                } for s in selected_subs]
                csv_buf = _io.StringIO()
                writer = _csv.DictWriter(csv_buf, fieldnames=list(csv_rows[0].keys()))
                writer.writeheader()
                writer.writerows(csv_rows)
                pc1.download_button(
                    f"⬇️ Download CSV ({n})",
                    data=csv_buf.getvalue().encode(),
                    file_name=f"paper_renewals_{today}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            else:
                pc1.button("⬇️ Download CSV", disabled=True, use_container_width=True)

            # ── PDF download ────────────────────────────────────────────────────
            if pc2.button(f"📄 Generate PDF ({n})", use_container_width=True, disabled=not selected_subs):
                from reportlab.lib.pagesizes import letter
                import os as _os
                from reportlab.lib.pagesizes import letter
                from reportlab.lib.units import inch
                from reportlab.lib import colors
                from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                                Table, TableStyle, HRFlowable, PageBreak,
                                                Image as RLImage)
                from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
                from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

                # Resolve customize values
                _org     = st.session_state.get("pdf_org",     "The Duxbury Clipper")
                _phone   = st.session_state.get("pdf_phone",   "781-934-2811")
                _addr    = st.session_state.get("pdf_address", "P.O. Box 1656")
                _cityst  = st.session_state.get("pdf_cityst",  "Duxbury, MA 02331")
                _website = st.session_state.get("pdf_website", "www.duxburyclipper.com")
                _email_c = st.session_state.get("pdf_email",   "subscribe@duxburyclipper.com")
                _payable = st.session_state.get("pdf_payable", "The Duxbury Clipper")
                _tagline = st.session_state.get("pdf_tagline", "Your hometown paper, delivered every Wednesday.")
                _fine    = st.session_state.get("pdf_fine", "").strip()
                _online  = st.session_state.get("pdf_online",
                    f"Want print + digital access? Visit <b>{_website}</b> for combo packages.")
                _return  = (st.session_state.get("pdf_return",
                    "Please return this form & your payment via courtesy envelope or mail to {org}, {address}, {cityst}.")
                    .replace("{org}", _org).replace("{address}", _addr).replace("{cityst}", _cityst))
                _thanks  = st.session_state.get("pdf_thanks",
                    f"Thank you for subscribing to the Clipper and supporting your hometown newspaper!")

                pdf_buf = _io.BytesIO()
                doc = SimpleDocTemplate(pdf_buf, pagesize=letter,
                                        leftMargin=0.75*inch, rightMargin=0.75*inch,
                                        topMargin=0.6*inch, bottomMargin=0.6*inch)
                styles = getSampleStyleSheet()

                # Styles
                renew_style  = ParagraphStyle("pren", parent=styles["Normal"],
                    fontSize=15, fontName="Helvetica-BoldOblique", alignment=TA_CENTER,
                    spaceBefore=6, spaceAfter=6)
                addr_style   = ParagraphStyle("paddr", parent=styles["Normal"],
                    fontSize=11, leading=14, fontName="Helvetica")
                addr_r_style = ParagraphStyle("paddrr", parent=styles["Normal"],
                    fontSize=10, leading=14, fontName="Helvetica-Oblique",
                    textColor=colors.HexColor("#333333"))
                tagline_style= ParagraphStyle("ptag", parent=styles["Normal"],
                    fontSize=13, fontName="Helvetica-Bold", alignment=TA_CENTER,
                    spaceBefore=8, spaceAfter=6)
                body_style   = ParagraphStyle("pbody", parent=styles["Normal"],
                    fontSize=10, leading=14, fontName="Helvetica")
                center_style = ParagraphStyle("pctr", parent=styles["Normal"],
                    fontSize=10, leading=14, fontName="Helvetica", alignment=TA_CENTER)
                small_style  = ParagraphStyle("psmall", parent=styles["Normal"],
                    fontSize=8, leading=11, fontName="Helvetica",
                    textColor=colors.HexColor("#444444"))
                online_style = ParagraphStyle("ponline", parent=styles["Normal"],
                    fontSize=10, fontName="Helvetica-Bold", alignment=TA_CENTER,
                    spaceBefore=6, spaceAfter=4)
                thanks_style = ParagraphStyle("pthanks", parent=styles["Normal"],
                    fontSize=11, fontName="Helvetica-Oblique", alignment=TA_CENTER,
                    spaceBefore=8)
                hdraddr_style= ParagraphStyle("phdraddr", parent=styles["Normal"],
                    fontSize=10, leading=14, fontName="Helvetica", alignment=TA_RIGHT)

                # Logo path
                _logo_path = _os.path.join(_os.path.dirname(__file__), "logo.png")

                # Count Wednesdays remaining helper
                def _issues_left(exp_date):
                    if not exp_date or exp_date < date.today():
                        return 0
                    delta = (exp_date - date.today()).days + 1
                    return sum(1 for i in range(delta)
                               if (date.today() + timedelta(days=i)).weekday() == 2)

                story = []
                for s in selected_subs:
                    # ── Header: logo left, address right ──────────────────────
                    logo_w = 3.2*inch
                    logo_h = logo_w * (323/1741)
                    logo_cell = RLImage(_logo_path, width=logo_w, height=logo_h) \
                                if _os.path.exists(_logo_path) else Paragraph(_org, renew_style)

                    hdr_addr_text = (f"{_org}<br/>{_addr}<br/>{_cityst}<br/>"
                                     f"<i>{_website}</i>")
                    hdr_tbl = Table(
                        [[logo_cell, Paragraph(hdr_addr_text, hdraddr_style)]],
                        colWidths=[4.0*inch, 3.0*inch],
                    )
                    hdr_tbl.setStyle(TableStyle([
                        ("VALIGN",  (0,0), (-1,-1), "TOP"),
                        ("TOPPADDING", (0,0), (-1,-1), 0),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 0),
                    ]))
                    story.append(hdr_tbl)
                    story.append(HRFlowable(width="100%", thickness=1,
                                            color=colors.black, spaceAfter=8))

                    # ── "It's time to renew!" ──────────────────────────────────
                    story.append(Paragraph("<i>It's time to renew!</i>", renew_style))
                    story.append(Spacer(1, 0.08*inch))

                    # ── Subscriber address (left) + expiry info (right) ────────
                    issues = _issues_left(s.expiration_date)
                    addr_lines = []
                    if s.simplecirc_id:
                        addr_lines.append(f"<i>{s.simplecirc_id}</i>")
                    addr_lines += [s.full_name, s.address1]
                    if s.address2:
                        addr_lines.append(s.address2)
                    addr_lines.append(f"{s.city}, {s.state} {s.zipcode}")

                    expiry_text = (
                        f"You have <b>{issues}</b> issue{'s' if issues != 1 else ''} remaining.<br/>"
                        f"Your subscription will expire: "
                        f"<b>{s.expiration_date.strftime('%m/%d/%Y') if s.expiration_date else '—'}</b>"
                    )
                    sub_tbl = Table(
                        [[Paragraph("<br/>".join(addr_lines), addr_style),
                          Paragraph(expiry_text, addr_r_style)]],
                        colWidths=[3.5*inch, 3.5*inch],
                    )
                    sub_tbl.setStyle(TableStyle([
                        ("VALIGN", (0,0), (-1,-1), "TOP"),
                        ("TOPPADDING", (0,0), (-1,-1), 2),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 2),
                    ]))
                    story.append(sub_tbl)
                    story.append(Spacer(1, 0.18*inch))

                    # ── Tagline ────────────────────────────────────────────────
                    story.append(Paragraph(_tagline, tagline_style))

                    # ── All rates table with checkboxes ────────────────────────
                    rate_data = [["", "Plan", "Rate"]]
                    for plan_code, label in PLAN_LABELS.items():
                        if plan_code in (PlanCode.COMPLIMENTARY, PlanCode.GIFT):
                            continue
                        price = PLAN_PRICES.get(plan_code, 0)
                        weeks = 52
                        weekly = price / weeks if weeks else 0
                        # Highlight subscriber's current plan
                        is_this = (plan_code == s.plan)
                        check = "☑" if is_this else "☐"
                        rate_str = f"${weekly:.2f} a week = ${price:,.0f} per year"
                        rate_data.append([check, label, rate_str])

                    rate_tbl = Table(rate_data, colWidths=[0.3*inch, 3.2*inch, 3.5*inch])
                    rate_tbl.setStyle(TableStyle([
                        ("FONTNAME",      (0,0), (-1,0), "Helvetica-Bold"),
                        ("FONTSIZE",      (0,0), (-1,-1), 10),
                        ("TOPPADDING",    (0,0), (-1,-1), 5),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
                        ("LEFTPADDING",   (0,0), (-1,-1), 6),
                        ("GRID",          (0,0), (-1,-1), 0.5, colors.HexColor("#bbbbbb")),
                        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#eeeeee")),
                        # Highlight subscriber's row
                        *[("BACKGROUND", (0, i+1), (-1, i+1), colors.HexColor("#e8f5e9"))
                          for i, (pc, _) in enumerate(
                              [(k,v) for k,v in PLAN_LABELS.items() if k != PlanCode.COMPLIMENTARY])
                          if pc == s.plan],
                        ("FONTNAME",      (2,0), (2,-1), "Helvetica"),
                    ]))
                    story.append(rate_tbl)
                    story.append(Spacer(1, 0.15*inch))

                    # ── Payment method ─────────────────────────────────────────
                    pay_tbl = Table(
                        [["____  Pay by check", "____  Charge my credit card*"]],
                        colWidths=[3.5*inch, 3.5*inch],
                    )
                    pay_tbl.setStyle(TableStyle([
                        ("FONTSIZE",   (0,0), (-1,-1), 11),
                        ("FONTNAME",   (0,0), (-1,-1), "Helvetica"),
                        ("ALIGN",      (0,0), (0,0),   "LEFT"),
                        ("ALIGN",      (1,0), (1,0),   "RIGHT"),
                        ("TOPPADDING", (0,0), (-1,-1),  3),
                        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                    ]))
                    story.append(pay_tbl)
                    story.append(Spacer(1, 0.1*inch))

                    # CC fields
                    cc_tbl = Table(
                        [["Credit card #: ", "_" * 42],
                         ["Exp: _______/_______ ", "CVC ____________   Billing Zip ____________"]],
                        colWidths=[1.5*inch, 5.5*inch],
                    )
                    cc_tbl.setStyle(TableStyle([
                        ("FONTSIZE",   (0,0), (-1,-1), 10),
                        ("FONTNAME",   (0,0), (-1,-1), "Helvetica"),
                        ("TOPPADDING", (0,0), (-1,-1),  4),
                        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
                        ("LEFTPADDING",(0,0), (-1,-1),  4),
                    ]))
                    story.append(cc_tbl)
                    story.append(Spacer(1, 0.1*inch))

                    story.append(Paragraph(_return, center_style))
                    story.append(Spacer(1, 0.1*inch))

                    # Email capture
                    email_tbl = Table(
                        [["If you would like to provide an email address for future renewal notices: ",
                          "_" * 28]],
                        colWidths=[4.2*inch, 2.8*inch],
                    )
                    email_tbl.setStyle(TableStyle([
                        ("FONTSIZE",   (0,0), (-1,-1), 9),
                        ("FONTNAME",   (0,0), (-1,-1), "Helvetica"),
                        ("TOPPADDING", (0,0), (-1,-1),  3),
                        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
                    ]))
                    story.append(email_tbl)
                    story.append(HRFlowable(width="100%", thickness=0.5,
                                            color=colors.HexColor("#aaaaaa"), spaceAfter=4))

                    if _fine:
                        story.append(Paragraph(_fine, small_style))
                    story.append(Spacer(1, 0.08*inch))
                    story.append(Paragraph(_online, online_style))
                    story.append(Paragraph(_thanks, thanks_style))
                    story.append(PageBreak())

                doc.build(story)
                st.download_button(
                    "⬇️ Download PDF",
                    data=pdf_buf.getvalue(),
                    file_name=f"renewal_notices_{today}.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )

    with tab5:
        import io as _io2
        st.subheader("📧 Send Renewal Emails")
        st.caption(
            "Review every email queued to go out, uncheck anyone to skip, then click **Approve & Send**. "
            "Nothing is sent until you click that button."
        )

        # ── Status advancement (separate, always safe) ─────────────────────────
        with st.expander("⚙️ Advance Subscriber Statuses", expanded=False):
            st.write(
                "This updates statuses only — no emails sent. "
                "Run this weekly to keep Active/Grace/Expired current."
            )
            adv_col1, adv_col2 = st.columns([3,1])
            adv_col1.markdown(
                "- Subscribers past their expiry date → **Grace**  \n"
                "- Subscribers 28+ days past expiry → **Expired** (delivery stops)"
            )
            if adv_col2.button("▶ Run Status Advance", use_container_width=True):
                changed = 0
                grace_cutoff = today - timedelta(days=28)
                for s in db.query(Subscriber).filter(
                    Subscriber.status == SubscriberStatus.ACTIVE,
                    Subscriber.expiration_date < today,
                    Subscriber.expiration_date.isnot(None),
                ).all():
                    s.status = SubscriberStatus.GRACE
                    changed += 1
                for s in db.query(Subscriber).filter(
                    Subscriber.status == SubscriberStatus.GRACE,
                    Subscriber.expiration_date < grace_cutoff,
                    Subscriber.expiration_date.isnot(None),
                ).all():
                    s.status = SubscriberStatus.EXPIRED
                    changed += 1
                db.commit()
                st.success(f"✅ {changed} status changes applied.")

        st.divider()

        # ── Build the queue ────────────────────────────────────────────────────
        RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
        if not RESEND_API_KEY:
            st.warning(
                "⚠️ **RESEND_API_KEY not configured** — emails will be previewed but not sent. "
                "Set the key in your .env file to enable sending."
            )

        all_subs = db.query(Subscriber).filter(
            Subscriber.status.in_([SubscriberStatus.ACTIVE, SubscriberStatus.GRACE]),
            Subscriber.expiration_date.isnot(None),
            Subscriber.email.isnot(None),
            Subscriber.email != "",
        ).all()

        # Map each subscriber to the single most-due email they should receive
        EFFORT_ORDER = [
            ("grace_final_sent",    "Final Grace Notice (paper stopping)",  "🔴"),
            ("grace_14_sent",       "Grace Period — Past Due",               "🟠"),
            ("reminder_expire_sent","Expiry Day Notice",                     "🟠"),
            ("reminder_14_sent",    "14-Day Reminder (2 issues left)",       "🟡"),
            ("reminder_21_sent",    "21-Day Reminder (3 issues left)",       "🟡"),
            ("reminder_35_sent",    "35-Day Reminder (5 issues left)",       "🟢"),
        ]

        queue = []  # (subscriber, flag_attr, label, icon)
        for s in all_subs:
            exp = s.expiration_date
            days_to_exp  = (exp - today).days
            days_past_exp = (today - exp).days

            eligibility = {
                "grace_final_sent":    days_past_exp >= 27 and not s.grace_final_sent,
                "grace_14_sent":       days_past_exp >= 14 and not s.grace_14_sent,
                "reminder_expire_sent":days_to_exp <= 0   and not s.reminder_expire_sent,
                "reminder_14_sent":    days_to_exp <= 14  and not s.reminder_14_sent,
                "reminder_21_sent":    days_to_exp <= 21  and not s.reminder_21_sent,
                "reminder_35_sent":    days_to_exp <= 35  and not s.reminder_35_sent,
            }

            for flag_attr, label, icon in EFFORT_ORDER:
                if eligibility.get(flag_attr):
                    queue.append({
                        "Send":    True,
                        "icon":    icon,
                        "Type":    label,
                        "Name":    s.full_name,
                        "Email":   s.email,
                        "Expires": exp,
                        "_id":     s.id,
                        "_flag":   flag_attr,
                    })
                    break  # only most urgent per subscriber per run

        if not queue:
            st.info("✅ No renewal emails are queued right now. Everyone is up to date.")
        else:
            st.markdown(f"**{len(queue)} email{'s' if len(queue)!=1 else ''} ready to review:**")

            edited_q = st.data_editor(
                pd.DataFrame(queue),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Send":    st.column_config.CheckboxColumn("Send", width="small"),
                    "icon":    st.column_config.TextColumn("", width="small"),
                    "Type":    st.column_config.TextColumn("Email Type", width="large"),
                    "Name":    st.column_config.TextColumn("Subscriber", width="medium"),
                    "Email":   st.column_config.TextColumn("Email", width="medium"),
                    "Expires": st.column_config.DateColumn("Expires", width="small"),
                    "_id":     None,
                    "_flag":   None,
                },
                disabled=["icon","Type","Name","Email","Expires","_id","_flag"],
                key="renewal_send_editor",
            )

            approved = edited_q[edited_q["Send"] == True]
            n_approved = len(approved)

            st.caption(f"**{n_approved}** of {len(queue)} selected to send.")

            send_col, _ = st.columns([2, 3])
            if send_col.button(
                f"📤 Approve & Send {n_approved} Email{'s' if n_approved!=1 else ''}",
                use_container_width=True,
                type="primary",
                disabled=n_approved == 0,
            ):
                if not RESEND_API_KEY:
                    st.error("Cannot send — RESEND_API_KEY not set. Emails previewed only.")
                else:
                    import resend as _resend
                    _resend.api_key = RESEND_API_KEY

                    from cron.nightly import (email_35_days, email_21_days, email_14_days,
                                              email_expire_day, email_grace_14, email_grace_final,
                                              FROM_EMAIL, FROM_NAME)
                    EMAIL_FNS = {
                        "reminder_35_sent":    email_35_days,
                        "reminder_21_sent":    email_21_days,
                        "reminder_14_sent":    email_14_days,
                        "reminder_expire_sent":email_expire_day,
                        "grace_14_sent":       email_grace_14,
                        "grace_final_sent":    email_grace_final,
                    }

                    sent = 0
                    errors = []
                    for _, row in approved.iterrows():
                        sub = db.query(Subscriber).filter_by(id=int(row["_id"])).first()
                        if not sub:
                            continue
                        fn = EMAIL_FNS.get(row["_flag"])
                        if not fn:
                            continue
                        subject, html = fn(sub)
                        try:
                            _resend.Emails.send({
                                "from": f"{FROM_NAME} <{FROM_EMAIL}>",
                                "to": sub.email,
                                "subject": subject,
                                "html": html,
                            })
                            setattr(sub, row["_flag"], True)
                            sent += 1
                        except Exception as e:
                            errors.append(f"{sub.full_name}: {e}")

                    db.commit()
                    if sent:
                        st.success(f"✅ {sent} email{'s' if sent!=1 else ''} sent successfully.")
                    if errors:
                        for err in errors:
                            st.error(err)


# ── Delivery List ──────────────────────────────────────────────────────────────

elif page == "📦 Delivery List":
    st.title("Weekly Delivery List")

    today = date.today()

    # Active subs not on hold this week
    active_subs = db.query(Subscriber).filter(
        Subscriber.status.in_([SubscriberStatus.ACTIVE, SubscriberStatus.GRACE])
    ).all()

    # Filter out subscribers on hold today
    on_hold_ids = set()
    for sub in active_subs:
        for hold in sub.holds:
            if hold.hold_start <= today <= hold.hold_end:
                on_hold_ids.add(sub.id)
                break

    delivery = [s for s in active_subs if s.id not in on_hold_ids]

    st.metric("Delivering this week", len(delivery))
    st.metric("On hold this week", len(on_hold_ids))

    rows = [{
        "Name": s.full_name,
        "Address 1": s.address1,
        "Address 2": s.address2 or "",
        "City": s.city,
        "State": s.state,
        "Zip": s.zipcode,
        "Postage": s.postage_code.value if s.postage_code else "P",
        "Copies": s.copies,
    } for s in delivery]

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False)
    st.download_button(
        label="Download CSV for Press",
        data=csv,
        file_name=f"delivery-list-{today}.csv",
        mime="text/csv",
    )

    # Email to press
    import resend, os
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    press_email = os.environ.get("PRESS_EMAIL", "")
    if press_email and st.button("Email List to Press"):
        try:
            resend.Emails.send({
                "from": f"Duxbury Clipper <{os.environ.get('FROM_EMAIL', 'subscribe@duxburyclipper.com')}>",
                "to": press_email,
                "subject": f"Duxbury Clipper Delivery List — {today}",
                "text": f"Delivery list for week of {today}. {len(delivery)} copies.",
                "attachments": [{
                    "filename": f"delivery-list-{today}.csv",
                    "content": csv,
                }],
            })
            st.success(f"Delivery list emailed to {press_email}.")
        except Exception as e:
            st.error(f"Failed to send: {e}")


# ── Duplicates ────────────────────────────────────────────────────────────────

elif page == "🔁 Duplicates":
    st.title("Duplicate Detection")
    st.write("Subscribers flagged as possible duplicates based on matching address or name + zip. Review and merge manually.")

    from collections import defaultdict
    all_subs = db.query(Subscriber).filter(
        Subscriber.status != SubscriberStatus.CANCELLED
    ).all()

    addr_groups = defaultdict(list)
    name_zip_groups = defaultdict(list)
    for s in all_subs:
        addr_key = (s.address1.strip().upper(), s.zipcode.strip()[:5])
        addr_groups[addr_key].append(s)
        last_name = s.full_name.strip().split()[-1].upper() if s.full_name.strip() else ""
        name_zip_groups[(last_name, s.zipcode.strip()[:5])].append(s)

    dupes = {}
    for key, group in addr_groups.items():
        if len(group) > 1:
            dupes[f"Same address: {key[0]}, {key[1]}"] = group
    for key, group in name_zip_groups.items():
        if len(group) > 1:
            label = f"Same last name + zip: {key[0]}, {key[1]}"
            if label not in dupes:
                dupes[label] = group

    if not dupes:
        st.success("No likely duplicates found.")
    else:
        st.warning(f"{len(dupes)} potential duplicate groups found.")
        for label, group in dupes.items():
            with st.expander(f"{label} ({len(group)} records)"):
                rows = [{
                    "ID": s.id,
                    "SimpleCirc ID": s.simplecirc_id or "—",
                    "Name": s.full_name,
                    "Address": s.address1,
                    "City": s.city,
                    "Zip": s.zipcode,
                    "Plan": PLAN_LABELS[s.plan],
                    "Status": s.status.value,
                    "Expires": s.expiration_date,
                    "Email": s.email or "—",
                } for s in group]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                st.caption("To merge: keep one record, cancel the other from the Subscribers page.")


elif page == "🧾 Payment Log":
    st.title("Payment Audit Log")
    st.caption("Complete history of all payments — including edited and deleted records. This log is never erased.")

    from sqlalchemy import desc as sa_desc
    logs = db.query(PaymentAuditLog).order_by(sa_desc(PaymentAuditLog.event_at)).limit(500).all()

    if logs:
        lf1, lf2 = st.columns(2)
        action_filter = lf1.selectbox("Filter by action", ["All", "CREATED", "EDITED", "DELETED"])
        name_filter = lf2.text_input("Filter by subscriber name")

        rows = []
        for l in logs:
            if action_filter != "All" and l.action != action_filter:
                continue
            if name_filter and name_filter.lower() not in (l.subscriber_name or "").lower():
                continue
            rows.append({
                "Date/Time": l.event_at.strftime("%Y-%m-%d %H:%M"),
                "Action": l.action,
                "Subscriber": l.subscriber_name or "—",
                "Sub ID": l.subscriber_id,
                "Amount": f"${l.amount}" if l.amount is not None else "—",
                "Method": l.payment_method or "—",
                "Check #": l.check_number or "—",
                "Period": f"{l.period_start} – {l.period_end}" if l.period_start else "—",
                "Entered By": l.entered_by or "—",
                "Notes": l.notes or "—",
            })

        if rows:
            df_log = pd.DataFrame(rows)

            def color_action(val):
                colors = {"CREATED": "#e8f5e9", "EDITED": "#fff8e1", "DELETED": "#fde8e8"}
                return f"background-color:{colors.get(val, 'white')}"

            st.dataframe(
                df_log.style.applymap(color_action, subset=["Action"]),
                use_container_width=True, hide_index=True
            )
            st.caption(f"Showing {len(rows)} entries")
        else:
            st.info("No entries match your filter.")
    else:
        st.info("No payment history yet.")

elif page == "📋 Hold Log":
    st.title("Delivery Hold Audit Log")
    st.caption("Complete history of all delivery holds added or removed. This log is never erased.")

    from sqlalchemy import desc as sa_desc2
    logs = db.query(HoldAuditLog).order_by(sa_desc2(HoldAuditLog.event_at)).limit(500).all()

    if logs:
        lf1, lf2 = st.columns(2)
        action_filter = lf1.selectbox("Filter by action", ["All", "ADDED", "REMOVED"], key="hold_log_action")
        name_filter = lf2.text_input("Filter by subscriber name", key="hold_log_name")

        rows = []
        for l in logs:
            if action_filter != "All" and l.action != action_filter:
                continue
            if name_filter and name_filter.lower() not in (l.subscriber_name or "").lower():
                continue
            rows.append({
                "Date/Time": l.event_at.strftime("%Y-%m-%d %H:%M"),
                "Action": l.action,
                "Subscriber": l.subscriber_name or "—",
                "Sub ID": l.subscriber_id,
                "Hold Start": str(l.hold_start) if l.hold_start else "—",
                "Hold End": str(l.hold_end) if l.hold_end else "—",
                "Reason": l.notes or "—",
                "Entered By": l.entered_by or "—",
            })

        if rows:
            df_hlog = pd.DataFrame(rows)

            def color_hold_action(val):
                return "background-color:#e8f5e9" if val == "ADDED" else "background-color:#fde8e8"

            st.dataframe(
                df_hlog.style.applymap(color_hold_action, subset=["Action"]),
                use_container_width=True, hide_index=True
            )
            st.caption(f"Showing {len(rows)} entries")
        else:
            st.info("No entries match your filter.")
    else:
        st.info("No hold history yet.")

elif page == "📰 Obituaries":
    st.title("📰 Obituary Submissions")
    submissions = db.query(ObituarySubmission).order_by(ObituarySubmission.submitted_at.desc()).all()
    if not submissions:
        st.info("No obituary submissions yet.")
    else:
        search = st.text_input("Search by name or submitter", placeholder="Type to filter...")
        filtered = [s for s in submissions if not search or
                    search.lower() in (s.deceased_name or "").lower() or
                    search.lower() in f"{s.submitter_first_name} {s.submitter_last_name}".lower()]
        st.caption(f"{len(filtered)} submission{'s' if len(filtered) != 1 else ''}")
        for s in filtered:
            label = f"{s.deceased_name}  —  submitted {s.submitted_at.strftime('%b %d, %Y %I:%M %p')}  —  ${float(s.amount_paid or 0):.2f}"
            with st.expander(label):
                c1, c2 = st.columns(2)
                c1.markdown(f"**Deceased:** {s.deceased_name}")
                c1.markdown(f"**Age:** {s.age or '—'}")
                c1.markdown(f"**Date of Death:** {s.date_of_death or '—'}")
                c1.markdown(f"**Word Count:** {s.word_count or '—'}")
                c1.markdown(f"**Photo Submitted:** {'Yes' if s.photo_submitted else 'No'}")
                c2.markdown(f"**Submitter:** {s.submitter_first_name} {s.submitter_last_name}")
                c2.markdown(f"**Email:** {s.submitter_email or '—'}")
                c2.markdown(f"**Phone:** {s.submitter_phone or '—'}")
                c2.markdown(f"**Relation:** {s.relation or '—'}")
                st.markdown(f"**Amount Charged:** ${float(s.amount_paid or 0):.2f}  |  **Card:** {s.card_description or '—'}  |  **Confirmation:** `{s.stripe_pi_id or '—'}`")
                st.divider()
                st.markdown("**Obituary Text:**")
                st.text_area("", value=s.obit_text or "", height=250, key=f"obit_text_{s.id}", disabled=False, label_visibility="collapsed")
                st.caption(f"IP: {s.ip_address or '—'}  |  Browser: {s.user_agent or '—'}")

elif page == "⚙️ Settings":
    if not st.session_state.user["is_admin"]:
        st.error("Admin access required.")
    else:
        st.title("⚙️ Settings")
        settings = load_settings()

        # ── Staff Users ────────────────────────────────────────────────────────
        st.subheader("👥 Staff Users")
        users = db.query(StaffUser).all()
        rows = [{"ID": u.id, "Name": u.name, "Email": u.email, "Admin": u.is_admin, "Active": u.is_active} for u in users]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        with st.form("add_staff"):
            sc1, sc2 = st.columns(2)
            new_name = sc1.text_input("Name")
            new_email = sc2.text_input("Email")
            new_password = st.text_input("Temporary Password", type="password")
            new_is_admin = st.checkbox("Admin")
            if st.form_submit_button("Add Staff User"):
                if new_name and new_email and new_password:
                    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
                    user = StaffUser(name=new_name, email=new_email.lower(), password_hash=pw_hash, is_admin=new_is_admin)
                    db.add(user)
                    db.commit()
                    st.success(f"User {new_name} added.")
                else:
                    st.error("All fields required.")

        st.divider()

        # ── Obituary Email Settings ────────────────────────────────────────────
        st.subheader("📰 Obituary Notification Emails")
        st.caption("Who receives the staff notification email when an obituary is submitted.")
        obit_to_row = db.query(Setting).filter_by(key="obit_notify_email").first()
        obit_cc_row = db.query(Setting).filter_by(key="obit_notify_cc").first()
        with st.form("obit_email_settings"):
            obit_to = st.text_input("Send To (primary)", value=obit_to_row.value if obit_to_row else "josh@joshcutler.com")
            obit_cc = st.text_input("CC (comma-separated, optional)", value=obit_cc_row.value if obit_cc_row else "")
            if st.form_submit_button("Save Obituary Email Settings"):
                for key, val in [("obit_notify_email", obit_to.strip()), ("obit_notify_cc", obit_cc.strip())]:
                    row = db.query(Setting).filter_by(key=key).first()
                    if row:
                        row.value = val
                    else:
                        db.add(Setting(key=key, value=val))
                db.commit()
                st.success("Obituary email settings saved.")

        st.divider()

        # ── Email Templates & Schedule ─────────────────────────────────────────
        st.subheader("✉️ Renewal Email Templates")
        st.caption("Edit subject and body for each automatic renewal email. Use **{first_name}**, **{full_name}**, and **{expiration_date}** as placeholders.")

        templates = settings.get("email_templates", DEFAULT_SETTINGS["email_templates"])
        schedule  = settings.get("email_schedule",  DEFAULT_SETTINGS["email_schedule"])

        EMAIL_META = [
            ("reminder_35", "📬 First Notice",        "Sent {reminder_35_days} days before expiration (~5 issues out)"),
            ("reminder_21", "📬 Second Notice",       "Sent {reminder_21_days} days before expiration (~3 issues out)"),
            ("reminder_14", "📬 Third Notice",        "Sent {reminder_14_days} days before expiration (~2 issues out)"),
            ("expire_day",  "⚠️ Expiration Day",      "Sent on the day the subscription expires"),
            ("grace_14",    "🔴 Grace — Mid Notice",  "Sent {grace_14_days} days into the grace period"),
            ("grace_final", "🔴 Grace — Final Notice","Sent {grace_final_days} days into the grace period (last warning before delivery stops)"),
        ]

        st.markdown("**Send Schedule** — days relative to expiration date when each email fires.")
        sc1, sc2, sc3, sc4, sc5 = st.columns(5)
        new_schedule = {}
        new_schedule["reminder_35_days"] = sc1.number_input("1st notice (days before)", min_value=1, step=1, value=int(schedule.get("reminder_35_days", 35)), key="sch_35")
        new_schedule["reminder_21_days"] = sc2.number_input("2nd notice (days before)", min_value=1, step=1, value=int(schedule.get("reminder_21_days", 21)), key="sch_21")
        new_schedule["reminder_14_days"] = sc3.number_input("3rd notice (days before)", min_value=1, step=1, value=int(schedule.get("reminder_14_days", 14)), key="sch_14")
        new_schedule["grace_14_days"]    = sc4.number_input("Grace mid (days after)",   min_value=1, step=1, value=int(schedule.get("grace_14_days", 14)),    key="sch_g14")
        new_schedule["grace_final_days"] = sc5.number_input("Grace final (days after)", min_value=1, step=1, value=int(schedule.get("grace_final_days", 27)), key="sch_gfin")

        new_templates = {}
        for key, label, hint_tpl in EMAIL_META:
            hint = hint_tpl.format(**new_schedule)
            with st.expander(f"{label}  —  _{hint}_", expanded=False):
                tmpl = templates.get(key, DEFAULT_SETTINGS["email_templates"].get(key, {}))
                subj = st.text_input("Subject line", value=tmpl.get("subject",""), key=f"subj_{key}")
                body = st.text_area("Body copy", value=tmpl.get("body",""), height=200, key=f"body_{key}",
                    help="Plain text. Use {first_name}, {full_name}, {expiration_date} as placeholders.")
                col_a, col_b = st.columns(2)
                btn_color = col_a.color_picker("Button color", value=tmpl.get("btn_color", "#2e7d32"), key=f"btn_color_{key}")
                box_color = col_b.color_picker("Accent box color", value=tmpl.get("box_color", "#2e7d32"), key=f"box_color_{key}")
                new_templates[key] = {"subject": subj, "body": body, "btn_color": btn_color, "box_color": box_color}

                st.markdown("**Preview** *(placeholders filled with sample data)*")
                preview_body = body.replace("{first_name}", "Jane").replace("{full_name}", "Jane Subscriber").replace("{expiration_date}", "August 15, 2026")
                st.markdown(
                    f"""<div style="background:#f9f9f9;border:1px solid #ddd;border-radius:6px;padding:16px 20px;font-family:Georgia,serif;font-size:0.92em;line-height:1.7;white-space:pre-wrap;color:#333;">
<strong style="font-size:1.05em;color:#1a3a1a;">The Duxbury Clipper</strong><br>
<span style="color:#888;font-size:0.85em;">P.O. Box 1656 • Duxbury, MA 02331 • 781-934-2811</span>
<hr style="border:none;border-top:1px solid #ddd;margin:10px 0;">
<div style="background:#f9f9f9;border-left:4px solid {box_color};border-radius:4px;padding:10px 14px;margin:12px 0;font-size:0.88em;color:#555;">
<strong>Plan:</strong> Duxbury/Plymouth County<br><strong>Renewal rate:</strong> $50.00/year &nbsp;•&nbsp; Just 96 cents a week!
</div>
Dear Jane,

{preview_body}

<div style="text-align:center;margin:16px 0;">
  <span style="background:{btn_color};color:white;padding:10px 24px;border-radius:6px;font-weight:bold;">Renew My Subscription</span>
</div>
</div>""", unsafe_allow_html=True)

                st.markdown("**Send a test email**")
                tc1, tc2 = st.columns([3, 1])
                test_addr = tc1.text_input("Email address", key=f"test_addr_{key}", placeholder="you@example.com", label_visibility="collapsed")
                if tc2.button("Send Test", key=f"test_btn_{key}"):
                    if not test_addr:
                        st.warning("Enter an email address first.")
                    else:
                        try:
                            import resend as _resend
                            import sys, os as _os
                            sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
                            from cron.nightly import _base, _body_paragraphs, _renew_btn
                            _resend.api_key = os.environ.get("RESEND_API_KEY", "")
                            from_email = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.net")
                            _subj = subj.replace("{first_name}", "Jane").replace("{full_name}", "Jane Subscriber").replace("{expiration_date}", "August 15, 2026")
                            _body_text = preview_body.replace("{price}", "$50.00")
                            _html = _base("Jane", _body_paragraphs(_body_text), _renew_btn("#", color=btn_color),
                                          price="$50.00", portal_link="#", plan_label="Duxbury/Plymouth County",
                                          box_color=box_color)
                            _resend.Emails.send({"from": f"Duxbury Clipper <{from_email}>", "to": test_addr, "subject": f"[TEST] {_subj}", "html": _html})
                            st.success(f"✓ Test email sent to {test_addr}")
                        except Exception as e:
                            st.error(f"Failed to send: {e}")

        if st.button("💾 Save Email Templates & Schedule", type="primary", use_container_width=True):
            settings["email_templates"] = new_templates
            settings["email_schedule"]  = new_schedule
            save_settings(settings)
            st.success("✓ Email templates and schedule saved.")

        st.divider()
        st.subheader("Subscription Prices & Durations")
        plan_labels_map = {
            "LOCAL": "Local (Duxbury / Plymouth County)",
            "SENIOR": "Senior Citizen",
            "OUT_OF_COUNTY": "Out of County",
            "SNOWBIRD": "Senior Snowbird",
            "GIFT": "Gift Subscription",
            "COMPLIMENTARY": "Complimentary (free)",
        }

        hc1, hc2, hc3 = st.columns([3, 1, 1])
        hc2.markdown("**Price ($)**")
        hc3.markdown("**Duration (wks)**")

        with st.form("settings_form"):
            new_prices = {}
            new_durations = {}
            for code, label in plan_labels_map.items():
                c1, c2, c3 = st.columns([3, 1, 1])
                c1.markdown(label)
                price = c2.number_input(
                    f"price_{code}", min_value=0.0, step=1.0,
                    value=float(settings["prices"].get(code, 0)),
                    key=f"price_{code}", label_visibility="collapsed"
                )
                duration = c3.number_input(
                    f"dur_{code}", min_value=1, step=1,
                    value=int(settings["durations_weeks"].get(code, 52)),
                    key=f"dur_{code}", label_visibility="collapsed"
                )
                new_prices[code] = price
                new_durations[code] = duration

            st.divider()
            st.subheader("Other Settings")
            o1, o2 = st.columns(2)
            grace = o1.number_input("Grace period (days)", min_value=0, step=1,
                                    value=int(settings.get("grace_period_days", 28)))
            rem_days = settings.get("reminder_days", [60, 30])
            r1, r2 = o2.columns(2)
            rem1 = r1.number_input("First reminder (days before expiry)", min_value=1, step=1,
                                   value=int(rem_days[0]))
            rem2 = r2.number_input("Second reminder (days before expiry)", min_value=1, step=1,
                                   value=int(rem_days[1]))

            if st.form_submit_button("💾 Save Settings", use_container_width=True):
                settings["prices"] = new_prices
                settings["durations_weeks"] = new_durations
                settings["grace_period_days"] = grace
                settings["reminder_days"] = [rem1, rem2]
                save_settings(settings)
                st.success("Settings saved.")

        st.divider()

        # ── Discount Codes ─────────────────────────────────────────────────────
        st.subheader("🏷️ Discount Codes")
        codes = db.query(DiscountCode).order_by(DiscountCode.id.desc()).all()
        if codes:
            code_rows = []
            for dc in codes:
                code_rows.append({
                    "Code": dc.code,
                    "Discount": f"{dc.discount_percent}%",
                    "Uses": f"{dc.use_count} / {'∞' if dc.max_uses is None else dc.max_uses}",
                    "Expires": dc.expires_at.strftime("%m/%d/%Y") if dc.expires_at else "—",
                    "Note": dc.note or "—",
                    "Active": "✅" if dc.active else "❌",
                })
            sel = st.dataframe(pd.DataFrame(code_rows), use_container_width=True, hide_index=True,
                               on_select="rerun", selection_mode="single-row", key="dc_table")
            selected_rows = sel.selection.rows if sel.selection else []
            if selected_rows:
                dc = codes[selected_rows[0]]
                col1, col2 = st.columns(2)
                if col1.button("Toggle Active", key="dc_toggle"):
                    dc.active = not dc.active
                    db.commit()
                    st.rerun()
                if col2.button("Delete Code", key="dc_delete"):
                    db.delete(dc)
                    db.commit()
                    st.rerun()
        else:
            st.caption("No discount codes yet.")

        with st.form("add_discount_code"):
            dc1, dc2, dc3 = st.columns([2, 1, 1])
            new_code    = dc1.text_input("Code").strip().upper()
            new_pct     = dc2.number_input("Discount %", min_value=1, max_value=100, value=10)
            new_maxuses = dc3.number_input("Max Uses (0=unlimited)", min_value=0, value=0)
            dc4, dc5    = st.columns([1, 2])
            new_expires = dc4.date_input("Expires (optional)", value=None)
            new_note    = dc5.text_input("Internal Note")
            if st.form_submit_button("Create Code"):
                if new_code:
                    from datetime import date as ddate
                    existing = db.query(DiscountCode).filter_by(code=new_code).first()
                    if existing:
                        st.error(f"Code {new_code} already exists.")
                    else:
                        dc_new = DiscountCode(
                            code=new_code,
                            discount_percent=int(new_pct),
                            max_uses=int(new_maxuses) if new_maxuses > 0 else None,
                            expires_at=new_expires if new_expires else None,
                            note=new_note or None,
                        )
                        db.add(dc_new)
                        db.commit()
                        st.success(f"Code {new_code} created.")
                        st.rerun()
                else:
                    st.error("Code is required.")

        st.divider()

        # ── Login Audit Log ────────────────────────────────────────────────────
        st.subheader("🔐 Admin Login Log")
        logs = db.query(AdminLoginLog).order_by(AdminLoginLog.event_at.desc()).limit(50).all()
        if logs:
            log_rows = []
            for l in logs:
                if l.success and l.logout_at and l.event_at:
                    delta = l.logout_at - l.event_at
                    mins = int(delta.total_seconds() // 60)
                    duration = f"{mins}m" if mins < 60 else f"{mins//60}h {mins%60}m"
                elif l.success and not l.logout_at:
                    duration = "Active"
                else:
                    duration = "—"
                log_rows.append({
                    "Time": l.event_at.strftime("%Y-%m-%d %H:%M") if l.event_at else "",
                    "Username": l.email or "",
                    "Result": "✅ Success" if l.success else "❌ Failed",
                    "Reason": l.reason or "",
                    "Browser": l.browser or "—",
                    "Session": duration,
                })
            st.dataframe(pd.DataFrame(log_rows), use_container_width=True, hide_index=True, height=280)
        else:
            st.caption("No login attempts recorded yet.")

db.close()
