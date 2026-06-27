"""
Import subscribers from a SimpleCirc mailing label CSV export.
Usage: python importer.py path/to/export.csv
"""
import sys
import csv
from datetime import datetime, date
from database import SessionLocal, engine
from models import Base, Subscriber, PlanCode, PostageCode, SubscriberStatus, PaymentMethod

POSTAGE_TO_PLAN = {
    "P": PlanCode.LOCAL,
    "SC": PlanCode.SENIOR,
    "SB": PlanCode.SNOWBIRD,
}

POSTAGE_CODE_MAP = {
    "P": PostageCode.P,
    "SC": PostageCode.SC,
    "SB": PostageCode.SB,
}


def parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def import_csv(filepath):
    Base.metadata.create_all(engine)
    db = SessionLocal()

    added = 0
    skipped = 0
    updated = 0

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            simplecirc_id = row.get("Subscriber Account ID", "").strip()
            if not simplecirc_id:
                skipped += 1
                continue

            postage_raw = row.get("Postage Type Code", "P").strip().upper()
            plan = POSTAGE_TO_PLAN.get(postage_raw, PlanCode.LOCAL)
            postage_code = POSTAGE_CODE_MAP.get(postage_raw, PostageCode.P)
            expiration = parse_date(row.get("Subscription Expiration Date", ""))

            # Determine status
            if expiration and expiration < date.today():
                status = SubscriberStatus.EXPIRED
            else:
                status = SubscriberStatus.ACTIVE

            existing = db.query(Subscriber).filter_by(simplecirc_id=simplecirc_id).first()

            if existing:
                # Update address and expiration in case it changed
                existing.expiration_date = expiration
                existing.status = status
                updated += 1
            else:
                sub = Subscriber(
                    simplecirc_id=simplecirc_id,
                    full_name=row.get("Subscriber Full Name", "").strip(),
                    address1=row.get("Subscriber Address 1", "").strip(),
                    address2=row.get("Subscriber Address 2", "").strip() or None,
                    city=row.get("Subscriber City", "").strip(),
                    state=row.get("Subscriber State", "").strip(),
                    zipcode=row.get("Subscriber Zipcode", "").strip(),
                    country=row.get("Subscriber Country", "UNITED STATES").strip(),
                    plan=plan,
                    postage_code=postage_code,
                    status=status,
                    expiration_date=expiration,
                    copies=int(row.get("Subscription Copies", 1) or 1),
                    # Default check — will be updated when subscriber creates portal account
                    payment_method=PaymentMethod.CHECK,
                    auto_renew=False,  # unknown at import time, set when they go online
                )
                db.add(sub)
                added += 1

    db.commit()
    db.close()
    print(f"Import complete: {added} added, {updated} updated, {skipped} skipped")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python importer.py path/to/export.csv")
        sys.exit(1)
    import_csv(sys.argv[1])
