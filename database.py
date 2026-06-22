"""
database.py
===========
SQLite database setup for the Smart Hospital Queue System.
Defines all tables, seed data, and a get_db() connection helper.

Tables
------
  departments   — Hospital departments (beds, capacity, priority rule)
  doctors       — Doctor records linked to departments
  patients      — All registered patients
  appointments  — Appointment/visit records (one per visit)
  queue_tokens  — Live queue entries for today
  alerts        — System-generated overload / drift alerts
  realloc_log   — Staff reallocation action log
  prediction_log— Every ML prediction made (for model monitoring)
  settings      — Key-value system configuration store
"""

import sqlite3
import os
from datetime import datetime, date
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hospital.db")


# -----------------------------------------------------------------------
# CONNECTION HELPER
# -----------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """
    Return a new SQLite connection with row_factory set to Row
    so columns are accessible by name.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # allows concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row) if row else None


def rows_to_list(rows) -> list:
    """Convert a list of sqlite3.Row objects to a list of dicts."""
    return [dict(r) for r in rows]


# -----------------------------------------------------------------------
# SCHEMA
# -----------------------------------------------------------------------

SCHEMA = """
-- ── DEPARTMENTS ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS departments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    emoji         TEXT    DEFAULT '🏥',
    floor_location TEXT   DEFAULT '',
    beds          INTEGER DEFAULT 20,
    max_capacity  INTEGER DEFAULT 25,
    doctor_count  INTEGER DEFAULT 0,
    nurse_count   INTEGER DEFAULT 0,
    priority_rule TEXT    DEFAULT 'FIFO',
    color_hex     TEXT    DEFAULT '#f0fdf4',
    is_active     INTEGER DEFAULT 1,
    created_at    TEXT    DEFAULT (datetime('now')),
    updated_at    TEXT    DEFAULT (datetime('now'))
);

-- ── DOCTORS ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS doctors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doctor_code     TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    specialization  TEXT    NOT NULL,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    shift           TEXT    DEFAULT 'Morning' CHECK(shift IN ('Morning','Evening','Night')),
    status          TEXT    DEFAULT 'On Duty' CHECK(status IN ('On Duty','Break','Off Duty')),
    patients_today  INTEGER DEFAULT 0,
    phone           TEXT    DEFAULT '',
    email           TEXT    DEFAULT '',
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

-- ── PATIENTS ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_code    TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    age             INTEGER DEFAULT 0,
    gender          TEXT    DEFAULT 'Other' CHECK(gender IN ('Male','Female','Other')),
    phone           TEXT    DEFAULT '',
    email           TEXT    DEFAULT '',
    blood_group     TEXT    DEFAULT '',
    address         TEXT    DEFAULT '',
    notes           TEXT    DEFAULT '',
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

-- ── APPOINTMENTS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    doctor_id       INTEGER REFERENCES doctors(id) ON DELETE SET NULL,
    visit_date      TEXT    NOT NULL,
    visit_time      TEXT    DEFAULT '',
    urgency         TEXT    DEFAULT 'Medium' CHECK(urgency IN ('Low','Medium','High')),
    status          TEXT    DEFAULT 'Scheduled'
                            CHECK(status IN ('Scheduled','Active','Completed','Missed','Cancelled','Transferred')),
    symptoms        TEXT    DEFAULT '',
    diagnosis       TEXT    DEFAULT '',
    check_in_time   TEXT    DEFAULT '',
    check_out_time  TEXT    DEFAULT '',
    wait_time_min   INTEGER DEFAULT 0,
    created_by      TEXT    DEFAULT 'admin',
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

-- ── QUEUE TOKENS ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS queue_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_number    TEXT    NOT NULL,
    appointment_id  INTEGER REFERENCES appointments(id) ON DELETE CASCADE,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE CASCADE,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    queue_date      TEXT    NOT NULL DEFAULT (date('now')),
    position        INTEGER DEFAULT 0,
    est_wait_min    INTEGER DEFAULT 0,
    status          TEXT    DEFAULT 'Waiting'
                            CHECK(status IN ('Waiting','Serving','Completed','Skipped','Transferred','Cancelled')),
    called_at       TEXT    DEFAULT '',
    completed_at    TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── ALERTS ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    alert_type      TEXT    DEFAULT 'overload'
                            CHECK(alert_type IN ('overload','understaffed','peak_hour','model_drift','custom')),
    level           TEXT    DEFAULT 'Medium' CHECK(level IN ('Low','Medium','High','Critical')),
    message         TEXT    NOT NULL,
    is_active       INTEGER DEFAULT 1,
    is_acknowledged INTEGER DEFAULT 0,
    acknowledged_by TEXT    DEFAULT '',
    acknowledged_at TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── REALLOCATION LOG ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS realloc_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT    NOT NULL,
    staff_type      TEXT    DEFAULT 'Doctor' CHECK(staff_type IN ('Doctor','Nurse')),
    from_dept_id    INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    to_dept_id      INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    reason          TEXT    DEFAULT '',
    approved_by     TEXT    DEFAULT 'admin',
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── PREDICTION LOG ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prediction_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id  INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
    department_id   INTEGER REFERENCES departments(id) ON DELETE SET NULL,
    predicted_wait  REAL    NOT NULL,
    actual_wait     REAL    DEFAULT NULL,
    error_min       REAL    DEFAULT NULL,
    urgency_score   INTEGER DEFAULT 2,
    nurse_ratio     REAL    DEFAULT 3.0,
    facility_size   INTEGER DEFAULT 200,
    is_peak         INTEGER DEFAULT 0,
    load_status     TEXT    DEFAULT 'Medium',
    model_version   TEXT    DEFAULT 'xgboost_v1',
    created_at      TEXT    DEFAULT (datetime('now'))
);

-- ── SETTINGS ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK(role IN ('admin','staff','doctor','viewer')),
    display_name    TEXT    DEFAULT '',
    email           TEXT    DEFAULT '',
    phone           TEXT    DEFAULT '',
    designation     TEXT    DEFAULT '',
    email_verified  INTEGER DEFAULT 0,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token           TEXT    NOT NULL UNIQUE,
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    DEFAULT (datetime('now')),
    last_seen_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username        TEXT    DEFAULT '',
    role            TEXT    DEFAULT '',
    action          TEXT    NOT NULL,
    entity_type     TEXT    DEFAULT '',
    entity_id       TEXT    DEFAULT '',
    details_json    TEXT    DEFAULT '{}',
    ip_address      TEXT    DEFAULT '',
    created_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    patient_id      INTEGER REFERENCES patients(id) ON DELETE SET NULL,
    appointment_id  INTEGER REFERENCES appointments(id) ON DELETE SET NULL,
    category        TEXT    NOT NULL DEFAULT 'feedback'
                            CHECK(category IN ('feedback','rating','support')),
    subject         TEXT    DEFAULT '',
    message         TEXT    NOT NULL,
    rating          INTEGER DEFAULT NULL,
    status          TEXT    DEFAULT 'Open',
    created_at      TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL,
    description     TEXT    DEFAULT '',
    updated_at      TEXT    DEFAULT (datetime('now'))
);

-- ── INDEXES ────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_appointments_patient   ON appointments(patient_id);
CREATE INDEX IF NOT EXISTS idx_appointments_dept      ON appointments(department_id);
CREATE INDEX IF NOT EXISTS idx_appointments_date      ON appointments(visit_date);
CREATE INDEX IF NOT EXISTS idx_appointments_status    ON appointments(status);
CREATE INDEX IF NOT EXISTS idx_queue_date             ON queue_tokens(queue_date);
CREATE INDEX IF NOT EXISTS idx_queue_status           ON queue_tokens(status);
CREATE INDEX IF NOT EXISTS idx_queue_dept             ON queue_tokens(department_id);
CREATE INDEX IF NOT EXISTS idx_alerts_active          ON alerts(is_active);
CREATE INDEX IF NOT EXISTS idx_prediction_created     ON prediction_log(created_at);
CREATE INDEX IF NOT EXISTS idx_doctors_dept           ON doctors(department_id);
CREATE INDEX IF NOT EXISTS idx_users_username         ON users(username);
CREATE INDEX IF NOT EXISTS idx_sessions_token         ON user_sessions(token);
CREATE INDEX IF NOT EXISTS idx_audit_created          ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity           ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user          ON feedback_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_feedback_patient       ON feedback_entries(patient_id);
"""


