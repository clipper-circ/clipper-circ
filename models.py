import enum
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, Date, DateTime,
    Enum, Numeric, ForeignKey, Text
)
from sqlalchemy.orm import relationship
from database import Base


class PlanCode(str, enum.Enum):
    LOCAL = "LOCAL"           # Duxbury/Plymouth County $50
    SENIOR = "SENIOR"         # Senior Citizen $40
    OUT_OF_COUNTY = "OUT_OF_COUNTY"  # Out-of-County $90
    SNOWBIRD = "SNOWBIRD"     # Senior Snowbird $55
    COMPLIMENTARY = "COMPLIMENTARY"  # Free
    GIFT = "GIFT"             # Gift subscription


class PostageCode(str, enum.Enum):
    P = "P"    # Periodical
    SC = "SC"  # Senior Citizen
    SB = "SB"  # Snowbird


class SubscriberStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"         # Current, delivering
    GRACE = "GRACE"           # Lapsed but still delivering (within grace window)
    ON_HOLD = "ON_HOLD"       # Temporarily paused by subscriber choice
    EXPIRED = "EXPIRED"       # Delivery stopped — past grace period
    CANCELLED = "CANCELLED"   # Permanently stopped

ACTIVE_STATUSES = {
    SubscriberStatus.ACTIVE,
    SubscriberStatus.GRACE,
    SubscriberStatus.ON_HOLD,
}


class PaymentMethod(str, enum.Enum):
    CREDIT_CARD = "CREDIT_CARD"
    PAYPAL = "PAYPAL"
    CHECK = "CHECK"
    COMPLIMENTARY = "COMPLIMENTARY"


PLAN_PRICES = {
    PlanCode.LOCAL: 50.00,
    PlanCode.SENIOR: 40.00,
    PlanCode.OUT_OF_COUNTY: 90.00,
    PlanCode.SNOWBIRD: 55.00,
    PlanCode.COMPLIMENTARY: 0.00,
    PlanCode.GIFT: 50.00,  # default, can vary
}

PLAN_LABELS = {
    PlanCode.LOCAL: "Duxbury/Plymouth County",
    PlanCode.SENIOR: "Senior Citizen",
    PlanCode.OUT_OF_COUNTY: "Out-of-County",
    PlanCode.SNOWBIRD: "Senior Snowbird (Half & Half)",
    PlanCode.COMPLIMENTARY: "Complimentary",
    PlanCode.GIFT: "Gift Subscription",
}

PLAN_DESCRIPTIONS = {
    PlanCode.LOCAL: "For a mailing address in Duxbury or anywhere in Plymouth County",
    PlanCode.OUT_OF_COUNTY: "For a mailing address outside of Plymouth County",
    PlanCode.SENIOR: "For a mailing address in Duxbury for a senior citizen",
    PlanCode.SNOWBIRD: "For a mailing address in Duxbury for a senior citizen who spends winter elsewhere",
}


class Subscriber(Base):
    __tablename__ = "subscribers"

    id = Column(Integer, primary_key=True)
    simplecirc_id = Column(String(20), unique=True, nullable=True)  # for migration

    # Name & contact
    full_name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=True)
    phone = Column(String(20), nullable=True)

    # Mailing address
    address1 = Column(String(200), nullable=False)
    address2 = Column(String(200), nullable=True)
    city = Column(String(100), nullable=False)
    state = Column(String(50), nullable=False)
    zipcode = Column(String(10), nullable=False)
    country = Column(String(50), default="UNITED STATES")

    # Subscription details
    plan = Column(Enum(PlanCode), nullable=False)
    postage_code = Column(Enum(PostageCode), nullable=True)
    status = Column(Enum(SubscriberStatus), default=SubscriberStatus.ACTIVE)
    start_date = Column(Date, nullable=True)
    expiration_date = Column(Date, nullable=True)
    copies = Column(Integer, default=1)

    # Billing
    payment_method = Column(Enum(PaymentMethod), default=PaymentMethod.CREDIT_CARD)
    auto_renew = Column(Boolean, default=True)
    stripe_customer_id = Column(String(50), nullable=True)
    stripe_subscription_id = Column(String(50), nullable=True)

    # Gift subscription fields
    is_gift = Column(Boolean, default=False)
    gift_giver_name = Column(String(200), nullable=True)
    gift_giver_email = Column(String(200), nullable=True)

    # Renewal reminder flags — set True once sent, reset to False on renewal
    reminder_35_sent     = Column(Boolean, default=False)  # 35 days before expiry (~5 issues)
    reminder_21_sent     = Column(Boolean, default=False)  # 21 days before expiry (~3 issues)
    reminder_14_sent     = Column(Boolean, default=False)  # 14 days before expiry (~2 issues)
    reminder_expire_sent = Column(Boolean, default=False)  # on expiry day
    grace_14_sent        = Column(Boolean, default=False)  # 14 days into grace period
    grace_final_sent     = Column(Boolean, default=False)  # 28 days into grace (final — paper stops)

    # Subscriber portal
    portal_password_hash = Column(String(200), nullable=True)
    portal_token          = Column(String(64),  nullable=True, index=True)
    portal_token_expires  = Column(DateTime,    nullable=True)

    # Alternate delivery address (e.g. summer/winter home)
    alt_address1      = Column(String(200), nullable=True)
    alt_address2      = Column(String(200), nullable=True)
    alt_city          = Column(String(100), nullable=True)
    alt_state         = Column(String(50),  nullable=True)
    alt_zipcode       = Column(String(10),  nullable=True)
    using_alt_address = Column(Boolean, default=False)

    backup_email              = Column(String(200), nullable=True)
    pending_email             = Column(String(200), nullable=True)
    pending_email_token       = Column(String(64),  nullable=True, index=True)
    pending_email_token_expires = Column(DateTime,  nullable=True)

    # Notes
    notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    holds = relationship("DeliveryHold", back_populates="subscriber", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="subscriber", cascade="all, delete-orphan")


