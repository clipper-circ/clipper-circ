"""
Weekly subscriber backup — emails a CSV to the admin.
Run every Sunday via Railway cron or manually.
"""
import os, sys, csv, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from database import SessionLocal
from models import Subscriber
import resend
from datetime import date

resend.api_key = os.environ["RESEND_API_KEY"]
FROM_EMAIL     = os.environ.get("FROM_EMAIL", "subscribe@duxburyclipper.com")
BACKUP_EMAIL   = os.environ.get("STAFF_ALERT_EMAIL", "subscribe@duxburyclipper.com")

db = SessionLocal()
subscribers = db.query(Subscriber).order_by(Subscriber.id).all()
db.close()

buf = io.StringIO()
writer = csv.writer(buf)
writer.writerow([
    "ID", "Name", "Email", "Phone", "Address1", "Address2",
    "City", "State", "Zip", "Plan", "Status",
    "Expiration", "Auto-Renew", "SimpleCirc ID", "Notes"
])
for s in subscribers:
    writer.writerow([
        s.id, s.full_name, s.email or "", s.phone or "",
        s.address1, s.address2 or "", s.city, s.state, s.zipcode,
        s.plan.value if s.plan else "", s.status.value if s.status else "",
        s.expiration_date.isoformat() if s.expiration_date else "",
        "Yes" if s.auto_renew else "No",
        s.simplecirc_id or "", s.notes or "",
    ])

csv_bytes = buf.getvalue().encode("utf-8")
today = date.today().isoformat()

resend.Emails.send({
    "from": FROM_EMAIL,
    "to": BACKUP_EMAIL,
    "subject": f"Clipper Subscriber Backup — {today}",
    "html": f"<p>Weekly subscriber backup attached. {len(subscribers)} subscribers as of {today}.</p>",
    "attachments": [{
        "filename": f"clipper_subscribers_{today}.csv",
        "content": list(csv_bytes),
    }],
})

print(f"Backup sent: {len(subscribers)} subscribers → {BACKUP_EMAIL}")
