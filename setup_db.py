"""
Run once to create all database tables and the first admin user.
Usage: python setup_db.py
"""
from database import engine
from models import Base, StaffUser
from database import SessionLocal
import bcrypt

Base.metadata.create_all(engine)
print("Database tables created.")

db = SessionLocal()
existing = db.query(StaffUser).filter_by(email="subscribe@duxburyclipper.com").first()
if not existing:
    pw = input("Create admin password: ")
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    admin = StaffUser(
        name="Admin",
        email="subscribe@duxburyclipper.com",
        password_hash=pw_hash,
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    print("Admin user created: subscribe@duxburyclipper.com")
else:
    print("Admin user already exists.")
db.close()