class DeliveryHold(Base):
    __tablename__ = "delivery_holds"

    id = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    hold_start = Column(Date, nullable=False)
    hold_end = Column(Date, nullable=False)
    notes = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    subscriber = relationship("Subscriber", back_populates="holds")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, ForeignKey("subscribers.id"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    stripe_payment_intent_id = Column(String(100), nullable=True)
    check_number = Column(String(50), nullable=True)
    notes = Column(String(200), nullable=True)
    paid_at = Column(DateTime, default=datetime.utcnow)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    entered_by = Column(String(200), nullable=True)  # staff name who recorded it

    subscriber = relationship("Subscriber", back_populates="payments")


class PaymentAuditLog(Base):
    """Immutable record written whenever a payment is created, edited, or deleted.
    Not cascade-deleted with the subscriber — preserved indefinitely."""
    __tablename__ = "payment_audit_log"

    id = Column(Integer, primary_key=True)
    event_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    action = Column(String(20), nullable=False)        # CREATED / EDITED / DELETED
    payment_id = Column(Integer, nullable=True)        # original payment row id (nullable after delete)
    subscriber_id = Column(Integer, nullable=False)    # kept even after subscriber deleted
    subscriber_name = Column(String(200), nullable=True)
    amount = Column(Numeric(10, 2), nullable=True)
    payment_method = Column(String(50), nullable=True)
    check_number = Column(String(50), nullable=True)
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    entered_by = Column(String(200), nullable=True)    # staff name


class HoldAuditLog(Base):
    """Immutable record written whenever a delivery hold is added or removed."""
    __tablename__ = "hold_audit_log"

    id = Column(Integer, primary_key=True)
    event_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    action = Column(String(20), nullable=False)        # ADDED / REMOVED
    hold_id = Column(Integer, nullable=True)
    subscriber_id = Column(Integer, nullable=False)
    subscriber_name = Column(String(200), nullable=True)
    hold_start = Column(Date, nullable=True)
    hold_end = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    entered_by = Column(String(200), nullable=True)


class SubscriberEventLog(Base):
    """Audit trail for non-payment subscriber events."""
    __tablename__ = "subscriber_event_log"

    id            = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, nullable=False)
    event_at      = Column(DateTime, default=datetime.utcnow, nullable=False)
    event_type    = Column(String(50), nullable=False)
    description   = Column(Text, nullable=True)
    performed_by  = Column(String(200), nullable=True)


class AdminLoginLog(Base):
    """Audit log for admin login attempts."""
    __tablename__ = "admin_login_log"

    id             = Column(Integer, primary_key=True)
    event_at       = Column(DateTime, default=datetime.utcnow, nullable=False)
    logout_at      = Column(DateTime, nullable=True)
    email          = Column(String(200), nullable=True)
    success        = Column(Boolean, nullable=False)
    reason         = Column(String(100), nullable=True)
    ip_address     = Column(String(50), nullable=True)
    browser        = Column(String(200), nullable=True)


class StaffUser(Base):
    __tablename__ = "staff_users"

    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False)
    name = Column(String(200), nullable=False)
    password_hash = Column(String(200), nullable=False)
    is_admin = Column(Boolean, default=False)  # admins can manage other staff
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ObituarySubmission(Base):
    __tablename__ = "obituary_submissions"

    id                    = Column(Integer, primary_key=True)
    submitted_at          = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Deceased info
    deceased_name         = Column(String(200), nullable=False)
    age                   = Column(String(10),  nullable=True)
    date_of_death         = Column(String(50),  nullable=True)
    obit_text             = Column(Text,         nullable=False)
    word_count            = Column(Integer,      nullable=True)

    # Submitter info
    submitter_first_name  = Column(String(100),  nullable=True)
    submitter_last_name   = Column(String(100),  nullable=True)
    submitter_email       = Column(String(200),  nullable=True)
    submitter_phone       = Column(String(30),   nullable=True)
    relation              = Column(String(200),  nullable=True)

    # Publication
    pub_timing            = Column(String(200),  nullable=True)
    photo_submitted       = Column(Boolean,      default=False)

    # Payment
    amount_paid           = Column(Numeric(10,2), nullable=True)
    card_description      = Column(String(100),  nullable=True)
    stripe_pi_id          = Column(String(100),  nullable=True)

    # Metadata
    ip_address            = Column(String(50),   nullable=True)
    user_agent            = Column(Text,          nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key   = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    id               = Column(Integer, primary_key=True)
    code             = Column(String(50), unique=True, nullable=False)
    discount_percent = Column(Integer, nullable=False)          # e.g. 10 = 10% off
    active           = Column(Boolean, default=True, nullable=False)
    expires_at       = Column(Date, nullable=True)             # None = no expiry
    max_uses         = Column(Integer, nullable=True)          # None = unlimited
    use_count        = Column(Integer, default=0, nullable=False)
    note             = Column(String(200), nullable=True)      # internal label
    created_at       = Column(DateTime, default=datetime.utcnow)
