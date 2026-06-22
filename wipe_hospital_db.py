#!/usr/bin/env python3
from pathlib import Path
from datetime import date
import sqlite3
import shutil
import os

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "hospital.db"

def main():
    if not DB_PATH.exists():
        print(f"[wipe] DB not found: {DB_PATH}")
        return

    backup = BASE_DIR / f"hospital_backup_{date.today().isoformat()}.db"
    shutil.copy2(DB_PATH, backup)
    print(f"[wipe] Backup created: {backup.name}")

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        tables = [
            "queue_tokens",
            "appointments",
            "alerts",
            "realloc_log",
            "prediction_log",
            "audit_log",
            "doctors",
            "patients",
            "departments",
            "user_sessions",
        ]
        for table in tables:
            conn.execute(f"DELETE FROM {table}")

        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('departments','doctors','patients','appointments','queue_tokens','alerts','realloc_log','prediction_log','audit_log','user_sessions')"
        )

        today = date.today().isoformat()
        conn.execute(
            """
            INSERT INTO settings (key, value, description)
            VALUES ('operational_date', ?, 'Operational reporting date')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
            """,
            (today,),
        )
        conn.commit()
        print("[wipe] Operational tables cleared successfully.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