# -----------------------------------------------------------------------
# SEED DATA
# -----------------------------------------------------------------------

SEED_DEPARTMENTS = [
    ("Cardiology",       "❤️",  "Floor 3, Block A", 45, 50, 6, 12, "Emergency first",  "#fef2f2"),
    ("Orthopedics",      "🦴",  "Floor 2, Block B", 30, 35, 4,  8, "FIFO",             "#eff6ff"),
    ("General Medicine", "💊",  "Floor 1, Block A", 60, 70, 8, 16, "Urgency-based",    "#f0fdf4"),
    ("Pediatrics",       "👶",  "Floor 2, Block C", 25, 30, 3,  9, "FIFO",             "#fff7ed"),
    ("Dermatology",      "🔬",  "Floor 1, Block D", 20, 25, 5,  7, "FIFO",             "#f5f3ff"),
    ("Neurology",        "🧠",  "Floor 4, Block A", 35, 40, 4, 10, "Urgency-based",    "#ecfdf5"),
]

SEED_DOCTORS = [
    ("DR001", "Dr. Suresh Mehta",  "Cardiologist",       1, "Morning", "On Duty"),
    ("DR002", "Dr. Anita Sharma",  "Orthopedic Surgeon", 2, "Morning", "On Duty"),
    ("DR003", "Dr. Ravi Gupta",    "General Physician",  3, "Evening", "Break"),
    ("DR004", "Dr. Kavita Patel",  "Pediatrician",       4, "Morning", "On Duty"),
    ("DR005", "Dr. Arjun Singh",   "Dermatologist",      5, "Evening", "Off Duty"),
    ("DR006", "Dr. Priya Nair",    "Neurologist",        6, "Night",   "On Duty"),
    ("DR007", "Dr. Anil Verma",    "Cardiologist",       1, "Evening", "On Duty"),
    ("DR008", "Dr. Meena Reddy",   "General Physician",  3, "Morning", "On Duty"),
]

