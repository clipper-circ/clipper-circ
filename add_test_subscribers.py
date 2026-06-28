"""
One-time script to add two test subscribers.
Run via Railway console: python add_test_subscribers.py
"""
from database import SessionLocal
from models import Subscriber, SubscriberStatus, PlanCode, PaymentMethod
from datetime import date

db = SessionLocal()

subs = [
    Subscriber(
        account_number="10001",
        full_name="John Smith",
        email="josh@joshcutler.com",
        address1="10 Elm Street",
        city="Duxbury",
        state="MA",
        zipcode="02332",
        plan=PlanCode.LOCAL,
        status=SubscriberStatus.ACTIVE,
        payment_method=PaymentMethod.CHECK,
        auto_renew=False,
        expiration_date=date(2027, 1, 1),
    ),
    Subscriber(
        account_number="10002",
        full_name="Jane Doe",
        email="jsum2271@gmail.com",
        address1="25 Harbor Road",
        city="Duxbury",
        state="MA",
        zipcode="02332",
        plan=PlanCode.LOCAL,
        status=SubscriberStatus.ACTIVE,
        payment_method=PaymentMethod.CHECK,
        auto_renew=False,
        expiration_date=date(2027, 1, 1),
    ),
]

for s in subs:
    existing = db.query(Subscriber).filter_by(account_number=s.account_number).first()
    if not existing:
        db.add(s)
        print(f"Added: {s.full_name}")
    else:
        print(f"Already exists: {s.full_name}")

db.commit()
db.close()
print("Done.")
