"""Add alt_full_name column to subscribers table."""
from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("""
        ALTER TABLE subscribers
        ADD COLUMN IF NOT EXISTS alt_full_name VARCHAR(200)
    """))
    conn.commit()
    print("Done: alt_full_name column added.")