SEED_PATIENTS = [
    ("P001", "Rahul Sharma",  45, "Male",   "9876543210"),
    ("P002", "Priya Patel",   32, "Female", "9867432101"),
    ("P003", "Amit Desai",    58, "Male",   "9856321012"),
    ("P004", "Sunita Rao",    28, "Female", "9845210123"),
    ("P005", "Vikram Singh",  41, "Male",   "9834101234"),
    ("P006", "Meena Joshi",   67, "Female", "9823012345"),
    ("P007", "Ravi Kumar",    52, "Male",   "9812123456"),
    ("P008", "Sita Devi",     38, "Female", "9801234567"),
    ("P009", "Arjun Kumar",   25, "Male",   "9790123456"),
    ("P010", "Lakshmi Bai",   71, "Female", "9779012345"),
    ("P011", "Mohan Das",     44, "Male",   "9768901234"),
    ("P012", "Nandini Roy",   29, "Female", "9757890123"),
]

SEED_APPOINTMENTS = [
    # (patient_id, dept_id, doctor_id, visit_date, urgency, status, symptoms)
    (1, 1, 1, date.today().isoformat(), "High",   "Active",    "Chest pain"),
    (2, 2, 2, date.today().isoformat(), "Medium", "Active",    "Knee injury"),
    (3, 3, 3, date.today().isoformat(), "High",   "Active",    "Fever, fatigue"),
    (4, 4, 4, date.today().isoformat(), "Low",    "Active",    "Child vaccination"),
    (5, 5, 5, date.today().isoformat(), "Low",    "Completed", "Skin rash"),
    (6, 6, 6, date.today().isoformat(), "Medium", "Active",    "Headaches"),
    (7, 1, 7, date.today().isoformat(), "High",   "Missed",    "Palpitations"),
    (8, 2, 2, (date.today().isoformat()), "Medium","Completed","Back pain"),
    (9, 3, 8, (date.today().isoformat()), "Low",   "Cancelled", "Routine checkup"),
    (10,6, 6, (date.today().isoformat()), "High",  "Completed","Seizure history"),
    (11,1, 1, date.today().isoformat(), "Medium", "Active",    "Hypertension"),
    (12,4, 4, date.today().isoformat(), "Low",    "Active",    "Cough and cold"),
]

SEED_QUEUE_TOKENS = [
    # (token, appt_id, patient_id, dept_id, position, est_wait, status)
    ("A01", 1,  1,  1, 1, 18, "Serving"),
    ("B03", 2,  2,  2, 2, 22, "Waiting"),
    ("C07", 3,  3,  3, 3, 30, "Waiting"),
    ("D02", 4,  4,  4, 4, 12, "Waiting"),
    ("F09", 6,  6,  6, 5, 26, "Waiting"),
    ("A03", 11, 11, 1, 6, 35, "Waiting"),
    ("D04", 12, 12, 4, 7, 20, "Waiting"),
]

SEED_SETTINGS = [
    ("peak_hour_from",      "17",    "Peak hours start (24h)"),
    ("peak_hour_to",        "22",    "Peak hours end (24h)"),
    ("threshold_high",      "20",    "Patient count that triggers High load alert"),
    ("threshold_medium",    "12",    "Patient count that triggers Medium load alert"),
    ("pred_cap_minutes",    "120",   "Max displayed predicted wait time"),
    ("drift_mae_threshold", "6",     "MAE threshold for model drift warning"),
    ("notify_overload",     "true",  "Enable overload alerts"),
    ("notify_realloc",      "true",  "Enable reallocation suggestions"),
    ("notify_daily",        "false", "Enable daily summary notifications"),
    ("notify_drift",        "true",  "Enable model drift warnings"),
    ("notify_peak_pre",     "false", "Alert 1h before peak hours"),
    ("admin_name",          "Hospital Admin",          "Admin display name"),
    ("admin_email",         "admin@smarthospital.in",  "Admin email"),
    ("hospital_name",       "Smart Hospital",          "Hospital name"),
]

SEED_USERS = [
    ("admin", "1234", "admin", "Hospital Admin", "admin@smarthospital.in"),
    ("staff", "1234", "staff", "Front Desk Staff", "staff@smarthospital.in"),
]



# -----------------------------------------------------------------------
# INIT DATABASE
# -----------------------------------------------------------------------

def init_db():
    """
    Create all tables and keep the database empty except for:
      - users (admin/staff login accounts if missing)
      - settings (system defaults if missing)

    No demo departments, doctors, patients, appointments, or queue rows
    are inserted anymore.
    """
    conn = get_db()
    cur = conn.cursor()

    # Create tables
    cur.executescript(SCHEMA)

    # ── Live migrations (idempotent ALTER TABLE) ───────────────────────
    try:
        cur.execute("ALTER TABLE queue_tokens ADD COLUMN ml_predicted_wait REAL DEFAULT 0")
        conn.commit()
        print("[DB] ✓ Migrated: queue_tokens.ml_predicted_wait added")
    except Exception:
        pass  # column already exists

    try:
        cur.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")
        conn.commit()
        print("[DB] users.phone added")
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE users ADD COLUMN designation TEXT DEFAULT ''")
        conn.commit()
        print("[DB] users.designation added")
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        conn.commit()
        print("[DB] users.email_verified added")
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE users ADD COLUMN patient_id INTEGER REFERENCES patients(id) ON DELETE SET NULL")
        conn.commit()
        print("[DB] users.patient_id added")
    except Exception:
        pass

    # Always sync operational_date to real calendar today on every startup.
    # This prevents stale demo/test dates from causing old queue data to show
    # on the dashboard instead of today's real data.
    today = date.today().isoformat()
    cur.execute(
        """
        INSERT INTO settings (key, value, description)
        VALUES ('operational_date', ?, 'Operational reporting date (always today)')
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = datetime('now')
        """,
        (today,),
    )

    # Ensure default settings exist (but do not seed demo data)
    default_settings = [
        ("peak_hour_from",      "17",    "Peak hours start (24h)"),
        ("peak_hour_to",        "22",    "Peak hours end (24h)"),
        ("threshold_high",      "20",    "Patient count that triggers High load alert"),
        ("threshold_medium",    "12",    "Patient count that triggers Medium load alert"),
        ("pred_cap_minutes",    "120",   "Max displayed predicted wait time"),
        ("drift_mae_threshold", "6",     "MAE threshold for model drift warning"),
        ("notify_overload",     "true",  "Enable overload alerts"),
        ("notify_realloc",      "true",  "Enable reallocation suggestions"),
        ("notify_daily",        "false", "Enable daily summary notifications"),
        ("notify_drift",        "true",  "Enable model drift warnings"),
        ("notify_peak_pre",     "false", "Alert 1h before peak hours"),
        ("admin_name",          "Hospital Admin",         "Admin display name"),
        ("admin_email",         "admin@smarthospital.in", "Admin email"),
        ("hospital_name",       "Smart Hospital",         "Hospital name"),
    ]
    for key, value, desc in default_settings:
        cur.execute(
            """
            INSERT INTO settings (key, value, description)
            VALUES (?,?,?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, value, desc),
        )

    # Ensure at least one admin account exists so the app can be used.
    # If you already have users, this does nothing.
    if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        cur.executemany(
            """INSERT INTO users (username, password_hash, role, display_name, email)
               VALUES (?,?,?,?,?)""",
            [(u, generate_password_hash(pw), r, name, email) for u, pw, r, name, email in SEED_USERS]
        )
        print("[DB] Users seeded")

    conn.commit()
    conn.close()
    print(f"[DB] Database ready at: {DB_PATH}")


def reset_operational_data(keep_users: bool = True):
    """
    Hard-reset the hospital database operational tables so you can enter
    all doctors, departments, patients, appointments, and queue records manually.

    By default, users/settings are preserved to keep login working.
    """
    conn = get_db()
    try:
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

        # Reset autoincrement counters
        conn.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ('departments','doctors','patients','appointments','queue_tokens','alerts','realloc_log','prediction_log','audit_log','user_sessions')"
        )

        # Make sure the operational date is today
        today = date.today().isoformat()
        conn.execute(
            """
            INSERT INTO settings (key, value, description)
            VALUES ('operational_date', ?, 'Operational reporting date')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')
            """,
            (today,),
        )

        if not keep_users:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('users')")

        conn.commit()
    finally:
        conn.close()

    print("[DB] Operational data cleared.")


if __name__ == "__main__":
    init_db()