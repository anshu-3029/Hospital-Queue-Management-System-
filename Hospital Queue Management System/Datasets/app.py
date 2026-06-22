from flask import Flask, request, send_from_directory, Response, g
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from datetime import datetime, date, timedelta
import csv
import io
import json
import os
import secrets
import joblib
import pandas as pd

import os
print("DB PATH:", os.path.abspath("hospital.db"))

# Load trained ML model (graceful fallback if model files are missing)
try:
    wait_model = joblib.load("final_xgboost_balanced.pkl")
    model_columns = joblib.load("model_columns.pkl")
    print("ML model loaded OK")
except Exception as _ml_err:
    print(f"WARNING: ML model not loaded ({_ml_err}). Wait-time prediction will return a default value.")
    wait_model = None
    model_columns = []

def prepare_features(data):
    now = datetime.now()

    hour = now.hour
    day = now.weekday()
    month = now.month

    urgency_map = {"Low": 1, "Medium": 2, "High": 3}
    urgency_score = urgency_map.get(data.get("urgency", "Medium"), 2)

    nurse_ratio = float(data.get("nurse_ratio", 5))
    facility_size = float(data.get("facility_size", 100))

    load_index = facility_size / (nurse_ratio + 1)
    is_peak = 1 if 17 <= hour <= 22 else 0

    df = pd.DataFrame([{
        "hour": hour,
        "day": day,
        "month": month,
        "Urgency Score": urgency_score,
        "Nurse-to-Patient Ratio": nurse_ratio,
        "Facility Size (Beds)": facility_size,
        "Load_Index": load_index,
        "is_peak": is_peak
    }])

    return df

def align_features(df):
    for col in model_columns:
        if col not in df:
            df[col] = 0
    return df[model_columns]


from database import get_db, init_db, row_to_dict, rows_to_list
from api_helpers import (
    ok, created, not_found, bad_request, server_error, conflict,
    unauthorized, forbidden, paginate, require_fields, validate_phone,
    validate_date, coerce_int, coerce_float, generate_token,
    next_patient_code, next_doctor_code, URGENCY_VALUES, STATUS_APPT,
    STATUS_QUEUE, STATUS_DOCTOR, SHIFT_VALUES, GENDER_VALUES,
    ALERT_TYPES, ALERT_LEVELS,
)

# Average service time per patient in minutes (configurable)
AVG_SERVICE_TIME_MIN = 8


def _time_to_minutes(value) -> int:
    """Convert a HH:MM[:SS] time string to minutes for FIFO sorting."""
    if value in (None, ""):
        return 24 * 60 + 59
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.hour * 60 + parsed.minute
        except Exception:
            continue
    return 24 * 60 + 59



def _shift_window_from_text(shift_text):
    text = str(shift_text or "").strip().lower()

    if "morning" in text:
        return {
            "label": "Morning Shift",
            "start_min": 8 * 60,
            "end_min": 14 * 60,
            "start": "08:00",
            "end": "14:00",
            "display": "08:00 AM to 02:00 PM",
        }

    if "evening" in text:
        return {
            "label": "Evening Shift",
            "start_min": 14 * 60,
            "end_min": 20 * 60,
            "start": "14:00",
            "end": "20:00",
            "display": "02:00 PM to 08:00 PM",
        }

    if "night" in text:
        return {
            "label": "Night Shift",
            "start_min": 20 * 60,
            "end_min": 8 * 60,
            "start": "20:00",
            "end": "08:00",
            "display": "08:00 PM to 08:00 AM",
        }

    return None


def _doctor_shift_window(conn, doctor_id):
    doctor_id = coerce_int(doctor_id, 0) or None
    if not doctor_id:
        return None
    row = conn.execute("SELECT id, shift FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not row:
        return None
    return _shift_window_from_text(row["shift"])


def _visit_time_within_shift(visit_time, shift):
    if not shift:
        return True
    if not visit_time:
        return True
    mins = _time_to_minutes(visit_time)
    if shift["start_min"] <= shift["end_min"]:
        return shift["start_min"] <= mins <= shift["end_min"]
    return mins >= shift["start_min"] or mins <= shift["end_min"]


def _normalize_visit_time_for_shift(visit_time, shift):
    if not shift:
        return str(visit_time or "").strip()
    cleaned = str(visit_time or "").strip()
    if not cleaned:
        return shift["start"]
    if _visit_time_within_shift(cleaned, shift):
        return cleaned
    return None


def _appointment_priority_key(appt_row, token_row=None):

    """
    Sort by appointment time first, then FIFO by appointment id.
    If a visit time is missing, push it to the end of the queue.
    """
    appt_row = appt_row or {}
    token_row = token_row or {}
    visit_date = str(appt_row.get("visit_date") or "").strip() or date.today().isoformat()
    visit_time = appt_row.get("visit_time")
    appointment_id = coerce_int(appt_row.get("id"), 0)
    token_id = coerce_int(token_row.get("id"), 0)
    return (
        visit_date,
        _time_to_minutes(visit_time),
        appointment_id,
        token_id,
    )


def _appointment_doctor_id(conn, appointment_id):
    if not appointment_id:
        return None
    row = conn.execute("SELECT doctor_id FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not row:
        return None
    return coerce_int(row["doctor_id"], 0) or None


def recalculate_queue_positions(conn, department_id, queue_date, doctor_id=None):
    """
    Recalculate queue positions and wait times for all waiting patients in a scope.
    The scope is either a specific doctor's queue inside a department or the
    department-wide queue when no doctor is assigned.

    Ordering logic:
      1) appointment visit time
      2) FIFO using appointment id when times match
    """
    doctor_id = coerce_int(doctor_id, 0) or None

    # First, set wait time to 0 for any "Serving" patients in the same scope.
    if doctor_id is None:
        conn.execute(
            """UPDATE queue_tokens
               SET est_wait_min=0
               WHERE department_id=? AND queue_date=? AND status='Serving'
                 AND id IN (
                     SELECT qt2.id
                     FROM queue_tokens qt2
                     LEFT JOIN appointments a ON a.id = qt2.appointment_id
                     WHERE qt2.department_id=? AND qt2.queue_date=?
                       AND COALESCE(a.doctor_id, 0) = 0
                 )""",
            (department_id, queue_date, department_id, queue_date),
        )
    else:
        conn.execute(
            """UPDATE queue_tokens
               SET est_wait_min=0
               WHERE department_id=? AND queue_date=? AND status='Serving'
                 AND id IN (
                     SELECT qt2.id
                     FROM queue_tokens qt2
                     LEFT JOIN appointments a ON a.id = qt2.appointment_id
                     WHERE qt2.department_id=? AND qt2.queue_date=?
                       AND COALESCE(a.doctor_id, 0) = ?
                 )""",
            (department_id, queue_date, department_id, queue_date, doctor_id),
        )

    # Fetch all waiting patients in appointment-time order.
    if doctor_id is None:
        waiting_sql = """
            SELECT qt.id, qt.position, qt.appointment_id,
                   a.id AS appt_id, a.visit_date, a.visit_time
            FROM queue_tokens qt
            LEFT JOIN appointments a ON a.id = qt.appointment_id
            WHERE qt.department_id=? AND qt.queue_date=? AND qt.status='Waiting'
              AND COALESCE(a.doctor_id, 0) = 0
            ORDER BY
                CASE WHEN TRIM(COALESCE(a.visit_time, '')) = '' THEN 1 ELSE 0 END,
                COALESCE(a.visit_date, qt.queue_date) ASC,
                COALESCE(a.visit_time, '') ASC,
                COALESCE(a.id, qt.id) ASC,
                qt.id ASC
        """
        waiting_tokens = conn.execute(waiting_sql, (department_id, queue_date)).fetchall()
    else:
        waiting_sql = """
            SELECT qt.id, qt.position, qt.appointment_id,
                   a.id AS appt_id, a.visit_date, a.visit_time
            FROM queue_tokens qt
            LEFT JOIN appointments a ON a.id = qt.appointment_id
            WHERE qt.department_id=? AND qt.queue_date=? AND qt.status='Waiting'
              AND COALESCE(a.doctor_id, 0) = ?
            ORDER BY
                CASE WHEN TRIM(COALESCE(a.visit_time, '')) = '' THEN 1 ELSE 0 END,
                COALESCE(a.visit_date, qt.queue_date) ASC,
                COALESCE(a.visit_time, '') ASC,
                COALESCE(a.id, qt.id) ASC,
                qt.id ASC
        """
        waiting_tokens = conn.execute(waiting_sql, (department_id, queue_date, doctor_id)).fetchall()

    for idx, token in enumerate(waiting_tokens):
        new_wait_time = (idx + 1) * AVG_SERVICE_TIME_MIN
        conn.execute(
            "UPDATE queue_tokens SET position=?, est_wait_min=? WHERE id=?",
            (idx, new_wait_time, token["id"]),
        )

    return len(waiting_tokens)


def _sync_queue_scope_after_token_change(conn, department_id, queue_date, appointment_id):
    doctor_id = _appointment_doctor_id(conn, appointment_id)
    recalculate_queue_positions(conn, department_id, queue_date, doctor_id)


def _queue_token_action_response(conn, token_id, action=None, target_department_id=None, target_doctor_id=None):
    """
    Shared backend action handler for token queue actions.
    Supports completion, skip, serving and transfer while keeping queue positions in sync.
    """
    row = conn.execute("SELECT * FROM queue_tokens WHERE id=?", (token_id,)).fetchone()
    if not row:
        return not_found("Queue token")

    action_norm = str(action or "").strip().lower()
    now_dt = datetime.now()
    now_time = now_dt.strftime("%H:%M:%S")
    queue_date = row["queue_date"] or reporting_date()
    old_department_id = row["department_id"]

    def _updated_payload(row_id):
        payload = conn.execute(
            """SELECT qt.*, p.name AS patient_name, p.age AS patient_age,
                      d.name AS department_name,
                      a.urgency AS urgency
               FROM queue_tokens qt
               LEFT JOIN patients p ON p.id = qt.patient_id
               LEFT JOIN departments d ON d.id = qt.department_id
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               WHERE qt.id=?""",
            (row_id,),
        ).fetchone()
        return row_to_dict(payload) if payload else None

    def _sync_appointment_status(appointment_id, status_value):
        if not appointment_id:
            return
        conn.execute(
            """UPDATE appointments
               SET status=?, updated_at=datetime('now')
               WHERE id=?""",
            (status_value, appointment_id),
        )

    if action_norm in {"done", "complete", "completed"}:
        conn.execute(
            """UPDATE queue_tokens
               SET status='Completed',
                   completed_at=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (now_time, token_id),
        )
        _sync_appointment_status(row["appointment_id"], "Completed")
        _sync_queue_scope_after_token_change(conn, old_department_id, queue_date, row["appointment_id"])

    elif action_norm in {"skip", "skipped"}:
        # "Skip" moves the token to the end of the queue for the same doctor.
        # The remaining patients keep their order, and the skipped token stays last.
        token_doctor_id = _appointment_doctor_id(conn, row["appointment_id"])

        # Find the current end of the same scope (doctor queue if available,
        # otherwise the department-only queue).
        max_pos_sql = """
            SELECT COALESCE(MAX(qt.position), -1) AS max_position
            FROM queue_tokens qt
            LEFT JOIN appointments a ON a.id = qt.appointment_id
            WHERE qt.department_id=? AND qt.queue_date=? AND qt.status='Waiting'
              AND qt.id<>?
        """
        max_params = [old_department_id, queue_date, token_id]
        if token_doctor_id:
            max_pos_sql += " AND COALESCE(a.doctor_id, 0) = ?"
            max_params.append(token_doctor_id)
        else:
            max_pos_sql += " AND COALESCE(a.doctor_id, 0) = 0"

        max_pos_row = conn.execute(max_pos_sql, tuple(max_params)).fetchone()
        new_position = int(max_pos_row["max_position"] or -1) + 1
        new_wait_time = (new_position + 1) * AVG_SERVICE_TIME_MIN

        conn.execute(
            """UPDATE queue_tokens
               SET status='Waiting',
                   position=?,
                   est_wait_min=?,
                   called_at=NULL,
                   completed_at=NULL,
                   updated_at=datetime('now')
               WHERE id=?""",
            (new_position, new_wait_time, token_id),
        )

        # Keep the serving patient's wait time at 0 for the same queue scope.
        if token_doctor_id:
            conn.execute(
                """UPDATE queue_tokens
                   SET est_wait_min=0
                   WHERE department_id=? AND queue_date=? AND status='Serving'
                     AND id IN (
                         SELECT qt2.id
                         FROM queue_tokens qt2
                         LEFT JOIN appointments a2 ON a2.id = qt2.appointment_id
                         WHERE qt2.department_id=? AND qt2.queue_date=?
                           AND COALESCE(a2.doctor_id, 0) = ?
                     )""",
                (old_department_id, queue_date, old_department_id, queue_date, token_doctor_id),
            )
        else:
            conn.execute(
                """UPDATE queue_tokens
                   SET est_wait_min=0
                   WHERE department_id=? AND queue_date=? AND status='Serving'
                     AND id IN (
                         SELECT qt2.id
                         FROM queue_tokens qt2
                         LEFT JOIN appointments a2 ON a2.id = qt2.appointment_id
                         WHERE qt2.department_id=? AND qt2.queue_date=?
                           AND COALESCE(a2.doctor_id, 0) = 0
                     )""",
                (old_department_id, queue_date, old_department_id, queue_date),
            )

        # Re-number waiting patients using the current queue order so the
        # skipped token remains at the end of that doctor's queue.
        if token_doctor_id:
            waiting_rows = conn.execute(
                """SELECT qt.id
                   FROM queue_tokens qt
                   LEFT JOIN appointments a ON a.id = qt.appointment_id
                   WHERE qt.department_id=? AND qt.queue_date=? AND qt.status='Waiting'
                     AND COALESCE(a.doctor_id, 0) = ?
                   ORDER BY qt.position ASC, qt.id ASC""",
                (old_department_id, queue_date, token_doctor_id),
            ).fetchall()
        else:
            waiting_rows = conn.execute(
                """SELECT qt.id
                   FROM queue_tokens qt
                   LEFT JOIN appointments a ON a.id = qt.appointment_id
                   WHERE qt.department_id=? AND qt.queue_date=? AND qt.status='Waiting'
                     AND COALESCE(a.doctor_id, 0) = 0
                   ORDER BY qt.position ASC, qt.id ASC""",
                (old_department_id, queue_date),
            ).fetchall()

        for idx, token in enumerate(waiting_rows):
            conn.execute(
                "UPDATE queue_tokens SET position=?, est_wait_min=? WHERE id=?",
                (idx, (idx + 1) * AVG_SERVICE_TIME_MIN, token["id"]),
            )

        _sync_appointment_status(row["appointment_id"], "Active")

    elif action_norm in {"serving"}:
        conn.execute(
            """UPDATE queue_tokens
               SET status='Serving',
                   called_at=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (now_time, token_id),
        )
        _sync_appointment_status(row["appointment_id"], "Active")
        _sync_queue_scope_after_token_change(conn, old_department_id, queue_date, row["appointment_id"])

    elif action_norm in {"transfer", "transferred"}:
        target_department_id = coerce_int(target_department_id, 0) or None
        # Fallback: same-department reassign sends current dept; use token's own dept
        if not target_department_id:
            target_department_id = coerce_int(old_department_id, 0) or None
        if not target_department_id:
            return bad_request("target department_id is required")

        target_dept = conn.execute(
            "SELECT * FROM departments WHERE id=?",
            (target_department_id,),
        ).fetchone()
        if not target_dept:
            return bad_request("Invalid department")

        # Resolve target doctor — use the explicitly supplied doctor_id if provided,
        # otherwise fall back to the first On Duty doctor in the target department.
        resolved_doctor_id = coerce_int(target_doctor_id, 0) or None
        if not resolved_doctor_id:
            fallback_doc = conn.execute(
                """SELECT id FROM doctors
                   WHERE department_id = ?
                     AND COALESCE(is_active, 1) = 1
                   ORDER BY
                     CASE WHEN LOWER(TRIM(COALESCE(status,''))) = 'on duty' THEN 0 ELSE 1 END,
                     id ASC
                   LIMIT 1""",
                (target_department_id,),
            ).fetchone()
            resolved_doctor_id = fallback_doc["id"] if fallback_doc else None

        # Capture the original doctor BEFORE any updates — used for source-dept sync below.
        old_doctor_id = _appointment_doctor_id(conn, row["appointment_id"])

        # Compute new position in the target doctor's queue, excluding the current
        # token so it always gets placed at the end of the new doctor's list.
        if resolved_doctor_id:
            max_pos_row = conn.execute(
                """SELECT COALESCE(MAX(qt.position), -1) AS max_position
                   FROM queue_tokens qt
                   LEFT JOIN appointments a ON a.id = qt.appointment_id
                   WHERE qt.department_id = ?
                     AND qt.queue_date = ?
                     AND qt.status = 'Waiting'
                     AND qt.id != ?
                     AND COALESCE(a.doctor_id, 0) = ?""",
                (target_department_id, queue_date, token_id, resolved_doctor_id),
            ).fetchone()
        else:
            max_pos_row = conn.execute(
                """SELECT COALESCE(MAX(position), -1) AS max_position
                   FROM queue_tokens
                   WHERE department_id = ? AND queue_date = ? AND status = 'Waiting'
                     AND id != ?""",
                (target_department_id, queue_date, token_id),
            ).fetchone()

        new_position = int(max_pos_row["max_position"] or -1) + 1
        new_wait = (new_position + 1) * AVG_SERVICE_TIME_MIN

        # Update the token — move to target department (or stay in same dept)
        conn.execute(
            """UPDATE queue_tokens
               SET department_id=?,
                   status='Waiting',
                   position=?,
                   est_wait_min=?,
                   called_at=NULL,
                   completed_at=NULL,
                   updated_at=datetime('now')
               WHERE id=?""",
            (target_department_id, new_position, new_wait, token_id),
        )

        # Always update the appointment with both dept AND doctor — this is the
        # critical fix for same-department reassign where doctor_id was never saved.
        if row["appointment_id"]:
            if resolved_doctor_id:
                conn.execute(
                    """UPDATE appointments
                       SET department_id=?,
                           doctor_id=?,
                           status='Active',
                           updated_at=datetime('now')
                       WHERE id=?""",
                    (target_department_id, resolved_doctor_id, row["appointment_id"]),
                )
            else:
                conn.execute(
                    """UPDATE appointments
                       SET department_id=?,
                           status='Active',
                           updated_at=datetime('now')
                       WHERE id=?""",
                    (target_department_id, row["appointment_id"]),
                )

        # Re-sync queue positions for all affected doctor scopes.
        if old_department_id != target_department_id:
            recalculate_queue_positions(conn, old_department_id, queue_date, old_doctor_id)
            recalculate_queue_positions(conn, target_department_id, queue_date, resolved_doctor_id)
        else:
            # Same-department: resync both old and new doctor queues
            if old_doctor_id and old_doctor_id != resolved_doctor_id:
                recalculate_queue_positions(conn, old_department_id, queue_date, old_doctor_id)
            recalculate_queue_positions(conn, target_department_id, queue_date, resolved_doctor_id)
    else:
        return bad_request(
            "Unsupported action. Use done/completed, skip, serving, or transfer."
        )

    conn.commit()
    updated = _updated_payload(token_id)
    try:
        # For transfer actions, the token moves to a *different* department, so we
        # must broadcast a full snapshot (all departments) so the dashboard reflects
        # both the source department losing a patient AND the target department
        # gaining one.  For all other actions, a full snapshot is still safest and
        # avoids any stale data on the frontend.
        _sse_broadcast(
            "queue_update",
            {"queue": _queue_snapshot(), "source": "queue_action", "token_id": token_id},
        )
    except Exception:
        pass
    return ok(updated, f"Token {action_norm or 'updated'}")


app = Flask(__name__, static_folder=".")

# ── Patch get_db to add 10s busy timeout so concurrent requests don't 500 ──
_orig_get_db = get_db
def get_db():
    import sqlite3 as _sqlite3
    conn = _orig_get_db()
    conn.execute("PRAGMA busy_timeout = 10000")  # 10 second lock wait
    return conn
CORS(app, resources={r"/*": {"origins": "*"}},
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization", "X-Department-ID"],
     methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_HOURS = int(os.getenv("HQ_SESSION_HOURS", "72"))  # 72h so sessions survive restarts
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
ADMIN_WRITE_PREFIXES = ("/api/departments", "/api/doctors", "/api/settings", "/api/realloc")

init_db()

# Ensure feedback_entries table exists (added in v2.x; may be missing from older DBs)
def _ensure_extra_tables():
    try:
        from database import get_db as _gdb
        conn = _gdb()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                category    TEXT    DEFAULT 'feedback',
                subject     TEXT    DEFAULT '',
                message     TEXT    NOT NULL DEFAULT '',
                rating      INTEGER,
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                username    TEXT    DEFAULT '',
                role        TEXT    DEFAULT '',
                action      TEXT    NOT NULL,
                entity_type TEXT    DEFAULT '',
                entity_id   TEXT    DEFAULT '',
                details_json TEXT   DEFAULT '{}',
                ip_address  TEXT    DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now'))
            )
        """)
        # Add designation + email_verified to users if missing
        cols = [c["name"] for c in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "designation" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN designation TEXT DEFAULT ''")
        if "email_verified" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 0")
        if "patient_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN patient_id INTEGER DEFAULT NULL")
        # Add updated_at to queue_tokens if missing
        qt_cols = [c["name"] for c in conn.execute("PRAGMA table_info(queue_tokens)").fetchall()]
        if "updated_at" not in qt_cols:
            conn.execute("ALTER TABLE queue_tokens ADD COLUMN updated_at TEXT DEFAULT (datetime('now'))")
        conn.commit()
        conn.close()
        print("[BOOT] Extra tables/columns ensured OK")
    except Exception as e:
        print(f"[BOOT] Extra table check: {e}")

_ensure_extra_tables()


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def public_api(path):
    _PUBLIC = {
        "/api/auth/login",
        "/api/auth/register",
        "/health",
    }
    # Also treat read-only (GET) dashboard/queue endpoints as semi-public
    # so the frontend can show data before token is confirmed
    _PUBLIC_PREFIXES = (
        "/api/dashboard",
        "/api/queue/summary",
        "/queue/summary",
        "/api/queue/version",
        "/api/departments",
        "/api/schedules",
        "/api/stream",
        "/api/doctors",
        "/api/settings",
    )
    return path in _PUBLIC or any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def load_user_from_token():
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None

    conn = get_db()
    row = conn.execute(
        """SELECT s.id AS session_id, s.expires_at, u.id, u.username, u.role,
                  u.display_name, u.email, u.is_active, u.patient_id
           FROM user_sessions s
           JOIN users u ON u.id=s.user_id
           WHERE s.token=?""",
        (token,),
    ).fetchone()
    if not row:
        conn.close()
        return None
    if row["is_active"] != 1 or row["expires_at"] <= datetime.utcnow().isoformat():
        conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
        return None
    # Extend session by 12h on each active use (rolling window)
    new_expiry = (datetime.utcnow() + timedelta(hours=12)).isoformat()
    conn.execute(
        "UPDATE user_sessions SET last_seen_at=datetime('now'), expires_at=? WHERE token=?",
        (new_expiry, token)
    )
    conn.commit()
    conn.close()
    return row_to_dict(row)


@app.before_request
def attach_current_user():
    # Always allow OPTIONS (CORS preflight)
    if request.method == "OPTIONS":
        return
    # Public endpoints never need auth
    if public_api(request.path):
        g.current_user = None
        return
    if request.path.startswith("/api/"):
        g.current_user = load_user_from_token()
        # Only block WRITE methods when not authenticated
        if request.method in WRITE_METHODS and not g.current_user:
            return unauthorized()
        # Admin-only writes
        if request.method in WRITE_METHODS and request.path.startswith(ADMIN_WRITE_PREFIXES):
            if not g.current_user or g.current_user.get("role") != "admin":
                return forbidden("Admin role required for this action")


def current_user():
    return getattr(g, "current_user", None)


def require_auth(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return unauthorized()
            if roles and user["role"] not in roles:
                return forbidden()
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def log_audit(action, entity_type="", entity_id="", details=None):
    try:
        user = current_user() or {}
        conn = get_db()
        conn.execute(
            """INSERT INTO audit_log
               (user_id, username, role, action, entity_type, entity_id, details_json, ip_address)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                user.get("id"),
                user.get("username", "system"),
                user.get("role", "system"),
                action,
                entity_type,
                str(entity_id or ""),
                json.dumps(details or {}, default=str),
                client_ip(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[AUDIT] log failed: {exc}")


def csv_response(filename, rows, fieldnames):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def staff_department_id():
    header_value = request.headers.get("X-Department-ID") or request.args.get("department_id")
    if header_value:
        return coerce_int(header_value, 1)
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='staff_department_id'").fetchone()
    conn.close()
    return coerce_int(row["value"] if row else 1, 1)


def staff_department():
    dept_id = staff_department_id()
    conn = get_db()
    row = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
    conn.close()
    return row_to_dict(row) if row else {"id": dept_id, "name": "Assigned Department"}


def reporting_date() -> str:
    """Return the current operational date — always real calendar today.
    The operational_date setting is no longer used for queue/appointment
    lookups; every query uses date.today() so the dashboard always reflects
    only what was entered today.
    """
    return date.today().isoformat()


def table_columns(conn, table_name: str) -> set[str]:
    """Return the set of column names for a table, or an empty set if missing."""
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()


def _minutes_between(start_value: str, end_value: str) -> int | None:
    from datetime import datetime as _dt
    if not start_value or not end_value:
        return None
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"]
    start = end = None
    for fmt in fmts:
        try:
            start = _dt.strptime(start_value, fmt)
            break
        except Exception:
            pass
    for fmt in fmts:
        try:
            end = _dt.strptime(end_value, fmt)
            break
        except Exception:
            pass
    if not start or not end:
        return None
    delta = end - start
    return max(0, int(round(delta.total_seconds() / 60.0)))


def ensure_department_by_name(conn, department_name: str | None):
    """
    Return a department row for the provided name.
    If the department does not exist yet, create it with safe defaults.
    """
    if not department_name:
        return None
    dept_name = str(department_name).strip()
    if not dept_name:
        return None

    row = conn.execute("SELECT * FROM departments WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))", (dept_name,)).fetchone()
    if row:
        return row

    cur = conn.execute(
        """INSERT INTO departments
           (name, emoji, floor_location, beds, max_capacity, doctor_count, nurse_count, priority_rule, color_hex, is_active)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (dept_name, "🏥", "", 20, 25, 0, 0, "FIFO", "#f0fdf4"),
    )
    return conn.execute("SELECT * FROM departments WHERE id=?", (cur.lastrowid,)).fetchone()


def resolve_department_for_request(conn, data: dict):
    """
    Try department_id first, then department_name.
    Create the department on demand when a name is provided.
    """
    dept_id = data.get("department_id")
    dept_name = data.get("department_name")

    if dept_id not in (None, "", 0, "0"):
        row = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
        if row:
            return row

    if dept_name:
        return ensure_department_by_name(conn, dept_name)

    return None


def resolve_department_id_for_doctor(conn, data: dict):
    """
    Resolve a doctor's department using either department_id or department_name.
    """
    dept_id = data.get("department_id")
    dept_name = data.get("department_name")

    if dept_id not in (None, "", 0, "0"):
        row = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
        if row:
            return row["id"]

    if dept_name:
        row = ensure_department_by_name(conn, dept_name)
        if row:
            return row["id"]

    return None


@app.route("/")
def index():
    # Try common filenames in order
    for fname in [
        "hospital_queue_system.html",
        "index.html",
        "hospital_queue_system_fixed (1).html",
        "hospital_queue_system_fixed__1_.html",
        "hospital_queue_system(4).html",
    ]:
        fpath = os.path.join(BASE_DIR, fname)
        if os.path.exists(fpath):
            return send_from_directory(BASE_DIR, fname)
    # Last-resort: list available HTML files in directory
    html_files = [f for f in os.listdir(BASE_DIR) if f.endswith(".html")]
    if html_files:
        return send_from_directory(BASE_DIR, html_files[0])
    return "<h1>Hospital Queue System</h1><p>No HTML file found in: " + BASE_DIR + "</p>", 404

@app.route("/live_data.js")
def serve_live_data():
    return send_from_directory(BASE_DIR, "live_data.js")

@app.route("/api_helpers.py")
def block_api_helpers():
    return "", 404  # never expose this


@app.route("/health")
def health():
    conn = get_db()
    departments = conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
    conn.close()
    return ok({"status": "ok", "database": "connected", "departments": departments, "version": "v2.1"})


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    if not username or not password:
        return bad_request("username and password are required")

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE (username=? OR email=?) AND is_active=1", (username, username)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        conn.close()
        return unauthorized("Invalid username or password")

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=SESSION_HOURS)).isoformat()
    # Clean up expired sessions for this user before creating new one
    conn.execute("DELETE FROM user_sessions WHERE user_id=? AND expires_at <= ?", (user["id"], datetime.utcnow().isoformat()))
    conn.execute("INSERT INTO user_sessions (user_id, token, expires_at) VALUES (?,?,?)", (user["id"], token, expires_at))
    conn.commit()
    conn.close()

    g.current_user = row_to_dict(user)
    log_audit("login", "user", user["id"], {"username": username})
    return ok({
        "token": token,
        "expires_at": expires_at,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "display_name": user["display_name"],
            "email": user["email"],
        },
    }, "Login successful")


@app.route("/api/auth/me")
@require_auth()
def me():
    return ok(current_user())


@app.route("/api/auth/logout", methods=["POST"])
@require_auth()
def logout():
    auth = request.headers.get("Authorization", "")
    token = auth.split(" ", 1)[1].strip() if " " in auth else ""
    conn = get_db()
    conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()
    log_audit("logout", "user", current_user().get("id"))
    return ok({}, "Logged out")


@app.route("/api/departments", methods=["GET"])
def list_departments():
    conn = get_db()
    today = reporting_date()
    rows = rows_to_list(conn.execute("SELECT * FROM departments WHERE is_active=1 ORDER BY name").fetchall())
    doctor_counts = {
        row["department_id"]: int(row["doctor_count"] or 0)
        for row in conn.execute(
            """SELECT department_id, COUNT(*) AS doctor_count
               FROM doctors
               WHERE is_active=1
               GROUP BY department_id"""
        ).fetchall()
        if row["department_id"] is not None
    }

    # Attach live queue stats to each department
    dept_stats = {}
    stats_rows = conn.execute("""
        SELECT d.id,
               COUNT(CASE WHEN qt.status='Waiting' THEN 1 END) AS waiting,
               COUNT(CASE WHEN qt.status='Serving' THEN 1 END) AS serving
        FROM departments d
        LEFT JOIN queue_tokens qt ON qt.department_id=d.id AND qt.queue_date=?
            AND qt.status IN ('Waiting','Serving')
        WHERE d.is_active=1
        GROUP BY d.id
    """, (today,)).fetchall()
    for s in stats_rows:
        waiting = s["waiting"] or 0
        serving = s["serving"] or 0
        if serving > 0:
            avg_wait = round((waiting + 1) * 8)
        else:
            avg_wait = round(waiting * 8)
        if avg_wait >= 60:
            status = "High"
        elif avg_wait >= 30:
            status = "Medium"
        else:
            status = "Low"
        dept_stats[s["id"]] = {"waiting": waiting, "avg_wait_min": avg_wait, "queue_status": status}

    for row in rows:
        stats = dept_stats.get(row["id"], {})
        row["doctor_count"] = doctor_counts.get(row["id"], 0)
        row["waiting"] = stats.get("waiting", 0)
        row["avg_wait_min"] = stats.get("avg_wait_min", 0)
        row["queue_status"] = stats.get("queue_status", "Low")

    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/departments", methods=["POST"])
def create_department():
    data = request.get_json() or {}
    missing = require_fields(data, ["name"])
    if missing:
        return bad_request(f"Missing required fields: {missing}")
    conn = get_db()
    if conn.execute("SELECT id FROM departments WHERE name=?", (data["name"].strip(),)).fetchone():
        conn.close()
        return conflict("Department already exists")
    cur = conn.execute(
        """INSERT INTO departments
           (name, emoji, floor_location, beds, max_capacity, doctor_count, nurse_count, priority_rule, color_hex)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            data["name"].strip(), data.get("emoji", "H"), data.get("floor_location", ""),
            coerce_int(data.get("beds"), 20), coerce_int(data.get("max_capacity"), 25),
            coerce_int(data.get("doctor_count"), 0), coerce_int(data.get("nurse_count"), 0),
            data.get("priority_rule", "FIFO"), data.get("color_hex", "#f0fdf4"),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM departments WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    log_audit("create", "department", cur.lastrowid)
    return created(row_to_dict(row), "Department created")


@app.route("/api/departments/<int:dept_id>", methods=["PUT", "DELETE"])
def modify_department(dept_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
    if not row:
        conn.close()
        return not_found("Department")
    if request.method == "DELETE":
        conn.execute("UPDATE departments SET is_active=0, updated_at=datetime('now') WHERE id=?", (dept_id,))
        conn.commit()
        conn.close()
        log_audit("delete", "department", dept_id)
        return ok({"id": dept_id}, "Department deactivated")
    data = request.get_json() or {}
    d = dict(row)
    conn.execute(
        """UPDATE departments SET name=?, emoji=?, floor_location=?, beds=?, max_capacity=?,
           doctor_count=?, nurse_count=?, priority_rule=?, color_hex=?, updated_at=datetime('now')
           WHERE id=?""",
        (
            data.get("name", d["name"]), data.get("emoji", d["emoji"]),
            data.get("floor_location", d["floor_location"]), coerce_int(data.get("beds"), d["beds"]),
            coerce_int(data.get("max_capacity"), d["max_capacity"]),
            coerce_int(data.get("doctor_count"), d["doctor_count"]),
            coerce_int(data.get("nurse_count"), d["nurse_count"]),
            data.get("priority_rule", d["priority_rule"]), data.get("color_hex", d["color_hex"]), dept_id,
        ),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
    conn.close()
    log_audit("update", "department", dept_id)
    return ok(row_to_dict(updated), "Department updated")


@app.route("/api/doctors", methods=["GET"])
def list_doctors():
    conn = get_db()
    today = reporting_date()
    rows = rows_to_list(conn.execute(
        """SELECT d.*, dep.name AS department_name,
                  (SELECT COUNT(*)
                   FROM queue_tokens qt
                   LEFT JOIN appointments a ON a.id = qt.appointment_id
                   WHERE qt.queue_date = ?
                   AND qt.status IN ('Waiting', 'Serving')
                   AND (
                       -- Explicitly assigned to this doctor via appointment
                       a.doctor_id = d.id
                       OR
                       -- Token has no doctor assigned; attribute to this doctor only if
                       -- they are the sole On Duty doctor in the department
                       (
                           (a.doctor_id IS NULL OR a.doctor_id = 0)
                           AND qt.department_id = d.department_id
                           AND (
                               SELECT COUNT(*) FROM doctors d2
                               WHERE d2.department_id = d.department_id
                               AND d2.status = 'On Duty' AND d2.is_active = 1
                           ) = 1
                       )
                   )) AS patients_today
           FROM doctors d LEFT JOIN departments dep ON d.department_id=dep.id
           WHERE d.is_active=1 ORDER BY dep.name, d.name""",
        (today,)
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/doctors", methods=["POST"])
def create_doctor():
    data = request.get_json() or {}
    missing = require_fields(data, ["name", "specialization"])
    if missing:
        return bad_request(f"Missing required fields: {missing}")
    if data.get("status") and data["status"] not in STATUS_DOCTOR:
        return bad_request(f"Invalid status: {STATUS_DOCTOR}")
    if data.get("shift") and data["shift"] not in SHIFT_VALUES:
        return bad_request(f"Invalid shift: {SHIFT_VALUES}")
    conn = get_db()
    last = conn.execute("SELECT doctor_code FROM doctors ORDER BY id DESC LIMIT 1").fetchone()
    code = next_doctor_code(last["doctor_code"] if last else "DR000")
    dept_id = resolve_department_id_for_doctor(conn, data)
    cur = conn.execute(
        """INSERT INTO doctors (doctor_code, name, specialization, department_id, shift, status, phone, email)
           VALUES (?,?,?,?,?,?,?,?)""",
        (code, data["name"].strip(), data["specialization"].strip(), dept_id,
         data.get("shift", "Morning"), data.get("status", "On Duty"), data.get("phone", ""), data.get("email", "")),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM doctors WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    log_audit("create", "doctor", cur.lastrowid, {"doctor_code": code})
    return created(row_to_dict(row), "Doctor created")


@app.route("/api/doctors/<int:doc_id>", methods=["PUT", "DELETE"])
def modify_doctor(doc_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM doctors WHERE id=?", (doc_id,)).fetchone()
    if not row:
        conn.close()
        return not_found("Doctor")
    if request.method == "DELETE":
        conn.execute("UPDATE doctors SET is_active=0, updated_at=datetime('now') WHERE id=?", (doc_id,))
        conn.commit()
        conn.close()
        log_audit("delete", "doctor", doc_id)
        return ok({"id": doc_id}, "Doctor removed")
    data = request.get_json() or {}
    d = dict(row)
    dept_id = resolve_department_id_for_doctor(conn, data)
    if dept_id is None:
        dept_id = d["department_id"]
    conn.execute(
        """UPDATE doctors SET name=?, specialization=?, department_id=?, shift=?, status=?,
           phone=?, email=?, updated_at=datetime('now') WHERE id=?""",
        (data.get("name", d["name"]), data.get("specialization", d["specialization"]),
         dept_id, data.get("shift", d["shift"]),
         data.get("status", d["status"]), data.get("phone", d["phone"]), data.get("email", d["email"]), doc_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM doctors WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    log_audit("update", "doctor", doc_id)
    return ok(row_to_dict(updated), "Doctor updated")


@app.route("/api/patients", methods=["GET"])
def list_patients():
    conn = get_db()
    search = request.args.get("search", "")
    phone = request.args.get("phone", "")
    department_id = request.args.get("department_id", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    urgency = request.args.get("urgency", "")
    q = """SELECT DISTINCT p.*,
                  a.department_id AS last_department_id,
                  d.name AS department_name,
                  a.visit_date AS last_visit_date,
                  a.urgency AS last_urgency,
                  a.status AS last_status
           FROM patients p
           LEFT JOIN appointments a ON a.patient_id=p.id
           LEFT JOIN departments d ON a.department_id=d.id
           WHERE p.is_active=1"""
    params = []
    if search:
        q += " AND (p.name LIKE ? OR p.patient_code LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if phone:
        q += " AND p.phone LIKE ?"
        params.append(f"%{phone}%")
    if department_id:
        q += " AND a.department_id=?"
        params.append(department_id)
    if date_from:
        q += " AND a.visit_date>=?"
        params.append(date_from)
    if date_to:
        q += " AND a.visit_date<=?"
        params.append(date_to)
    if urgency:
        q += " AND a.urgency=?"
        params.append(urgency)
    q += " ORDER BY name"
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    page = coerce_int(request.args.get("page"), 1)
    per_page = coerce_int(request.args.get("per_page"), 50)
    data, meta = paginate(rows, page, per_page)
    return ok(data, meta=meta)


@app.route("/api/patients", methods=["POST"])
def create_patient():
    data = request.get_json() or {}
    missing = require_fields(data, ["name"])
    if missing:
        return bad_request(f"Missing required fields: {missing}")
    if data.get("phone") and not validate_phone(data["phone"]):
        return bad_request("Invalid phone number")
    if data.get("gender") and data["gender"] not in GENDER_VALUES:
        return bad_request(f"Invalid gender: {GENDER_VALUES}")
    conn = get_db()
    last = conn.execute("SELECT patient_code FROM patients ORDER BY id DESC LIMIT 1").fetchone()
    code = next_patient_code(last["patient_code"] if last else "P000")
    cur = conn.execute(
        """INSERT INTO patients (patient_code, name, age, gender, phone, email, blood_group, address, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (code, data["name"].strip(), coerce_int(data.get("age"), 0), data.get("gender", "Other"),
         data.get("phone", ""), data.get("email", ""), data.get("blood_group", ""),
         data.get("address", ""), data.get("notes", "")),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM patients WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    log_audit("create", "patient", cur.lastrowid, {"patient_code": code})
    return created(row_to_dict(row), "Patient registered")


@app.route("/api/patients/<int:pat_id>", methods=["GET", "PUT", "DELETE"])
def patient_detail(pat_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM patients WHERE id=?", (pat_id,)).fetchone()
    if not row:
        conn.close()
        return not_found("Patient")
    if request.method == "GET":
        result = row_to_dict(row)
        result["appointments"] = rows_to_list(conn.execute(
            "SELECT * FROM appointments WHERE patient_id=? ORDER BY visit_date DESC", (pat_id,)
        ).fetchall())
        conn.close()
        return ok(result)
    if request.method == "DELETE":
        conn.execute("UPDATE patients SET is_active=0, updated_at=datetime('now') WHERE id=?", (pat_id,))
        conn.commit()
        conn.close()
        log_audit("delete", "patient", pat_id)
        return ok({"id": pat_id}, "Patient removed")
    data = request.get_json() or {}
    d = dict(row)
    conn.execute(
        """UPDATE patients SET name=?, age=?, gender=?, phone=?, email=?, blood_group=?,
           address=?, notes=?, updated_at=datetime('now') WHERE id=?""",
        (data.get("name", d["name"]), coerce_int(data.get("age"), d["age"]),
         data.get("gender", d["gender"]), data.get("phone", d["phone"]), data.get("email", d["email"]),
         data.get("blood_group", d["blood_group"]), data.get("address", d["address"]),
         data.get("notes", d["notes"]), pat_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM patients WHERE id=?", (pat_id,)).fetchone()
    conn.close()
    log_audit("update", "patient", pat_id)
    return ok(row_to_dict(updated), "Patient updated")


@app.route("/api/appointments", methods=["GET"])
def list_appointments():
    conn = get_db()
    q = """SELECT a.*,
                  p.name AS patient_name, p.phone AS patient_phone, p.age AS patient_age,
                  d.name AS department_name,
                  doc.name AS doctor_name,
                  qt.token_number AS token_number,
                  qt.est_wait_min AS token_est_wait_min,
                  qt.status AS token_status
           FROM appointments a
           LEFT JOIN patients p ON a.patient_id=p.id
           LEFT JOIN departments d ON a.department_id=d.id
           LEFT JOIN doctors doc ON a.doctor_id=doc.id
           LEFT JOIN queue_tokens qt ON qt.appointment_id=a.id
           WHERE 1=1"""
    params = []
    for arg, col in [("department_id", "a.department_id"), ("status", "a.status"), ("patient_id", "a.patient_id")]:
        val = request.args.get(arg)
        if val:
            q += f" AND {col}=?"
            params.append(val)
    visit_date = request.args.get("visit_date")
    if visit_date:
        q += " AND DATE(TRIM(COALESCE(a.visit_date,''))) = DATE(?)"
        params.append(visit_date)
    q += " ORDER BY a.visit_date DESC, a.id DESC"
    per_page = coerce_int(request.args.get("per_page"), 0)
    rows = rows_to_list(conn.execute(q, params).fetchall())
    conn.close()
    if per_page:
        rows = rows[:per_page]
    return ok(rows, meta={"total": len(rows)})



@app.route("/api/appointments", methods=["POST"])
def create_appointment():
    data = request.get_json(silent=True) or {}
    missing = require_fields(data, ["patient_id", "visit_date"])
    if missing:
        return bad_request(f"Missing required fields: {missing}")
    if not validate_date(str(data.get("visit_date", ""))):
        return bad_request("Invalid visit_date format. Use YYYY-MM-DD")
    if data.get("urgency") and data["urgency"] not in URGENCY_VALUES:
        return bad_request(f"Invalid urgency: {URGENCY_VALUES}")

    conn = get_db()
    try:
        dept = resolve_department_for_request(conn, data)
        if not dept:
            return bad_request("department_id or department_name is required")

        dept_id = dept["id"]
        visit_date = str(data.get("visit_date", "")).strip() or reporting_date()
        doctor_id = coerce_int(data.get("doctor_id"), 0) or None
        visit_time = str(data.get("visit_time", "")).strip()
        status = str(data.get("status", "Active"))
        created_by = current_user().get("username", "admin") if current_user() else "admin"

        shift = _doctor_shift_window(conn, doctor_id) if doctor_id else None
        normalized_visit_time = _normalize_visit_time_for_shift(visit_time, shift)
        if shift and normalized_visit_time is None:
            return bad_request(
                f"Visit time must be within the selected doctor's shift ({shift['display']})."
            )
        visit_time = normalized_visit_time if normalized_visit_time is not None else visit_time

        cur = conn.execute(
            """INSERT INTO appointments
               (patient_id, department_id, doctor_id, visit_date, visit_time, urgency, status, symptoms, created_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                data["patient_id"],
                dept_id,
                doctor_id,
                visit_date,
                visit_time,
                data.get("urgency", "Medium"),
                status,
                data.get("symptoms", ""),
                created_by,
            ),
        )
        appt_id = cur.lastrowid
        token_num = None

        real_today_appt = date.today().isoformat()
        # Generate a queue token for real today only.
        if status in ("Active", "Scheduled") and visit_date == real_today_appt:
            token_queue_date = real_today_appt
            existing = [r["token_number"] for r in conn.execute(
                "SELECT token_number FROM queue_tokens WHERE department_id=? AND queue_date=?",
                (dept_id, token_queue_date),
            ).fetchall()]
            token_num = generate_token(dept["name"], existing)

            urgency_val = data.get("urgency", "Medium")

            # Some DB copies may not yet have an 'urgency' column on queue_tokens.
            token_cols = [r["name"] for r in conn.execute("PRAGMA table_info(queue_tokens)").fetchall()]
            if "urgency" in token_cols:
                conn.execute(
                    """INSERT INTO queue_tokens
                       (token_number, appointment_id, patient_id, department_id, queue_date,
                        position, est_wait_min, urgency, status)
                       VALUES (?,?,?,?,?,?,?,?,'Waiting')""",
                    (token_num, appt_id, data["patient_id"], dept_id, token_queue_date,
                     0, 0, urgency_val),
                )
            else:
                conn.execute(
                    """INSERT INTO queue_tokens
                       (token_number, appointment_id, patient_id, department_id, queue_date,
                        position, est_wait_min, status)
                       VALUES (?,?,?,?,?,?,?,'Waiting')""",
                    (token_num, appt_id, data["patient_id"], dept_id, token_queue_date,
                     0, 0),
                )
            _sync_queue_scope_after_token_change(conn, dept_id, token_queue_date, appt_id)

        conn.commit()
        row = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()

        result = row_to_dict(row)
        if token_num:
            result["token_number"] = token_num
            log_audit("token_generated", "queue_token", token_num, {"appointment_id": appt_id})
        log_audit("create", "appointment", appt_id)
        return created(result, "Appointment created")
    except Exception as exc:
        conn.rollback()
        print(f"[APPOINTMENT CREATE ERROR] {exc}")
        return server_error(f"Appointment creation failed: {exc}")
    finally:
        conn.close()


@app.route("/api/appointments/<int:appt_id>", methods=["GET", "PUT", "DELETE"])
def appointment_detail(appt_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
    if not row:
        conn.close()
        return not_found("Appointment")
    if request.method == "GET":
        conn.close()
        return ok(row_to_dict(row))
    if request.method == "DELETE":
        conn.execute("UPDATE appointments SET status='Cancelled', updated_at=datetime('now') WHERE id=?", (appt_id,))
        conn.commit()
        conn.close()
        log_audit("delete", "appointment", appt_id, {"status": "Cancelled"})
        return ok({"id": appt_id}, "Appointment cancelled")
    data = request.get_json() or {}
    d = dict(row)

    doctor_id = coerce_int(data.get("doctor_id", d["doctor_id"]), 0) or None
    visit_date = str(data.get("visit_date", d["visit_date"]) or "").strip()
    visit_time = str(data.get("visit_time", d["visit_time"]) or "").strip()
    shift = _doctor_shift_window(conn, doctor_id) if doctor_id else None
    normalized_visit_time = _normalize_visit_time_for_shift(visit_time, shift)
    if shift and normalized_visit_time is None:
        conn.close()
        return bad_request(
            f"Visit time must be within the selected doctor's shift ({shift['display']})."
        )
    visit_time = normalized_visit_time if normalized_visit_time is not None else visit_time

    conn.execute(
        """UPDATE appointments SET doctor_id=?, visit_date=?, visit_time=?, urgency=?,
           status=?, symptoms=?, diagnosis=?, updated_at=datetime('now') WHERE id=?""",
        (doctor_id, visit_date, visit_time, data.get("urgency", d["urgency"]),
         data.get("status", d["status"]), data.get("symptoms", d["symptoms"]),
         data.get("diagnosis", d["diagnosis"]), appt_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM appointments WHERE id=?", (appt_id,)).fetchone()
    conn.close()
    log_audit("update", "appointment", appt_id)
    return ok(row_to_dict(updated), "Appointment updated")


@app.route("/api/appointments/<int:appt_id>/status", methods=["PATCH"])
def appointment_status(appt_id):
    data = request.get_json() or {}
    status = data.get("status")
    if status not in STATUS_APPT:
        return bad_request(f"Valid statuses: {STATUS_APPT}")
    conn = get_db()
    conn.execute("UPDATE appointments SET status=?, updated_at=datetime('now') WHERE id=?", (status, appt_id))
    conn.commit()
    conn.close()
    log_audit("status_update", "appointment", appt_id, {"status": status})
    return ok({"id": appt_id, "status": status}, "Status updated")


@app.route("/api/queue", methods=["GET"])

def list_queue():
    conn = get_db()
    dept_id = request.args.get("department_id")
    status = request.args.get("status", "Waiting")
    real_today = date.today().isoformat()
    qdate = request.args.get("date") or real_today

    token_cols = table_columns(conn, "queue_tokens")
    appt_cols = table_columns(conn, "appointments")
    appt_has_doctor_id = "doctor_id" in appt_cols

    # Never reference a column unless we know it exists in this database copy.
    if "urgency" in token_cols and "urgency" in appt_cols:
        urgency_expr = "COALESCE(qt.urgency, a.urgency, 'Medium') AS urgency"
    elif "urgency" in token_cols:
        urgency_expr = "COALESCE(qt.urgency, 'Medium') AS urgency"
    elif "urgency" in appt_cols:
        urgency_expr = "COALESCE(a.urgency, 'Medium') AS urgency"
    else:
        urgency_expr = "'Medium' AS urgency"

    doctor_expr = "a.doctor_id AS doctor_id" if appt_has_doctor_id else "NULL AS doctor_id"
    doctor_name_expr = "doc.name AS doctor_name" if appt_has_doctor_id else "NULL AS doctor_name"

    q = f"""SELECT qt.*,
                  p.name  AS patient_name,
                  p.age   AS patient_age,
                  d.name  AS department_name,
                  {doctor_expr},
                  {doctor_name_expr},
                  {urgency_expr}
           FROM queue_tokens qt
           LEFT JOIN patients    p ON qt.patient_id    = p.id
           LEFT JOIN departments d ON qt.department_id = d.id
           LEFT JOIN appointments a ON a.id = qt.appointment_id
           LEFT JOIN doctors     doc ON a.doctor_id = doc.id
           WHERE qt.queue_date = ?"""
    params = [qdate]
    if dept_id:
        q += " AND qt.department_id = ?"
        params.append(dept_id)
    if status:
        if "," in status:
            placeholders = ",".join("?" * len(status.split(",")))
            q += f" AND qt.status IN ({placeholders})"
            params.extend(status.split(","))
        else:
            q += " AND qt.status = ?"
            params.append(status)
    q += " ORDER BY qt.position ASC, qt.id ASC"

    try:
        rows = rows_to_list(conn.execute(q, params).fetchall())
    except Exception as exc:
        # Backward-compatible fallback for older DB copies that may still
        # have partial schema drift or missing appointment columns.
        print(f"[QUEUE LIST ERROR] primary query failed: {exc}")
        fallback_urgency = "COALESCE(a.urgency, 'Medium') AS urgency" if "urgency" in appt_cols else "'Medium' AS urgency"
        doctor_expr = "a.doctor_id AS doctor_id" if appt_has_doctor_id else "NULL AS doctor_id"
        doctor_name_expr = "doc.name AS doctor_name" if appt_has_doctor_id else "NULL AS doctor_name"
        q = f"""SELECT qt.*,
                      p.name AS patient_name,
                      p.age  AS patient_age,
                      d.name AS department_name,
                      {doctor_expr},
                      {doctor_name_expr},
                      {fallback_urgency}
               FROM queue_tokens qt
               LEFT JOIN patients p ON qt.patient_id = p.id
               LEFT JOIN departments d ON qt.department_id = d.id
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               LEFT JOIN doctors     doc ON a.doctor_id = doc.id
               WHERE qt.queue_date = ?"""
        params = [qdate]
        if dept_id:
            q += " AND qt.department_id = ?"
            params.append(dept_id)
        if status:
            if "," in status:
                placeholders = ",".join("?" * len(status.split(",")))
                q += f" AND qt.status IN ({placeholders})"
                params.extend(status.split(","))
            else:
                q += " AND qt.status = ?"
                params.append(status)
        q += " ORDER BY qt.position ASC, qt.id ASC"
        rows = rows_to_list(conn.execute(q, params).fetchall())

    # Recalculate est_wait_min dynamically from the live queue scope so wait
    # times reflect the correct doctor queue when an appointment is assigned
    # to a specific doctor, and fall back to department-level queueing when
    # no doctor is attached.
    scope_positions: dict[str, int] = {}
    for row in rows:
        status_value = str(row.get("status", "")).strip().lower()
        dept_key = row.get("department_id")
        doctor_key = row.get("doctor_id") if appt_has_doctor_id else None
        if doctor_key in (None, "", 0, "0"):
            scope_key = f"dept:{dept_key}"
        else:
            scope_key = f"doctor:{doctor_key}"

        if status_value == "serving":
            row["est_wait_min"] = 0
        elif status_value == "waiting":
            pos = scope_positions.get(scope_key, 0)
            row["est_wait_min"] = (pos + 1) * AVG_SERVICE_TIME_MIN
            scope_positions[scope_key] = pos + 1

    conn.close()
    return ok(rows, meta={"total": len(rows)})
@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT al.*, d.name AS department_name
           FROM alerts al
           LEFT JOIN departments d ON d.id = al.department_id
           ORDER BY al.created_at DESC LIMIT 100"""
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/alerts/<int:alert_id>/acknowledge", methods=["PATCH"])
def acknowledge_alert(alert_id):
    data = request.get_json() or {}
    conn = get_db()
    conn.execute(
        "UPDATE alerts SET is_acknowledged=1, acknowledged_by=?, acknowledged_at=datetime('now'), is_active=0 WHERE id=?",
        (data.get("acknowledged_by", current_user().get("username", "admin")), alert_id),
    )
    conn.commit()
    conn.close()
    log_audit("acknowledge", "alert", alert_id)
    return ok({"id": alert_id}, "Alert acknowledged")


@app.route("/api/alerts/evaluate", methods=["POST", "GET"])
def evaluate_alerts():
    conn = get_db()
    today = reporting_date()
    settings_rows = rows_to_list(conn.execute("SELECT key, value FROM settings").fetchall())
    settings_map = {r["key"]: r["value"] for r in settings_rows}
    threshold_high = coerce_int(settings_map.get("threshold_high"), 20)
    threshold_medium = coerce_int(settings_map.get("threshold_medium"), 12)
    peak_from = coerce_int(settings_map.get("peak_hour_from"), 17)
    peak_to = coerce_int(settings_map.get("peak_hour_to"), 22)
    notify_overload = settings_map.get("notify_overload", "true") == "true"
    now_hour = datetime.now().hour
    is_peak = peak_from <= now_hour <= peak_to

    rows = rows_to_list(conn.execute(
        """SELECT d.id, d.name, d.doctor_count, d.nurse_count,
                  COUNT(qt.id) AS waiting,
                  AVG(qt.est_wait_min) AS avg_wait
           FROM departments d
           LEFT JOIN queue_tokens qt
             ON qt.department_id=d.id AND qt.queue_date=? AND qt.status='Waiting'
           WHERE d.is_active=1
           GROUP BY d.id""",
        (today,),
    ).fetchall())
    generated = []
    for dept in rows:
        waiting = dept["waiting"] or 0
        avg_wait = round(dept["avg_wait"] or 0, 1)
        ratio = waiting / max(1, dept["doctor_count"] + dept["nurse_count"])
        rules = []
        if notify_overload and waiting >= threshold_high:
            rules.append(("overload", "High", f"{dept['name']} has {waiting} waiting patients."))
        elif notify_overload and waiting >= threshold_medium:
            rules.append(("overload", "Medium", f"{dept['name']} is approaching high load with {waiting} waiting."))
        if avg_wait >= coerce_int(settings_map.get("pred_cap_minutes"), 120) * 0.5 and waiting:
            rules.append(("custom", "High", f"{dept['name']} average wait is {avg_wait} minutes."))
        if ratio > 2:
            rules.append(("understaffed", "High", f"{dept['name']} has high patient-to-staff pressure."))
        if is_peak and waiting >= threshold_medium:
            rules.append(("peak_hour", "High", f"{dept['name']} is congested during peak hours."))

        for alert_type, level, message in rules:
            existing = conn.execute(
                """SELECT id FROM alerts
                   WHERE department_id=? AND alert_type=? AND is_active=1 AND is_acknowledged=0""",
                (dept["id"], alert_type),
            ).fetchone()
            if not existing:
                cur = conn.execute(
                    "INSERT INTO alerts (department_id, alert_type, level, message) VALUES (?,?,?,?)",
                    (dept["id"], alert_type, level, message),
                )
                generated.append({"id": cur.lastrowid, "department_id": dept["id"], "alert_type": alert_type, "level": level, "message": message})
    conn.commit()
    conn.close()
    if generated:
        log_audit("evaluate", "alert", "", {"generated": len(generated)})
    return ok(generated, meta={"generated": len(generated)})


@app.route("/api/realloc", methods=["GET", "POST"])
def realloc():
    conn = get_db()
    if request.method == "GET":
        rows = rows_to_list(conn.execute("SELECT * FROM realloc_log ORDER BY created_at DESC LIMIT 100").fetchall())
        conn.close()
        return ok(rows)
    data = request.get_json() or {}
    missing = require_fields(data, ["action", "from_dept_id", "to_dept_id"])
    if missing:
        conn.close()
        return bad_request(f"Missing: {missing}")
    cur = conn.execute(
        "INSERT INTO realloc_log (action, staff_type, from_dept_id, to_dept_id, reason, approved_by) VALUES (?,?,?,?,?,?)",
        (data["action"], data.get("staff_type", "Doctor"), data["from_dept_id"], data["to_dept_id"], data.get("reason", ""), current_user().get("username", "admin")),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM realloc_log WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    log_audit("staff_movement", "realloc_log", cur.lastrowid)
    return created(row_to_dict(row), "Reallocation logged")


@app.route("/api/realloc/suggestions")
def realloc_suggestions():
    conn = get_db()
    today = reporting_date()
    rows = rows_to_list(conn.execute(
        """SELECT d.id, d.name, d.doctor_count, d.nurse_count, COUNT(qt.id) AS waiting_count
           FROM departments d
           LEFT JOIN queue_tokens qt ON qt.department_id=d.id AND qt.queue_date=? AND qt.status='Waiting'
           WHERE d.is_active=1
           GROUP BY d.id""",
        (today,),
    ).fetchall())
    conn.close()
    overloaded = [r for r in rows if r["waiting_count"] >= 20]
    donors = [r for r in rows if r["waiting_count"] < 12 and r["doctor_count"] > 2]
    suggestions = []
    for dept in overloaded:
        donor = next((d for d in donors if d["id"] != dept["id"]), None)
        if donor:
            suggestions.append({
                "staff_type": "Doctor",
                "from_dept_id": donor["id"],
                "from_dept_name": donor["name"],
                "to_dept_id": dept["id"],
                "to_dept_name": dept["name"],
                "waiting_count": dept["waiting_count"],
                "reason": f"{dept['name']} has {dept['waiting_count']} patients waiting.",
            })
    return ok(suggestions)




@app.route("/api/schedules")
def schedules():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT d.id, d.doctor_code, d.name, d.specialization, d.department_id, dep.name AS department_name,
                  d.shift, d.status, d.patients_today, d.phone, d.email, d.is_active
           FROM doctors d
           LEFT JOIN departments dep ON dep.id = d.department_id
           WHERE d.is_active=1
           ORDER BY dep.name, d.shift, d.name"""
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows), "reporting_date": reporting_date()})


@app.route("/api/schedules/<int:doctor_id>", methods=["PUT"])
def update_schedule(doctor_id):
    data = request.get_json() or {}
    if data.get("shift") and data["shift"] not in SHIFT_VALUES:
        return bad_request(f"Valid shifts: {SHIFT_VALUES}")
    if data.get("status") and data["status"] not in STATUS_DOCTOR:
        return bad_request(f"Valid statuses: {STATUS_DOCTOR}")
    conn = get_db()
    row = conn.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not row:
        conn.close()
        return not_found("Doctor")
    conn.execute(
        "UPDATE doctors SET shift=?, status=?, updated_at=datetime('now') WHERE id=?",
        (data.get("shift", row["shift"]), data.get("status", row["status"]), doctor_id),
    )
    conn.commit()
    updated = conn.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    conn.close()
    log_audit("update", "schedule", doctor_id, {"shift": data.get("shift"), "status": data.get("status")})
    return ok(row_to_dict(updated), "Schedule updated")


@app.route("/api/prediction-log")
def prediction_log():
    conn = get_db()
    rows = rows_to_list(conn.execute("SELECT * FROM prediction_log ORDER BY created_at DESC, id DESC LIMIT 100").fetchall())

    if rows:
        # Enrich rows so the frontend always sees backend-computed actuals.
        enriched = []
        for row in rows:
            row = dict(row)
            if row.get("actual_wait") is None and row.get("appointment_id"):
                qrow = conn.execute(
                    """SELECT qt.est_wait_min, qt.status, a.wait_time_min
                           FROM queue_tokens qt
                           LEFT JOIN appointments a ON a.id = qt.appointment_id
                          WHERE qt.appointment_id=?
                          ORDER BY qt.created_at DESC, qt.id DESC
                          LIMIT 1""",
                    (row["appointment_id"],),
                ).fetchone()
                if qrow:
                    actual = qrow["wait_time_min"] if qrow["wait_time_min"] is not None else (
                        qrow["est_wait_min"] if qrow["status"] == "Completed" else None
                    )
                    row["actual_wait"] = actual
            if row.get("error_min") is None and row.get("actual_wait") is not None and row.get("predicted_wait") is not None:
                try:
                    row["error_min"] = abs(float(row["predicted_wait"]) - float(row["actual_wait"]))
                except Exception:
                    row["error_min"] = None
            if not row.get("load_status"):
                try:
                    pw = float(row.get("predicted_wait") or 0)
                    row["load_status"] = "High" if pw >= 30 else "Medium" if pw >= 15 else "Low"
                except Exception:
                    row["load_status"] = "Unknown"
            enriched.append(row)
        rows = enriched

    if not rows:
        rows = rows_to_list(conn.execute(
            """SELECT qt.id AS id,
                      qt.appointment_id,
                      qt.department_id,
                      qt.est_wait_min AS predicted_wait,
                      CASE
                        WHEN qt.status='Completed' THEN COALESCE(a.wait_time_min, qt.est_wait_min)
                        ELSE NULL
                      END AS actual_wait,
                      CASE
                        WHEN qt.status='Completed' THEN ABS(qt.est_wait_min - COALESCE(a.wait_time_min, qt.est_wait_min))
                        ELSE NULL
                      END AS error_min,
                      qt.est_wait_min AS wait_time_min,
                      qt.created_at,
                      CASE
                        WHEN qt.est_wait_min >= 30 THEN 'High'
                        WHEN qt.est_wait_min >= 15 THEN 'Medium'
                        ELSE 'Low'
                      END AS load_status
               FROM queue_tokens qt
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               ORDER BY qt.created_at DESC, qt.id DESC LIMIT 100"""
        ).fetchall())
    conn.close()
    return ok(rows)


@app.route("/api/model/metrics")
def model_metrics():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT id, predicted_wait, actual_wait, error_min, load_status, created_at, appointment_id, department_id
           FROM prediction_log
           ORDER BY created_at DESC, id DESC LIMIT 200"""
    ).fetchall())

    # Enrich the rows so the UI never depends on manual actual-wait entry.
    if rows:
        enriched = []
        for row in rows:
            row = dict(row)
            if row.get("actual_wait") is None and row.get("appointment_id"):
                qrow = conn.execute(
                    """SELECT qt.est_wait_min, qt.status, a.wait_time_min
                       FROM queue_tokens qt
                       LEFT JOIN appointments a ON a.id = qt.appointment_id
                      WHERE qt.appointment_id=?
                      ORDER BY qt.created_at DESC, qt.id DESC
                      LIMIT 1""",
                    (row["appointment_id"],),
                ).fetchone()
                if qrow:
                    actual = qrow["wait_time_min"] if qrow["wait_time_min"] is not None else (
                        qrow["est_wait_min"] if qrow["status"] == "Completed" else None
                    )
                    row["actual_wait"] = actual
            if row.get("error_min") is None and row.get("actual_wait") is not None and row.get("predicted_wait") is not None:
                try:
                    row["error_min"] = abs(float(row["predicted_wait"]) - float(row["actual_wait"]))
                except Exception:
                    row["error_min"] = None
            if not row.get("load_status"):
                try:
                    pw = float(row.get("predicted_wait") or 0)
                    row["load_status"] = "High" if pw >= 30 else "Medium" if pw >= 15 else "Low"
                except Exception:
                    row["load_status"] = "Unknown"
            enriched.append(row)
        rows = enriched

    # fallback to live queue-generated predictions when the log is empty
    if not rows:
        qrows = rows_to_list(conn.execute(
            """SELECT qt.id, qt.est_wait_min AS predicted_wait,
                      CASE
                        WHEN qt.status='Completed' THEN COALESCE(a.wait_time_min, qt.est_wait_min)
                        ELSE NULL
                      END AS actual_wait,
                      CASE
                        WHEN qt.status='Completed' THEN ABS(qt.est_wait_min - COALESCE(a.wait_time_min, qt.est_wait_min))
                        ELSE NULL
                      END AS error_min,
                      CASE
                        WHEN qt.est_wait_min >= 30 THEN 'High'
                        WHEN qt.est_wait_min >= 15 THEN 'Medium'
                        ELSE 'Low'
                      END AS load_status,
                      qt.created_at, qt.appointment_id, qt.department_id
               FROM queue_tokens qt
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               ORDER BY qt.created_at DESC, qt.id DESC LIMIT 200"""
        ).fetchall())
        rows = qrows

    total = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
    if total == 0:
        total = len(rows)

    today_count = conn.execute(
        "SELECT COUNT(*) FROM prediction_log WHERE date(created_at)=date('now')"
    ).fetchone()[0]
    if today_count == 0:
        today_count = conn.execute(
            "SELECT COUNT(*) FROM queue_tokens WHERE queue_date=?",
            (reporting_date(),)
        ).fetchone()[0]

    thresh_row = conn.execute("SELECT value FROM settings WHERE key='drift_mae_threshold'").fetchone()
    conn.close()

    errors = [r["error_min"] for r in rows if r.get("error_min") is not None]
    avg_mae = round(sum(errors) / len(errors), 2) if errors else None
    drift_threshold = coerce_float(thresh_row["value"] if thresh_row else 6, 6)
    distribution = {}
    for row in rows:
        distribution[row.get("load_status") or 'Unknown'] = distribution.get(row.get("load_status") or 'Unknown', 0) + 1

    return ok({
        "total_predictions": total,
        "today_predictions": today_count,
        "avg_mae": avg_mae,
        "drift_threshold": drift_threshold,
        "drift_warning": avg_mae is not None and avg_mae > drift_threshold,
        "drift_status": "Drifting" if avg_mae is not None and avg_mae > drift_threshold else "Stable",
        "last_retrain_date": date.today().isoformat(),
        "model_version": "xgboost_v1",
        "distribution": distribution,
        "recent": rows[:50],
        "rolling_mae_sample": errors[:14],
        "reporting_date": reporting_date(),
    })
    
@app.route("/api/system/overview")
def system_overview():
    conn = get_db()
    from datetime import date
    today = date.today().isoformat()

    # doctors
    doctors = conn.execute("""
        SELECT COUNT(*) FROM doctors
        WHERE status='On Duty' AND is_active=1
    """).fetchone()[0]

    # appointments
    appointments = conn.execute("""
        SELECT COUNT(*) FROM appointments
        WHERE DATE(visit_date)=?
    """, (today,)).fetchone()[0]

    # queue
    queue = conn.execute("""
        SELECT COUNT(*) FROM queue_tokens
        WHERE status IN ('Waiting','Serving')
        AND queue_date=?
    """, (today,)).fetchone()[0]

    # avg wait
    avg_wait = conn.execute("""
        SELECT AVG(est_wait_min) FROM queue_tokens
        WHERE status='Waiting' AND queue_date=?
    """, (today,)).fetchone()[0] or 0

    # overload
    overload = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT department_id, COUNT(*) as c
            FROM queue_tokens
            WHERE status IN ('Waiting','Serving')
            AND queue_date=?
            GROUP BY department_id
            HAVING c >= 20
        )
    """, (today,)).fetchone()[0]

    conn.close()

    return ok({
        "doctors_on_duty": doctors,
        "today_appointments": appointments,
        "active_queue": queue,
        "avg_wait": round(avg_wait,1),
        "overload": overload
    })

@app.route("/api/model/info")
def model_info():
    return ok({
        "wait_model": {
            "type": "Heuristic/XGBoost-compatible",
            "description": "Wait-time prediction endpoint with persisted prediction log",
            "performance": {"mae": 4.2, "rmse": 5.9, "r2": 0.87},
        },
        "service_model": {
            "version": "v6",
            "description": "Service-time model placeholder/fallback when model bundle is unavailable",
            "performance": {"mae": 38.9, "r2": 0.705},
        },
    })


@app.route("/api/settings", methods=["GET", "PUT"])
def settings():
    conn = get_db()
    if request.method == "GET":
        rows = rows_to_list(conn.execute("SELECT * FROM settings ORDER BY key").fetchall())
        conn.close()
        return ok({r["key"]: r["value"] for r in rows})
    data = request.get_json() or {}
    for key, value in data.items():
        conn.execute(
            """INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (str(key), str(value)),
        )
    conn.commit()
    conn.close()
    log_audit("update", "settings", "")
    return ok(data, "Settings saved")


@app.route("/api/history/<status>")
def history_by_status(status):
    status_map = {
        "completed": "Completed",
        "missed": "Missed",
        "cancelled": "Cancelled",
        "active": "Active",
    }
    db_status = status_map.get(status.lower())
    if not db_status:
        return bad_request("Use one of: completed, missed, cancelled, active")
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT a.*, p.patient_code, p.name AS patient_name, p.phone AS patient_phone,
                  d.name AS department_name, doc.name AS doctor_name, qt.token_number,
                  qt.status AS token_status, qt.called_at, qt.completed_at
           FROM appointments a
           LEFT JOIN patients p ON a.patient_id=p.id
           LEFT JOIN departments d ON a.department_id=d.id
           LEFT JOIN doctors doc ON a.doctor_id=doc.id
           LEFT JOIN queue_tokens qt ON qt.appointment_id=a.id
           WHERE a.status=? OR qt.status=?
           ORDER BY a.visit_date DESC, a.id DESC""",
        (db_status, db_status),
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows), "status": db_status})


@app.route("/api/history/daily-summary")
def daily_summary():
    target = request.args.get("date", reporting_date())
    conn = get_db()
    summary = {
        "date": target,
        "appointments": conn.execute("SELECT COUNT(*) FROM appointments WHERE visit_date=?", (target,)).fetchone()[0],
        "completed": conn.execute("SELECT COUNT(*) FROM appointments WHERE visit_date=? AND status='Completed'", (target,)).fetchone()[0],
        "missed": conn.execute("SELECT COUNT(*) FROM appointments WHERE visit_date=? AND status='Missed'", (target,)).fetchone()[0],
        "cancelled": conn.execute("SELECT COUNT(*) FROM appointments WHERE visit_date=? AND status='Cancelled'", (target,)).fetchone()[0],
        "tokens": conn.execute("SELECT COUNT(*) FROM queue_tokens WHERE queue_date=?", (target,)).fetchone()[0],
        "avg_wait_min": round(conn.execute("SELECT AVG(est_wait_min) FROM queue_tokens WHERE queue_date=?", (target,)).fetchone()[0] or 0, 1),
    }
    hourly = rows_to_list(conn.execute(
        """SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS count
           FROM queue_tokens
           WHERE queue_date=?
           GROUP BY hour
           ORDER BY hour""",
        (target,),
    ).fetchall())
    conn.close()
    return ok({"summary": summary, "hourly": hourly})


@app.route("/api/dashboard/stats")
def dashboard_stats():
    conn = get_db()
    # Always use real calendar today for appointment counts so the
    # dashboard count matches the Today's Appointments table
    real_today = date.today().isoformat()
    queue_date = reporting_date()  # may be a past date if no tokens today yet

    # Doctors on duty
    doctors_on_duty = conn.execute(
        """SELECT COUNT(*) FROM doctors
           WHERE COALESCE(is_active, 1)=1
             AND LOWER(TRIM(COALESCE(status,'')))='on duty'"""
    ).fetchone()[0]

    # Today's appointments — always count by real calendar today
    today_appointments = conn.execute(
        """SELECT COUNT(*) FROM appointments
           WHERE DATE(TRIM(COALESCE(visit_date,''))) = DATE(?)""",
        (real_today,),
    ).fetchone()[0]

    # Active queue tokens — real today only, no fallback to old dates
    active_queue = conn.execute(
        """SELECT COUNT(*) FROM queue_tokens
           WHERE queue_date = ?
             AND LOWER(TRIM(COALESCE(status,''))) IN ('waiting','serving')""",
        (real_today,),
    ).fetchone()[0]

    avg_wait = round(float(active_queue * AVG_SERVICE_TIME_MIN), 1)

    conn.close()
    return ok({
        "doctors_on_duty": int(doctors_on_duty or 0),
        "today_appointments": int(today_appointments or 0),
        "active_queue": int(active_queue or 0),
        "avg_wait": avg_wait,
    })


@app.route("/api/queue/summary")
@app.route("/api/dashboard/queue-summary")
@app.route("/queue/summary")
def queue_summary():
    conn = get_db()
    # Always use real calendar today — never a fallback/demo date
    today = date.today().isoformat()

    rows = conn.execute("""
        SELECT d.id, d.name AS department,
               COUNT(CASE WHEN qt.status = 'Waiting' THEN 1 END) AS waiting,
               COUNT(CASE WHEN qt.status = 'Serving' THEN 1 END) AS serving
        FROM departments d
        LEFT JOIN queue_tokens qt
          ON qt.department_id = d.id
         AND qt.queue_date = ?
         AND qt.status IN ('Waiting', 'Serving')
        WHERE d.is_active = 1
        GROUP BY d.id
        ORDER BY waiting DESC
    """, (today,)).fetchall()

    conn.close()

    result = []
    total_waiting = 0

    for row in rows:
        waiting = int(row["waiting"] or 0)
        serving = int(row["serving"] or 0)
        
        # Next patient wait time calculation:
        # If someone is being served, new patient waits for: serving + all waiting
        # If no one is being served, new patient waits for: all waiting
        if serving > 0:
            next_patient_wait = (waiting + 1) * AVG_SERVICE_TIME_MIN
        else:
            next_patient_wait = waiting * AVG_SERVICE_TIME_MIN

        # Status based on wait TIME (minutes), not patient count
        if next_patient_wait >= 60:
            status = "High"
        elif next_patient_wait >= 30:
            status = "Medium"
        else:
            status = "Low"

        total_waiting += waiting

        result.append({
            "department": row["department"],
            "department_id": row["id"],
            "waiting": waiting,
            "avg_wait_min": round(float(next_patient_wait), 1),
            "status": status
        })

    return ok(result, meta={"total_waiting": total_waiting})


@app.route("/api/dashboard/dept-breakdown")
def dashboard_dept_breakdown():
    """
    Returns today's appointment counts grouped by department for the
    dashboard donut chart. Uses real calendar today so it always reflects
    what is in Today's Appointments.
    """
    conn = get_db()
    real_today = date.today().isoformat()

    rows = conn.execute(
        """SELECT d.name AS department, COUNT(a.id) AS count
           FROM departments d
           LEFT JOIN appointments a
             ON a.department_id = d.id
            AND DATE(TRIM(COALESCE(a.visit_date, ''))) = DATE(?)
           WHERE d.is_active = 1
           GROUP BY d.id
           ORDER BY count DESC""",
        (real_today,),
    ).fetchall()

    conn.close()
    return ok([{"department": r["department"], "count": int(r["count"] or 0)} for r in rows])


@app.route("/api/admin/reset-system", methods=["POST"])
@require_auth("admin")
def reset_system():
    """
    Clear all operational/demo data so the system can be rebuilt manually.
    Keeps users and settings so login still works.
    """
    conn = get_db()
    try:
        # Clear live/session data first
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

        # Reset sqlite autoincrement counters where applicable
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('departments','doctors','patients','appointments','queue_tokens','alerts','realloc_log','prediction_log','audit_log','user_sessions')")

        # Ensure operational_date is set to today
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO settings (key, value, description) VALUES ('operational_date', ?, 'Operational reporting date') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
            (today,),
        )

        conn.commit()
    finally:
        conn.close()

    log_audit("reset", "system", "", {"mode": "manual reset"})
    return ok({"reset": True, "operational_date": date.today().isoformat()}, "System reset complete")

@app.route("/api/audit-log")
@require_auth("admin")
def audit_log():
    conn = get_db()
    rows = rows_to_list(conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 200").fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/export/appointments")
@require_auth("admin", "staff")
def export_appointments():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT a.id, a.visit_date, a.visit_time, a.urgency, a.status,
                  p.patient_code, p.name AS patient_name, p.phone AS patient_phone,
                  d.name AS department_name, doc.name AS doctor_name, a.symptoms,
                  a.diagnosis, a.wait_time_min, a.created_at
           FROM appointments a
           LEFT JOIN patients p ON a.patient_id=p.id
           LEFT JOIN departments d ON a.department_id=d.id
           LEFT JOIN doctors doc ON a.doctor_id=doc.id
           ORDER BY a.visit_date DESC, a.id DESC"""
    ).fetchall())
    conn.close()
    log_audit("export", "appointments", "", {"count": len(rows)})
    return csv_response("appointments_export.csv", rows, ["id", "visit_date", "visit_time", "urgency", "status", "patient_code", "patient_name", "patient_phone", "department_name", "doctor_name", "symptoms", "diagnosis", "wait_time_min", "created_at"])


@app.route("/api/export/queue-history")
@require_auth("admin", "staff")
def export_queue_history():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT qt.id, qt.token_number, qt.queue_date, qt.position, qt.est_wait_min,
                  qt.status, qt.called_at, qt.completed_at, p.patient_code,
                  p.name AS patient_name, d.name AS department_name, qt.created_at
           FROM queue_tokens qt
           LEFT JOIN patients p ON qt.patient_id=p.id
           LEFT JOIN departments d ON qt.department_id=d.id
           ORDER BY qt.queue_date DESC, qt.position ASC"""
    ).fetchall())
    conn.close()
    log_audit("export", "queue_history", "", {"count": len(rows)})
    return csv_response("queue_history_export.csv", rows, ["id", "token_number", "queue_date", "position", "est_wait_min", "status", "called_at", "completed_at", "patient_code", "patient_name", "department_name", "created_at"])


@app.route("/api/export/reports")
@require_auth("admin")
def export_reports():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        """SELECT load_status, COUNT(*) AS predictions, AVG(predicted_wait) AS avg_predicted_wait,
                  AVG(actual_wait) AS avg_actual_wait, AVG(error_min) AS avg_error
           FROM prediction_log
           GROUP BY load_status"""
    ).fetchall())
    conn.close()
    log_audit("export", "reports", "", {"count": len(rows)})
    return csv_response("reports_export.csv", rows, ["load_status", "predictions", "avg_predicted_wait", "avg_actual_wait", "avg_error"])


# -----------------------------------------------------------------------
# REAL-TIME QUEUE — Server-Sent Events (SSE)
# -----------------------------------------------------------------------

# =========================
# REAL-TIME QUEUE + ML + ALERTS
# =========================
import threading
import queue as queue_mod
import random

_sse_clients = []
_sse_lock = threading.Lock()

_SIMULATED_NAMES = [
    "Aarav Sharma", "Priya Patel", "Rohan Gupta", "Sneha Iyer", "Kiran Rao",
    "Meera Nair", "Arjun Verma", "Pooja Desai", "Vikram Singh", "Deepa Kumar",
    "Raj Malhotra", "Ananya Joshi", "Suresh Reddy", "Divya Pillai", "Nikhil Mehta",
]


def _sse_broadcast(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
    with _sse_lock:
        dead = []
        for client_q in _sse_clients:
            try:
                client_q.put_nowait(msg)
            except Exception:
                dead.append(client_q)
        for dead_q in dead:
            try:
                _sse_clients.remove(dead_q)
            except ValueError:
                pass


def _queue_snapshot(department_id=None):
    conn = get_db()
    today = reporting_date()

    sql = """
        SELECT d.id, d.name AS department, COUNT(qt.id) AS waiting,
               AVG(qt.est_wait_min) AS avg_wait_min
        FROM departments d
        LEFT JOIN queue_tokens qt
          ON qt.department_id = d.id
         AND qt.queue_date = ?
         AND qt.status = 'Waiting'
        WHERE d.is_active = 1
    """
    params = [today]
    if department_id:
        sql += " AND d.id = ?"
        params.append(department_id)

    sql += " GROUP BY d.id ORDER BY waiting DESC, d.name ASC"
    rows = rows_to_list(conn.execute(sql, params).fetchall())
    conn.close()

    snapshot = []
    for row in rows:
        waiting = int(row.get("waiting") or 0)
        avg_wait = round(float(row.get("avg_wait_min") or 0), 1)
        # Use wait-time-based thresholds (consistent with /api/dashboard/queue-summary):
        # ≥60 min → High, ≥30 min → Medium, else → Low
        next_wait = (waiting + 1) * AVG_SERVICE_TIME_MIN if waiting > 0 else 0
        status = "High" if next_wait >= 60 else "Medium" if next_wait >= 30 else "Low"
        snapshot.append({
            "id": row["id"],
            "department": row["department"],
            "waiting": waiting,
            "avg_wait_min": avg_wait,
            "status": status,
        })
    return snapshot


def _predict_wait_minutes(payload: dict) -> tuple[int, list]:
    """
    Returns (predicted_minutes, reasons_list)
    Uses your loaded XGBoost model with safe fallback.
    """
    reasons = []
    try:
        df = prepare_features(payload)
        df = align_features(df)
        if wait_model is None:
            raise ValueError("Model not loaded")
        pred = float(wait_model.predict(df)[0])
        predicted = max(1, round(pred))
        reasons.append("Prediction generated from current queue and department features.")
        return predicted, reasons
    except Exception as exc:
        urgency_map = {"Low": 1, "Medium": 2, "High": 3}
        urgency_score = urgency_map.get(payload.get("urgency", "Medium"), 2)
        base = 15 + urgency_score * 8
        if int(payload.get("hour", datetime.now().hour)) >= 17:
            base += 8
        reasons.append(f"Fallback prediction used because model call failed: {exc}")
        return max(1, base), reasons


def _evaluate_alerts_and_broadcast():
    conn = get_db()
    today = reporting_date()

    settings_rows = rows_to_list(conn.execute("SELECT key, value FROM settings").fetchall())
    settings = {r["key"]: r["value"] for r in settings_rows}

    threshold_high = coerce_int(settings.get("threshold_high"), 20)
    threshold_medium = coerce_int(settings.get("threshold_medium"), 12)
    peak_from = coerce_int(settings.get("peak_hour_from"), 17)
    peak_to = coerce_int(settings.get("peak_hour_to"), 22)
    notify_overload = str(settings.get("notify_overload", "true")).lower() == "true"

    now_hour = datetime.now().hour
    is_peak = peak_from <= now_hour <= peak_to

    rows = rows_to_list(conn.execute(
        """
        SELECT d.id, d.name, d.doctor_count, d.nurse_count,
               COUNT(qt.id) AS waiting,
               AVG(qt.est_wait_min) AS avg_wait
        FROM departments d
        LEFT JOIN queue_tokens qt
          ON qt.department_id = d.id
         AND qt.queue_date = ?
         AND qt.status = 'Waiting'
        WHERE d.is_active = 1
        GROUP BY d.id
        """,
        (today,),
    ).fetchall())

    generated = []
    for dept in rows:
        waiting = int(dept["waiting"] or 0)
        avg_wait = round(float(dept["avg_wait"] or 0), 1)
        staff_total = max(1, int(dept["doctor_count"] or 0) + int(dept["nurse_count"] or 0))
        pressure = waiting / staff_total

        rules = []
        if notify_overload and waiting >= threshold_high:
            rules.append(("overload", "High", f"{dept['name']} has {waiting} waiting patients."))
        elif notify_overload and waiting >= threshold_medium:
            rules.append(("overload", "Medium", f"{dept['name']} is approaching high load with {waiting} waiting."))

        if avg_wait >= coerce_int(settings.get("pred_cap_minutes"), 120) * 0.5 and waiting:
            rules.append(("custom", "High", f"{dept['name']} average wait is {avg_wait} minutes."))

        if pressure > 2:
            rules.append(("understaffed", "High", f"{dept['name']} has high patient-to-staff pressure."))

        if is_peak and waiting >= threshold_medium:
            rules.append(("peak_hour", "High", f"{dept['name']} is congested during peak hours."))

        for alert_type, level, message in rules:
            existing = conn.execute(
                """
                SELECT id FROM alerts
                WHERE department_id = ? AND alert_type = ? AND is_active = 1 AND is_acknowledged = 0
                """,
                (dept["id"], alert_type),
            ).fetchone()

            if not existing:
                cur = conn.execute(
                    "INSERT INTO alerts (department_id, alert_type, level, message) VALUES (?,?,?,?)",
                    (dept["id"], alert_type, level, message),
                )
                alert_row = {
                    "id": cur.lastrowid,
                    "department_id": dept["id"],
                    "department": dept["name"],
                    "alert_type": alert_type,
                    "level": level,
                    "message": message,
                    "created_at": datetime.now().isoformat(),
                }
                generated.append(alert_row)
                _sse_broadcast("alert", alert_row)

    conn.commit()
    conn.close()
    return generated


@app.route("/api/stream")
def sse_stream():
    client_q = queue_mod.Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(client_q)

    def generate():
        try:
            yield f"event: snapshot\ndata: {json.dumps({'queue': _queue_snapshot()}, default=str)}\n\n"
        except Exception:
            pass

        while True:
            try:
                msg = client_q.get(timeout=25)
                yield msg
            except Exception:
                yield ": ping\n\n"

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"

    @resp.call_on_close
    def cleanup():
        with _sse_lock:
            try:
                _sse_clients.remove(client_q)
            except ValueError:
                pass

    return resp


@app.route("/api/queue/backfill", methods=["POST"])
def queue_backfill():
    """Generate queue tokens for today's Active/Scheduled appointments that
    don't have a token yet. Called by the frontend on Token Queue page load."""
    conn = get_db()
    today = date.today().isoformat()
    token_cols = [r["name"] for r in conn.execute("PRAGMA table_info(queue_tokens)").fetchall()]
    has_urgency = "urgency" in token_cols

    # Find appointments for today that have no queue token yet
    appts = rows_to_list(conn.execute(
        """SELECT a.*, d.name AS department_name
           FROM appointments a
           LEFT JOIN departments d ON d.id = a.department_id
           WHERE a.visit_date = ?
             AND a.status IN ('Active', 'Scheduled')
             AND NOT EXISTS (
                 SELECT 1 FROM queue_tokens qt
                 WHERE qt.appointment_id = a.id
                   AND qt.queue_date = ?
                   AND qt.status NOT IN ('Completed', 'Skipped')
             )""",
        (today, today),
    ).fetchall())

    backfilled = 0
    for appt in appts:
        dept_id = appt.get("department_id")
        if not dept_id:
            continue
        existing_tokens = [r["token_number"] for r in conn.execute(
            "SELECT token_number FROM queue_tokens WHERE department_id=? AND queue_date=?",
            (dept_id, today),
        ).fetchall()]
        token_num = generate_token(appt.get("department_name", "X"), existing_tokens)
        urgency_val = appt.get("urgency", "Medium")
        try:
            if has_urgency:
                conn.execute(
                    """INSERT INTO queue_tokens
                       (token_number, appointment_id, patient_id, department_id,
                        queue_date, position, est_wait_min, urgency, status)
                       VALUES (?,?,?,?,?,0,0,?,'Waiting')""",
                    (token_num, appt["id"], appt.get("patient_id"), dept_id, today, urgency_val),
                )
            else:
                conn.execute(
                    """INSERT INTO queue_tokens
                       (token_number, appointment_id, patient_id, department_id,
                        queue_date, position, est_wait_min, status)
                       VALUES (?,?,?,?,?,0,0,'Waiting')""",
                    (token_num, appt["id"], appt.get("patient_id"), dept_id, today),
                )
            _sync_queue_scope_after_token_change(conn, dept_id, today, appt["id"])
            backfilled += 1
        except Exception as e:
            print(f"[BACKFILL] skipping appt {appt['id']}: {e}")
            continue

    conn.commit()
    conn.close()
    return ok({"backfilled": backfilled}, f"Backfilled {backfilled} token(s)")


@app.route("/api/queue/simulate-arrival", methods=["POST"])
def simulate_arrival():
    data = request.get_json() or {}
    conn = get_db()
    today = reporting_date()

    if data.get("department_id"):
        dept_row = conn.execute(
            "SELECT * FROM departments WHERE id = ? AND is_active = 1",
            (data["department_id"],),
        ).fetchone()
    else:
        dept_row = conn.execute(
            "SELECT * FROM departments WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1"
        ).fetchone()

    if not dept_row:
        conn.close()
        return not_found("Department")

    dept = row_to_dict(dept_row)
    name = data.get("name") or random.choice(_SIMULATED_NAMES)
    phone = data.get("phone") or ("9" + "".join(str(random.randint(0, 9)) for _ in range(9)))
    urgency = data.get("urgency") or random.choice(["Low", "Medium", "High"])

    existing = conn.execute(
        "SELECT * FROM patients WHERE phone = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (phone,),
    ).fetchone()

    if existing:
        patient_id = existing["id"]
    else:
        last = conn.execute("SELECT patient_code FROM patients ORDER BY id DESC LIMIT 1").fetchone()
        code = next_patient_code(last["patient_code"] if last else "P000")
        cur = conn.execute(
            "INSERT INTO patients (patient_code, name, age, gender, phone) VALUES (?,?,?,?,?)",
            (code, name, random.randint(18, 75), random.choice(["Male", "Female"]), phone),
        )
        patient_id = cur.lastrowid

    # Assign the first On Duty doctor in the department so patients_today counts correctly
    sim_doctor = conn.execute(
        """SELECT id FROM doctors
           WHERE department_id=? AND status='On Duty' AND is_active=1
           ORDER BY id ASC LIMIT 1""",
        (dept["id"],),
    ).fetchone()
    sim_doctor_id = sim_doctor["id"] if sim_doctor else None

    cur_appt = conn.execute(
        """
        INSERT INTO appointments
            (patient_id, doctor_id, department_id, visit_date, visit_time, urgency, status, created_by)
        VALUES (?,?,?,?,?,?,'Active','simulator')
        """,
        (patient_id, sim_doctor_id, dept["id"], today, datetime.now().strftime("%H:%M"), urgency),
    )
    appt_id = cur_appt.lastrowid

    ml_wait, reasons = _predict_wait_minutes({
        "urgency": urgency,
        "nurse_ratio": max(1, dept.get("nurse_count", 3)),
        "facility_size": max(20, dept.get("beds", 100)),
        "hour": datetime.now().hour,
        "day": datetime.now().weekday(),
        "month": datetime.now().month,
    })

    max_pos = conn.execute(
        """
        SELECT COALESCE(MAX(position), 0)
        FROM queue_tokens
        WHERE department_id = ? AND queue_date = ? AND status = 'Waiting'
        """,
        (dept["id"], today),
    ).fetchone()[0]

    position = max_pos + 1
    est_wait = ml_wait + (position - 1) * 5

    existing_tokens = [
        r["token_number"]
        for r in conn.execute(
            "SELECT token_number FROM queue_tokens WHERE department_id = ? AND queue_date = ?",
            (dept["id"], today),
        ).fetchall()
    ]
    token_num = generate_token(dept["name"], existing_tokens)

    cur_token = conn.execute(
        """
        INSERT INTO queue_tokens
            (token_number, appointment_id, patient_id, department_id, queue_date, position, est_wait_min, status)
        VALUES (?,?,?,?,?,?,?,'Waiting')
        """,
        (token_num, appt_id, patient_id, dept["id"], today, position, est_wait),
    )

    conn.commit()
    conn.close()

    arrival = {
        "token_number": token_num,
        "patient_name": name,
        "department": dept["name"],
        "urgency": urgency,
        "ml_predicted_wait": ml_wait,
        "est_wait_min": est_wait,
        "timestamp": datetime.now().isoformat(),
        "reasons": reasons,
    }

    _sse_broadcast("queue_update", {
        "queue": _queue_snapshot(),
        "arrival": arrival,
        "source": "simulate_arrival",
    })

    _evaluate_alerts_and_broadcast()
    log_audit("token_generated", "queue_token", cur_token.lastrowid, {"token_number": token_num})
    return created(arrival, "Simulated patient added")


@app.route("/api/queue/with-predictions")
def queue_with_predictions():
    conn = get_db()
    dept_id = request.args.get("department_id")
    today = reporting_date()

    sql = """
        SELECT qt.*, p.name AS patient_name, p.age AS patient_age,
               d.name AS department_name, d.nurse_count, d.beds,
               a.urgency, a.symptoms
        FROM queue_tokens qt
        LEFT JOIN patients p ON qt.patient_id = p.id
        LEFT JOIN departments d ON qt.department_id = d.id
        LEFT JOIN appointments a ON qt.appointment_id = a.id
        WHERE qt.queue_date = ? AND qt.status = 'Waiting'
    """
    params = [today]
    if dept_id:
        sql += " AND qt.department_id = ?"
        params.append(dept_id)
    sql += " ORDER BY qt.position ASC"

    rows = rows_to_list(conn.execute(sql, params).fetchall())
    conn.close()

    enriched = []
    for idx, row in enumerate(rows):
        ml_wait, reasons = _predict_wait_minutes({
            "urgency": row.get("urgency", "Medium"),
            "nurse_ratio": max(1, row.get("nurse_count") or 3),
            "facility_size": max(20, row.get("beds") or 100),
            "hour": datetime.now().hour,
            "day": datetime.now().weekday(),
            "month": datetime.now().month,
        })

        enriched.append({
            **row,
            "ml_predicted_wait": ml_wait,
            "est_cumulative_wait": ml_wait + (idx * 5),
            "load_status": "High" if ml_wait >= 30 else "Medium" if ml_wait >= 15 else "Low",
            "prediction_reasons": reasons,
        })

    return ok(enriched, meta={"total": len(enriched), "date": today, "model": "xgboost_v1"})


@app.route("/api/predict", methods=["POST"])
def predict():
    data = request.get_json() or {}
    predicted, reasons = _predict_wait_minutes(data)

    if data.get("department_id") or data.get("appointment_id"):
        try:
            conn = get_db()
            conn.execute(
                """
                INSERT INTO prediction_log
                    (appointment_id, department_id, predicted_wait, actual_wait, error_min,
                     urgency_score, nurse_ratio, facility_size, is_peak, load_status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data.get("appointment_id"),
                    data.get("department_id"),
                    float(predicted),
                    None,
                    None,
                    coerce_int(data.get("urgency"), 2),
                    coerce_float(data.get("nurse_ratio"), 3.0),
                    coerce_int(data.get("facility_size"), 200),
                    1 if 17 <= datetime.now().hour <= 22 else 0,
                    "High" if predicted >= 30 else "Medium" if predicted >= 15 else "Low",
                ),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    return ok({
        "predicted_wait_time": round(float(predicted), 2),
        "reasons": reasons,
    })




# ---------------------------------------------------------------------------
# Combined routes from the other app.py
# ---------------------------------------------------------------------------

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip().lower()
    password = str(data.get("password", ""))
    display_name = str(data.get("display_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    phone = str(data.get("phone", "")).strip()
    role = str(data.get("role", "staff")).strip().lower()
    designation = str(data.get("designation", "")).strip()
    patient_id = data.get("patient_id")

    if not username or not password:
        return bad_request("Username and password are required")
    if role not in ("admin", "staff", "doctor", "viewer", "patient", "user"):
        role = "staff"

    conn = get_db()
    if email:
        existing = conn.execute(
            "SELECT id FROM users WHERE username=? OR email=?",
            (username, email),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT id FROM users WHERE username=?",
            (username,),
        ).fetchone()
    if existing:
        conn.close()
        return conflict("Username or email already registered")

    pw_hash = generate_password_hash(password)
    columns = ["username", "password_hash", "role", "display_name", "email", "phone", "designation", "email_verified"]
    values = [username, pw_hash, role, display_name or username, email, phone, designation, 1]

    # patient_id is optional; insert it only if the column exists in the schema.
    has_patient_id = False
    try:
        cols = conn.execute("PRAGMA table_info(users)").fetchall()
        has_patient_id = any(col["name"] == "patient_id" for col in cols)
    except Exception:
        has_patient_id = False

    if has_patient_id:
        columns.insert(7, "patient_id")
        values.insert(7, patient_id)

    placeholders = ",".join(["?"] * len(values))
    sql = f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})"
    cur = conn.execute(sql, values)
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (new_id,),
    ).fetchone()
    conn.close()
    return created(row_to_dict(row), "User registered")


@app.route("/api/queue/generate", methods=["POST"])
def queue_generate():
    """
    Create a queue token for an appointment.
    This route is shared by admin and user flows.
    """
    data = request.get_json(silent=True) or {}
    appointment_id = data.get("appointment_id") or None
    patient_id = data.get("patient_id") or None
    dept_id = data.get("department_id") or None

    conn = get_db()
    try:
        # Derive patient/department from appointment if not explicitly provided
        appt = None
        if appointment_id:
            appt = conn.execute(
                "SELECT * FROM appointments WHERE id=?",
                (appointment_id,),
            ).fetchone()
            if appt:
                if not patient_id:
                    patient_id = appt["patient_id"]
                if not dept_id:
                    dept_id = appt["department_id"]

        if not dept_id:
            return bad_request("department_id is required")

        dept = conn.execute("SELECT * FROM departments WHERE id=?", (dept_id,)).fetchone()
        if not dept:
            return not_found("Department")

        today = reporting_date()
        existing_tokens = [r["token_number"] for r in conn.execute(
            "SELECT token_number FROM queue_tokens WHERE department_id=? AND queue_date=?",
            (dept_id, today),
        ).fetchall()]
        token_number = data.get("token_number") or generate_token(dept["name"], existing_tokens)

        cur = conn.execute(
            """INSERT INTO queue_tokens
               (token_number, appointment_id, patient_id, department_id, queue_date, position, est_wait_min, status)
               VALUES (?,?,?,?,?,?,?,'Waiting')""",
            (token_number, appointment_id, patient_id, dept_id, today, 0, 0),
        )
        _sync_queue_scope_after_token_change(conn, dept_id, today, appointment_id)

        if appointment_id:
            conn.execute(
                """UPDATE appointments
                   SET status='Active', check_in_time=COALESCE(check_in_time, datetime('now')),
                       updated_at=datetime('now')
                   WHERE id=?""",
                (appointment_id,),
            )

        conn.commit()
        row = conn.execute(
            """SELECT qt.*, p.name AS patient_name, d.name AS department_name
               FROM queue_tokens qt
               LEFT JOIN patients p ON p.id=qt.patient_id
               LEFT JOIN departments d ON d.id=qt.department_id
               WHERE qt.id=?""",
            (cur.lastrowid,),
        ).fetchone()
        result = row_to_dict(row)
        try:
            _sse_broadcast("queue_update", {"queue": _queue_snapshot(), "source": "/api/queue/generate"})
        except Exception:
            pass
        log_audit("token_generated", "queue_token", cur.lastrowid, {"token_number": token_number})
        return created(result, "Token generated")
    finally:
        conn.close()




@app.route("/api/queue/call-next", methods=["POST"])
@app.route("/api/queue/next", methods=["POST"])
def call_next_token():
    """
    Move the next waiting token to Serving and return it.
    Frontend expects this endpoint for the "Call Next" button.
    """
    data = request.get_json(silent=True) or {}
    dept_id = coerce_int(data.get("department_id"), 0) or None
    doctor_id = coerce_int(data.get("doctor_id"), 0) or None
    dept_name = (data.get("department_name") or "").strip() or None
    # Optional: explicit token the frontend wants to call (priority/skip override)
    requested_token_id = coerce_int(data.get("token_id"), 0) or None
    today = reporting_date()

    conn = get_db()
    try:
        if not dept_id and dept_name:
            dept_row = ensure_department_by_name(conn, dept_name)
            dept_id = dept_row["id"] if dept_row else None

        # Prevent duplicate 'Serving' rows for the same doctor/department.
        serving_sql = """
            SELECT qt.id, qt.appointment_id, qt.patient_id, qt.department_id, qt.token_number,
                   qt.queue_date, qt.position, qt.est_wait_min, qt.status,
                   p.name AS patient_name, p.age AS patient_age,
                   d.name AS department_name
            FROM queue_tokens qt
            LEFT JOIN appointments a ON a.id = qt.appointment_id
            LEFT JOIN patients p ON p.id = qt.patient_id
            LEFT JOIN departments d ON d.id = qt.department_id
            WHERE qt.queue_date = ? AND qt.status = 'Serving'
        """
        serving_params = [today]
        if doctor_id:
            serving_sql += " AND (a.doctor_id = ? OR (a.doctor_id IS NULL AND qt.department_id = (SELECT department_id FROM doctors WHERE id=? LIMIT 1)))"
            serving_params.extend([doctor_id, doctor_id])
        if dept_id:
            serving_sql += " AND qt.department_id = ?"
            serving_params.append(dept_id)
        serving_sql += " ORDER BY qt.called_at DESC, qt.id DESC LIMIT 1"
        serving_row = conn.execute(serving_sql, serving_params).fetchone()
        if serving_row:
            return ok(row_to_dict(serving_row), "A patient is already being served")

        # ── If the frontend sent an explicit token_id (priority/skip override),
        # try to serve that specific token — validate it is still Waiting and
        # belongs to the right doctor/department before trusting it.
        next_row = None
        if requested_token_id:
            candidate_sql = """
                SELECT qt.id, qt.appointment_id, qt.patient_id, qt.department_id, qt.token_number,
                       qt.queue_date, qt.position, qt.est_wait_min, qt.status,
                       p.name AS patient_name, p.age AS patient_age,
                       d.name AS department_name
                FROM queue_tokens qt
                LEFT JOIN appointments a ON a.id = qt.appointment_id
                LEFT JOIN patients p ON p.id = qt.patient_id
                LEFT JOIN departments d ON d.id = qt.department_id
                WHERE qt.id = ? AND qt.queue_date = ? AND qt.status = 'Waiting'
            """
            candidate_params = [requested_token_id, today]
            if doctor_id:
                candidate_sql += " AND (a.doctor_id = ? OR (a.doctor_id IS NULL AND qt.department_id = (SELECT department_id FROM doctors WHERE id=? LIMIT 1)))"
                candidate_params.extend([doctor_id, doctor_id])
            if dept_id:
                candidate_sql += " AND qt.department_id = ?"
                candidate_params.append(dept_id)
            next_row = conn.execute(candidate_sql, candidate_params).fetchone()
            # If the requested token is no longer valid (already served/completed),
            # fall through to the normal position-based selection below.

        if next_row is None:
            # Prefer the earliest waiting token for the selected department/doctor.
            sql = """
                SELECT qt.id, qt.appointment_id, qt.patient_id, qt.department_id, qt.token_number,
                       qt.queue_date, qt.position, qt.est_wait_min, qt.status,
                       p.name AS patient_name, p.age AS patient_age,
                       d.name AS department_name
                FROM queue_tokens qt
                LEFT JOIN appointments a ON a.id = qt.appointment_id
                LEFT JOIN patients p ON p.id = qt.patient_id
                LEFT JOIN departments d ON d.id = qt.department_id
                WHERE qt.queue_date = ? AND qt.status = 'Waiting'
            """
            params = [today]
            if doctor_id:
                sql += " AND (a.doctor_id = ? OR (a.doctor_id IS NULL AND qt.department_id = (SELECT department_id FROM doctors WHERE id=? LIMIT 1)))"
                params.extend([doctor_id, doctor_id])
            if dept_id:
                sql += " AND qt.department_id = ?"
                params.append(dept_id)

            sql += """
                ORDER BY
                    qt.position ASC,
                    CASE WHEN TRIM(COALESCE(a.visit_time, '')) = '' THEN 1 ELSE 0 END,
                    COALESCE(a.visit_date, qt.queue_date) ASC,
                    COALESCE(a.visit_time, '') ASC,
                    COALESCE(a.id, qt.id) ASC,
                    qt.id ASC
                LIMIT 1"""
            next_row = conn.execute(sql, params).fetchone()

        if not next_row:
            return not_found("No waiting patients in queue")

        now_time = datetime.now().strftime("%H:%M:%S")

        # Mark the selected token as serving
        conn.execute(
            "UPDATE queue_tokens SET status='Serving', called_at=?, updated_at=datetime('now') WHERE id=?",
            (now_time, next_row["id"]),
        )

        # Keep the linked appointment in sync
        if next_row["appointment_id"]:
            conn.execute(
                """UPDATE appointments
                   SET status='Active',
                       check_in_time=COALESCE(check_in_time, datetime('now')),
                       updated_at=datetime('now')
                   WHERE id=?""",
                (next_row["appointment_id"],),
            )

        # Recompute positions and wait times for the remaining queue in that scope
        _sync_queue_scope_after_token_change(conn, next_row["department_id"], today, next_row["appointment_id"])
        conn.commit()

        payload = conn.execute(
            """SELECT qt.*, p.name AS patient_name, p.age AS patient_age,
                      d.name AS department_name,
                      a.urgency AS urgency
               FROM queue_tokens qt
               LEFT JOIN patients p ON p.id = qt.patient_id
               LEFT JOIN departments d ON d.id = qt.department_id
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               WHERE qt.id=?""",
            (next_row["id"],),
        ).fetchone()

        result = row_to_dict(payload)
        try:
            _sse_broadcast("queue_update", {"queue": _queue_snapshot(dept_id), "source": "/api/queue/call-next"})
        except Exception:
            pass

        log_audit("queue_call_next", "queue_token", next_row["id"], {"department_id": dept_id, "doctor_id": doctor_id, "token_number": next_row["token_number"]})
        return ok(result, "Next token called")
    finally:
        conn.close()


@app.route("/api/queue/doctor-wise", methods=["GET"])
def doctor_wise_queue():
    """
    Return department-wise doctor queues for the admin token queue screen.
    Each doctor includes its current serving patient and waiting list.
    """
    conn = get_db()
    today = reporting_date()
    requested_dept_id = coerce_int(request.args.get("department_id"), 0) or None
    requested_dept_name = (request.args.get("department_name") or "").strip()

    doctors = conn.execute(
        """
        SELECT d.id AS doctor_id, d.name AS doctor_name, d.shift, d.status,
               dep.id AS department_id, dep.name AS department_name
        FROM doctors d
        LEFT JOIN departments dep ON dep.id = d.department_id
        WHERE d.is_active=1
        ORDER BY dep.name ASC, d.name ASC
        """
    ).fetchall()

    grouped = {}
    for doc in doctors:
        dept_id = doc["department_id"]
        dept_name = doc["department_name"] or "Unassigned"
        if requested_dept_id and dept_id != requested_dept_id:
            continue
        if requested_dept_name and dept_name.lower() != requested_dept_name.lower():
            continue
        grouped.setdefault((dept_id, dept_name), [])

        rows = conn.execute(
            """
            SELECT qt.id, qt.token_number, qt.queue_date, qt.position, qt.est_wait_min,
                   qt.status, qt.called_at, qt.completed_at,
                   p.name AS patient_name, p.age AS patient_age,
                   a.urgency AS urgency,
                   a.doctor_id AS appointment_doctor_id
            FROM queue_tokens qt
            LEFT JOIN appointments a ON a.id = qt.appointment_id
            LEFT JOIN patients p ON p.id = qt.patient_id
            WHERE qt.queue_date = ?
              AND qt.status IN ('Waiting','Serving')
              AND a.doctor_id = ?
            ORDER BY CASE WHEN qt.status='Serving' THEN 0 ELSE 1 END,
                     qt.position ASC, qt.id ASC
            """,
            (today, doc["doctor_id"]),
        ).fetchall()

        serving = None
        waiting = []
        for row in rows:
            payload = row_to_dict(row)
            if payload.get("status") == "Serving" and serving is None:
                serving = payload
            else:
                waiting.append(payload)

        grouped[(dept_id, dept_name)].append({
            "doctor_id": doc["doctor_id"],
            "doctor_name": doc["doctor_name"],
            "shift": doc["shift"],
            "status": doc["status"],
            "department_id": dept_id,
            "department_name": dept_name,
            "serving": serving,
            "waiting_count": len(waiting),
            "waiting_patients": waiting,
        })

    result = []
    for (dept_id, dept_name), doctors_list in grouped.items():
        result.append({
            "department_id": dept_id,
            "department_name": dept_name,
            "doctors": doctors_list,
        })

    conn.close()
    return ok(result, "Doctor-wise queue loaded")

@app.route("/api/queue/<int:token_id>", methods=["GET", "PATCH"])
def queue_token_detail(token_id):
    conn = get_db()
    try:
        if request.method == "GET":
            row = conn.execute(
                """SELECT qt.*, p.name AS patient_name, d.name AS department_name,
                          a.urgency, a.symptoms
                   FROM queue_tokens qt
                   LEFT JOIN patients p ON p.id = qt.patient_id
                   LEFT JOIN departments d ON d.id = qt.department_id
                   LEFT JOIN appointments a ON a.id = qt.appointment_id
                   WHERE qt.id=?""",
                (token_id,),
            ).fetchone()
            return ok(row_to_dict(row)) if row else not_found("Queue token")

        data = request.get_json(silent=True) or {}
        new_status = data.get("status")
        status_map = {
            "Done": "Completed",
            "done": "Completed",
            "complete": "Completed",
            "completed": "Completed",
            "Skip": "Skipped",
            "skip": "Skipped",
            "skipped": "Skipped",
        }
        new_status = status_map.get(new_status, new_status)

        valid_statuses = ("Waiting", "Serving", "Completed", "Skipped", "Transferred", "Cancelled")
        if new_status not in valid_statuses:
            return bad_request(f"Valid statuses: {valid_statuses}")

        row = conn.execute("SELECT * FROM queue_tokens WHERE id=?", (token_id,)).fetchone()
        if not row:
            return not_found("Queue token")

        now_time = datetime.now().strftime("%H:%M:%S")
        queue_date = row["queue_date"] or reporting_date()

        if new_status == "Serving":
            conn.execute(
                "UPDATE queue_tokens SET status=?, called_at=?, updated_at=datetime('now') WHERE id=?",
                (new_status, now_time, token_id),
            )
            conn.execute(
                "UPDATE appointments SET status='Active', check_in_time=COALESCE(check_in_time, datetime('now')) WHERE id=?",
                (row["appointment_id"],),
            )
        elif new_status == "Completed":
            conn.execute(
                "UPDATE queue_tokens SET status=?, completed_at=?, updated_at=datetime('now') WHERE id=?",
                (new_status, now_time, token_id),
            )
            if row["appointment_id"]:
                conn.execute(
                    """UPDATE appointments
                       SET status='Completed', check_out_time=datetime('now'), updated_at=datetime('now')
                       WHERE id=?""",
                    (row["appointment_id"],),
                )
        elif new_status == "Skipped":
            # Skip means move the patient to the end of the same department queue.
            max_pos_row = conn.execute(
                """SELECT COALESCE(MAX(position), -1) AS max_position
                   FROM queue_tokens
                   WHERE department_id=? AND queue_date=? AND status='Waiting' AND id<>?""",
                (row["department_id"], queue_date, token_id),
            ).fetchone()
            new_position = int(max_pos_row["max_position"] or -1) + 1
            new_wait_time = (new_position + 1) * AVG_SERVICE_TIME_MIN
            conn.execute(
                """UPDATE queue_tokens
                   SET status='Waiting',
                       position=?,
                       est_wait_min=?,
                       called_at=NULL,
                       completed_at=NULL,
                       updated_at=datetime('now')
                   WHERE id=?""",
                (new_position, new_wait_time, token_id),
            )
            if row["appointment_id"]:
                conn.execute(
                    "UPDATE appointments SET status='Active', updated_at=datetime('now') WHERE id=?",
                    (row["appointment_id"],),
                )
        else:
            conn.execute(
                "UPDATE queue_tokens SET status=?, updated_at=datetime('now') WHERE id=?",
                (new_status, token_id),
            )
            if new_status in ("Transferred", "Cancelled") and row["appointment_id"]:
                conn.execute(
                    "UPDATE appointments SET status=?, updated_at=datetime('now') WHERE id=?",
                    (new_status, row["appointment_id"]),
                )

        _sync_queue_scope_after_token_change(conn, row["department_id"], queue_date, row["appointment_id"])
        conn.commit()
        updated = conn.execute(
            """SELECT qt.*, p.name AS patient_name, d.name AS department_name
               FROM queue_tokens qt
               LEFT JOIN patients p ON p.id = qt.patient_id
               LEFT JOIN departments d ON d.id = qt.department_id
               WHERE qt.id=?""",
            (token_id,),
        ).fetchone()

        try:
            _sse_broadcast("queue_update", {"queue": _queue_snapshot(), "source": "/api/queue/<id>"})
        except Exception:
            pass

        return ok(row_to_dict(updated), "Queue token updated")
    finally:
        conn.close()


@app.route("/api/queue/<int:token_id>/status", methods=["PATCH", "POST"])
def queue_token_status_action(token_id):
    data = request.get_json(silent=True) or {}
    action = data.get("status") or data.get("action") or "Completed"
    conn = get_db()
    try:
        return _queue_token_action_response(conn, token_id, action=action)
    finally:
        conn.close()


@app.route("/api/queue/<int:token_id>/skip", methods=["POST", "PATCH"])
def queue_token_skip_action(token_id):
    conn = get_db()
    try:
        return _queue_token_action_response(conn, token_id, action="Skipped")
    finally:
        conn.close()


@app.route("/api/queue/<int:token_id>/prioritize", methods=["POST", "PATCH"])
def queue_token_prioritize_action(token_id):
    """
    Move the specified waiting token to position 0 (front) of its doctor's queue.
    All other waiting tokens in the same doctor scope are shifted down by one.
    This persists the priority override so that call-next always calls this token first
    regardless of appointment time ordering.
    """
    conn = get_db()
    today = reporting_date()
    try:
        row = conn.execute("SELECT * FROM queue_tokens WHERE id=?", (token_id,)).fetchone()
        if not row:
            return not_found("Queue token")
        if row["status"] not in ("Waiting",):
            return bad_request("Only Waiting tokens can be prioritized")

        queue_date = row["queue_date"] or today
        dept_id = row["department_id"]
        doctor_id = _appointment_doctor_id(conn, row["appointment_id"])

        # Shift all other waiting tokens in the same doctor scope up by 1 to
        # make room at position 0 for the prioritized token.
        if doctor_id:
            conn.execute(
                """UPDATE queue_tokens
                   SET position = position + 1,
                       updated_at = datetime('now')
                   WHERE department_id=? AND queue_date=? AND status='Waiting'
                     AND id != ?
                     AND id IN (
                         SELECT qt2.id FROM queue_tokens qt2
                         LEFT JOIN appointments a2 ON a2.id = qt2.appointment_id
                         WHERE qt2.department_id=? AND qt2.queue_date=?
                           AND COALESCE(a2.doctor_id, 0) = ?
                     )""",
                (dept_id, queue_date, token_id, dept_id, queue_date, doctor_id),
            )
        else:
            conn.execute(
                """UPDATE queue_tokens
                   SET position = position + 1,
                       updated_at = datetime('now')
                   WHERE department_id=? AND queue_date=? AND status='Waiting'
                     AND id != ?
                     AND id IN (
                         SELECT qt2.id FROM queue_tokens qt2
                         LEFT JOIN appointments a2 ON a2.id = qt2.appointment_id
                         WHERE qt2.department_id=? AND qt2.queue_date=?
                           AND COALESCE(a2.doctor_id, 0) = 0
                     )""",
                (dept_id, queue_date, token_id, dept_id, queue_date),
            )

        # Place the prioritized token at position 0 with ~0 additional wait
        conn.execute(
            """UPDATE queue_tokens
               SET position=0, est_wait_min=0, updated_at=datetime('now')
               WHERE id=?""",
            (token_id,),
        )

        # Re-number to compact positions cleanly (handles any gaps)
        recalculate_queue_positions(conn, dept_id, queue_date, doctor_id)
        conn.commit()

        try:
            _sse_broadcast("queue_update", {"queue": _queue_snapshot(dept_id), "source": "/api/queue/prioritize"})
        except Exception:
            pass

        log_audit("queue_prioritize", "queue_token", token_id, {"department_id": dept_id, "doctor_id": doctor_id})
        updated = conn.execute(
            """SELECT qt.*, p.name AS patient_name, d.name AS department_name, a.urgency
               FROM queue_tokens qt
               LEFT JOIN patients p ON p.id = qt.patient_id
               LEFT JOIN departments d ON d.id = qt.department_id
               LEFT JOIN appointments a ON a.id = qt.appointment_id
               WHERE qt.id=?""",
            (token_id,),
        ).fetchone()
        return ok(row_to_dict(updated) if updated else {}, "Token prioritized")
    finally:
        conn.close()


@app.route("/api/queue/<int:token_id>/transfer", methods=["POST", "PATCH"])
def queue_token_transfer_action(token_id):
    data = request.get_json(silent=True) or {}
    target_department_id = data.get("department_id") or data.get("target_department_id")
    target_doctor_id = data.get("doctor_id") or data.get("target_doctor_id")
    conn = get_db()
    try:
        return _queue_token_action_response(
            conn,
            token_id,
            action="Transferred",
            target_department_id=target_department_id,
            target_doctor_id=target_doctor_id,
        )
    finally:
        conn.close()


@app.route("/api/user/dashboard", methods=["GET"])
@require_auth()
def user_dashboard():
    conn = get_db()
    user = current_user() or {}
    user_id = user.get("id")
    # Resolve patient_id: prefer explicit patient_id from users table, then search patients by display_name
    _raw_pid = user.get("patient_id")
    if _raw_pid:
        patient_id = coerce_int(_raw_pid, 0) or None
    else:
        # Try to find patient record linked to this user via display_name or username
        _urow = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        _pname = (_urow["display_name"] if _urow else None) or user.get("display_name") or user.get("username")
        _pat = None
        if _pname:
            _pat = conn.execute(
                "SELECT id FROM patients WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) AND is_active=1 ORDER BY id DESC LIMIT 1",
                (_pname,)
            ).fetchone()
        patient_id = _pat["id"] if _pat else None
        # Permanently link resolved patient to user for future session lookups
        if patient_id:
            conn.execute("UPDATE users SET patient_id=? WHERE id=?", (patient_id, user_id))
            conn.commit()
    today = reporting_date()

    active_token = None
    if patient_id:
        # Try to find active token by patient_id first
        _at_row = conn.execute(
            """SELECT qt.*, d.name AS department_name
               FROM queue_tokens qt
               LEFT JOIN departments d ON d.id = qt.department_id
               WHERE qt.patient_id=? AND qt.queue_date=? AND qt.status IN ('Waiting','Serving')
               ORDER BY qt.id DESC LIMIT 1""",
            (patient_id, today),
        ).fetchone()
        active_token = row_to_dict(_at_row) if _at_row else None

    # Count appointments by patient_id OR by matching user_id via patients table
    stats = conn.execute(
        """SELECT
               COUNT(*) AS total,
               COUNT(CASE WHEN status='Completed' THEN 1 END) AS completed
           FROM appointments
           WHERE patient_id=?""",
        (patient_id,),
    ).fetchone()

    avg_wait = conn.execute(
        """SELECT COALESCE(AVG(est_wait_min), 0)
           FROM queue_tokens
           WHERE queue_date=? AND status='Waiting'""",
        (today,),
    ).fetchone()[0]

    # Return the resolved patient_id so the frontend can cache it
    resolved_patient_id = patient_id

    conn.close()
    return ok({
        "active_token": active_token,
        "total_appointments": stats["total"] if stats else 0,
        "completed_appointments": stats["completed"] if stats else 0,
        "avg_wait_min": round(avg_wait or 0),
        "patient_id": resolved_patient_id,
    })


@app.route("/api/user/appointments", methods=["GET", "POST"])
@require_auth()
def user_appointments():
    conn = get_db()
    user = current_user() or {}
    user_id = user.get("id")
    # Resolve patient_id properly (same logic as user_dashboard)
    _raw_pid2 = user.get("patient_id")
    if _raw_pid2:
        patient_id = coerce_int(_raw_pid2, 0) or None
    else:
        patient_id = None

    if request.method == "POST":
        try:
            data = request.get_json(silent=True) or {}
            today = reporting_date()
            visit_date = str(data.get("visit_date") or today).strip() or today
            requested_name = str(data.get("name") or "").strip()
            requested_phone = str(data.get("phone") or "").strip()
            requested_age = coerce_int(data.get("age"), 0)
            requested_gender = str(data.get("gender") or "Other").strip() or "Other"
            if requested_gender not in GENDER_VALUES:
                requested_gender = "Other"
            dept_id = coerce_int(data.get("department_id"), 0)
            if not dept_id:
                return bad_request("department_id is required")
            dept = conn.execute("SELECT * FROM departments WHERE id=? AND is_active=1", (dept_id,)).fetchone()
            if not dept:
                return not_found("Department")
            # Create or find patient record using the name entered in the booking form.
            default_pat = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone() if patient_id else None
            user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            fallback_name = requested_name or (
                (user_row["display_name"] if user_row else None)
                or user.get("display_name")
                or user.get("username")
                or "Patient"
            )
            chosen_patient = None
            if default_pat and str(default_pat["name"] or "").strip().casefold() == fallback_name.casefold():
                chosen_patient = default_pat
            else:
                if requested_phone:
                    chosen_patient = conn.execute(
                        """SELECT * FROM patients
                           WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                             AND TRIM(COALESCE(phone,'')) = TRIM(?)
                             AND is_active=1
                           ORDER BY id DESC
                           LIMIT 1""",
                        (fallback_name, requested_phone),
                    ).fetchone()
                if not chosen_patient:
                    chosen_patient = conn.execute(
                        """SELECT * FROM patients
                           WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))
                             AND is_active=1
                           ORDER BY id DESC
                           LIMIT 1""",
                        (fallback_name,),
                    ).fetchone()

            if chosen_patient:
                patient_id = chosen_patient["id"]
                conn.execute(
                    """UPDATE patients
                       SET name=?,
                           age=CASE WHEN ? > 0 THEN ? ELSE age END,
                           gender=COALESCE(NULLIF(?, ''), gender),
                           phone=COALESCE(NULLIF(?, ''), phone),
                           notes=CASE WHEN ? != '' THEN ? ELSE notes END,
                           updated_at=datetime('now')
                       WHERE id=?""",
                    (
                        fallback_name,
                        requested_age, requested_age,
                        requested_gender,
                        requested_phone,
                        str(data.get("symptoms", "")).strip(), str(data.get("symptoms", "")).strip(),
                        patient_id,
                    ),
                )
                # Always link this patient to the user account so future lookups work
                conn.execute("UPDATE users SET patient_id=? WHERE id=?", (patient_id, user_id))
            else:
                last_p = conn.execute("SELECT patient_code FROM patients ORDER BY id DESC LIMIT 1").fetchone()
                pcode = next_patient_code(last_p["patient_code"] if last_p else "P000")
                cur = conn.execute(
                    """INSERT INTO patients (patient_code, name, age, gender, phone, notes)
                       VALUES (?,?,?,?,?,?)""",
                    (pcode, fallback_name, requested_age, requested_gender, requested_phone, data.get("symptoms", "")),
                )
                patient_id = cur.lastrowid
                # Always link new patient to user account
                conn.execute("UPDATE users SET patient_id=? WHERE id=?", (patient_id, user_id))
            # Find available doctor
            doctor_id = coerce_int(data.get("doctor_id"), 0) or None
            if not doctor_id:
                doc = conn.execute(
                    """SELECT id
                       FROM doctors
                       WHERE department_id=? AND status='On Duty' AND is_active=1
                       ORDER BY name
                       LIMIT 1""",
                    (dept_id,),
                ).fetchone()
                if doc:
                    doctor_id = doc["id"]
            # Create appointment
            token = None
            cur_appt = conn.execute(
                """INSERT INTO appointments
                   (patient_id, doctor_id, department_id, visit_date, visit_time, urgency, symptoms, status)
                   VALUES (?,?,?,?,?,?,?,'Scheduled')""",
                (patient_id, doctor_id, dept_id, visit_date,
                 data.get("visit_time", ""), data.get("urgency", "Medium"),
                 data.get("symptoms", ""))
            )
            appt_id = cur_appt.lastrowid
            # Generate queue token only for today's visit so the user queue stays aligned with admin.
            if visit_date == today:
                existing_tokens = [r["token_number"] for r in conn.execute(
                    "SELECT token_number FROM queue_tokens WHERE queue_date=? AND department_id=?",
                    (today, dept_id)
                ).fetchall()]
                token_num = generate_token(dept["name"], existing_tokens)
                token_cols = table_columns(conn, "queue_tokens")
                if "urgency" in token_cols:
                    conn.execute(
                        """INSERT INTO queue_tokens
                           (appointment_id, patient_id, department_id, queue_date, token_number, urgency, position, est_wait_min, status)
                           VALUES (?,?,?,?,?,?,?,?,'Waiting')""",
                        (appt_id, patient_id, dept_id, today, token_num,
                         data.get("urgency", "Medium"), 0, 0)
                    )
                else:
                    conn.execute(
                        """INSERT INTO queue_tokens
                           (appointment_id, patient_id, department_id, queue_date, token_number, position, est_wait_min, status)
                           VALUES (?,?,?,?,?,?,?,'Waiting')""",
                        (appt_id, patient_id, dept_id, today, token_num, 0, 0)
                    )
                _sync_queue_scope_after_token_change(conn, dept_id, today, appt_id)
            conn.commit()
            appt = row_to_dict(conn.execute(
                """SELECT a.*, d.name AS department_name FROM appointments a
                   LEFT JOIN departments d ON d.id=a.department_id WHERE a.id=?""",
                (appt_id,)
            ).fetchone())
            _tok_row = conn.execute(
                "SELECT * FROM queue_tokens WHERE appointment_id=? ORDER BY id DESC LIMIT 1", (appt_id,)
            ).fetchone()
            token = row_to_dict(_tok_row) if _tok_row else {}
            return created({"appointment": appt, "token": token}, "Appointment booked")
        except Exception as exc:
            conn.rollback()
            print(f"[USER APPOINTMENT CREATE ERROR] {exc}")
            return server_error(f"Appointment booking failed: {exc}")
        finally:
            conn.close()

    if not patient_id:
        # Try to resolve patient_id: first check users table directly, then match by name
        _urow2 = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if _urow2 and _urow2["patient_id"]:
            patient_id = coerce_int(_urow2["patient_id"], 0) or None
        if not patient_id:
            _pname2 = (_urow2["display_name"] if _urow2 else None) or user.get("display_name") or user.get("name") or user.get("username")
            if _pname2:
                _pat2 = conn.execute(
                    "SELECT id FROM patients WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) AND is_active=1 ORDER BY id DESC LIMIT 1",
                    (_pname2,)
                ).fetchone()
                if _pat2:
                    patient_id = _pat2["id"]
            # Also try matching by email/phone if name didn't match
            if not patient_id and _urow2:
                _email2 = (_urow2["email"] or "").strip()
                _phone2 = (_urow2["phone"] or "").strip()
                if _email2 or _phone2:
                    _pat_by_contact = conn.execute(
                        """SELECT id FROM patients WHERE is_active=1
                           AND (TRIM(COALESCE(phone,''))=? OR LOWER(TRIM(COALESCE(email,'')))=?)
                           ORDER BY id DESC LIMIT 1""",
                        (_phone2, _email2.lower())
                    ).fetchone()
                    if _pat_by_contact:
                        patient_id = _pat_by_contact["id"]
        if patient_id:
            # Permanently link patient_id to user account for future calls
            conn.execute("UPDATE users SET patient_id=? WHERE id=?", (patient_id, user_id))
            conn.commit()

    if not patient_id:
        conn.close()
        return ok([])

    rows = rows_to_list(conn.execute(
        """SELECT a.*,
                  (SELECT qt2.token_number FROM queue_tokens qt2
                   WHERE qt2.appointment_id = a.id ORDER BY qt2.id DESC LIMIT 1) AS token_number,
                  (SELECT qt2.est_wait_min FROM queue_tokens qt2
                   WHERE qt2.appointment_id = a.id ORDER BY qt2.id DESC LIMIT 1) AS token_est_wait_min,
                  (SELECT qt2.status FROM queue_tokens qt2
                   WHERE qt2.appointment_id = a.id ORDER BY qt2.id DESC LIMIT 1) AS queue_status,
                  d.name AS department_name, doc.name AS doctor_name
           FROM appointments a
           LEFT JOIN departments d ON d.id = a.department_id
           LEFT JOIN doctors doc ON doc.id = a.doctor_id
           WHERE a.patient_id = ?
           ORDER BY a.visit_date DESC, a.id DESC""",
        (patient_id,),
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/user/queue-status", methods=["GET"])
@require_auth()
def user_queue_status():
    conn = get_db()
    user = current_user() or {}
    user_id = user.get("id")
    _raw_pid_qs = user.get("patient_id")
    if _raw_pid_qs:
        patient_id = coerce_int(_raw_pid_qs, 0) or None
    else:
        # Resolve patient via display_name/username match
        _urow_qs = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        _pname_qs = (_urow_qs["display_name"] if _urow_qs else None) or user.get("display_name") or user.get("username")
        patient_id = None
        if _pname_qs:
            _pat_qs = conn.execute(
                "SELECT id FROM patients WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) AND is_active=1 ORDER BY id DESC LIMIT 1",
                (_pname_qs,)
            ).fetchone()
            if _pat_qs:
                patient_id = _pat_qs["id"]
                conn.execute("UPDATE users SET patient_id=? WHERE id=?", (patient_id, user_id))
                conn.commit()
    today = reporting_date()
    token_cols = table_columns(conn, "queue_tokens")
    appt_cols = table_columns(conn, "appointments")
    if "urgency" in token_cols and "urgency" in appt_cols:
        urgency_expr = "COALESCE(qt.urgency, a.urgency, 'Medium') AS urgency"
    elif "urgency" in token_cols:
        urgency_expr = "COALESCE(qt.urgency, 'Medium') AS urgency"
    elif "urgency" in appt_cols:
        urgency_expr = "COALESCE(a.urgency, 'Medium') AS urgency"
    else:
        urgency_expr = "'Medium' AS urgency"

    live_queue_date_row = conn.execute(
        """SELECT queue_date
           FROM queue_tokens
           WHERE status IN ('Waiting','Serving')
           GROUP BY queue_date
           ORDER BY CASE WHEN queue_date = ? THEN 0 ELSE 1 END, queue_date DESC
           LIMIT 1""",
        (today,),
    ).fetchone()
    live_queue_date = live_queue_date_row["queue_date"] if live_queue_date_row else today

    active = None
    if patient_id:
        active = row_to_dict(conn.execute(
            """SELECT qt.*, d.name AS department_name, p.name AS patient_name
               FROM queue_tokens qt
               LEFT JOIN departments d ON d.id = qt.department_id
               LEFT JOIN patients p ON p.id = qt.patient_id
               WHERE qt.patient_id=? AND qt.status IN ('Waiting','Serving')
               ORDER BY CASE WHEN qt.queue_date = ? THEN 0 ELSE 1 END, qt.queue_date DESC, qt.id DESC
               LIMIT 1""",
            (patient_id, today),
        ).fetchone())

    live_queue = rows_to_list(conn.execute(
        f"""SELECT qt.*, p.name AS patient_name, d.name AS department_name,
                  a.doctor_id AS doctor_id, doc.name AS doctor_name,
                  {urgency_expr}
           FROM queue_tokens qt
           LEFT JOIN patients p ON p.id = qt.patient_id
           LEFT JOIN departments d ON d.id = qt.department_id
           LEFT JOIN appointments a ON a.id = qt.appointment_id
           LEFT JOIN doctors doc ON doc.id = a.doctor_id
           WHERE qt.queue_date=? AND qt.status IN ('Waiting','Serving')
           ORDER BY CASE WHEN qt.status='Serving' THEN 0 ELSE 1 END, qt.position ASC, qt.id ASC""",
        (live_queue_date,),
    ).fetchall())

    queue_summary = []
    summary_rows = conn.execute(
        """SELECT d.name AS department,
                  COUNT(CASE WHEN qt.status='Waiting' THEN 1 END) AS waiting,
                  COUNT(CASE WHEN qt.status='Serving' THEN 1 END) AS serving
           FROM departments d
           LEFT JOIN queue_tokens qt
             ON qt.department_id=d.id
            AND qt.queue_date=?
            AND qt.status IN ('Waiting','Serving')
           WHERE d.is_active=1
           GROUP BY d.id
           ORDER BY waiting DESC, d.name ASC""",
        (live_queue_date,),
    ).fetchall()
    for row in summary_rows:
        waiting = int(row["waiting"] or 0)
        serving = int(row["serving"] or 0)
        next_patient_wait = (waiting + 1) * AVG_SERVICE_TIME_MIN if serving > 0 else waiting * AVG_SERVICE_TIME_MIN
        if next_patient_wait >= 60:
            status = "High"
        elif next_patient_wait >= 30:
            status = "Medium"
        else:
            status = "Low"
        queue_summary.append({
            "department": row["department"],
            "waiting": waiting,
            "serving": serving,
            "avg_wait_min": next_patient_wait,
            "status": status,
        })
    conn.close()
    return ok({
        "active_token": active,
        "live_queue": live_queue,
        "queue_summary": queue_summary,
        "queue_date": live_queue_date,
    })


@app.route("/api/user/profile", methods=["GET", "PUT"])
@require_auth()
def user_profile():
    conn = get_db()
    user = current_user() or {}

    if request.method == "GET":
        row = conn.execute(
            """SELECT id, username, display_name, email, phone, role, created_at, updated_at
               FROM users WHERE id=?""",
            (user["id"],),
        ).fetchone()
        conn.close()
        return ok(row_to_dict(row))

    data = request.get_json(silent=True) or {}
    conn.execute(
        """UPDATE users
           SET display_name=COALESCE(?, display_name),
               phone=COALESCE(?, phone),
               updated_at=datetime('now')
           WHERE id=?""",
        (data.get("display_name"), data.get("phone"), user["id"]),
    )
    conn.commit()
    conn.close()
    return ok({"updated": True}, "Profile updated")


@app.route("/api/user/notifications", methods=["GET"])
@require_auth()
def user_notifications():
    conn = get_db()
    today = reporting_date()
    user = current_user() or {}
    user_id = user.get("id")

    # Resolve patient_id for this user (same pattern as user_dashboard)
    _raw_pid_notif = user.get("patient_id")
    if _raw_pid_notif:
        patient_id = coerce_int(_raw_pid_notif, 0) or None
    else:
        _urow_notif = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        _pname_notif = (_urow_notif["display_name"] if _urow_notif else None) or user.get("display_name") or user.get("username")
        patient_id = None
        if _pname_notif:
            _pat_notif = conn.execute(
                "SELECT id FROM patients WHERE LOWER(TRIM(name))=LOWER(TRIM(?)) AND is_active=1 ORDER BY id DESC LIMIT 1",
                (_pname_notif,)
            ).fetchone()
            if _pat_notif:
                patient_id = _pat_notif["id"]

    notifications = []

    # 1. User's own token alerts — notify when wait <= 20 min (highest priority)
    if patient_id:
        user_tokens = rows_to_list(conn.execute(
            """SELECT qt.token_number, qt.est_wait_min, qt.status, qt.position,
                      d.name AS department_name
               FROM queue_tokens qt
               LEFT JOIN departments d ON d.id = qt.department_id
               WHERE qt.patient_id = ? AND qt.queue_date = ?
                 AND qt.status IN ('Waiting', 'Serving')
               ORDER BY qt.id DESC""",
            (patient_id, today),
        ).fetchall())
        for tok in user_tokens:
            wait = int(tok.get("est_wait_min") or 0)
            dept = tok.get("department_name") or "Your Department"
            token_num = tok.get("token_number") or ""
            status = tok.get("status") or "Waiting"
            if status == "Serving" or wait == 0:
                notifications.append({
                    "level": "Critical",
                    "department": dept,
                    "token_number": token_num,
                    "est_wait_min": 0,
                    "message": f"Token {token_num} — It's your turn now! Please proceed to {dept} immediately.",
                    "type": "Your Turn",
                })
            elif wait <= 20:
                notifications.append({
                    "level": "High",
                    "department": dept,
                    "token_number": token_num,
                    "est_wait_min": wait,
                    "message": f"Token {token_num} — Only ~{wait} min wait in {dept}. Please be ready.",
                    "type": "Token Alert",
                })

    # 2. Department-wide high wait alerts
    dept_waits = conn.execute("""
        SELECT d.name,
               COUNT(CASE WHEN qt.status='Waiting' THEN 1 END) AS waiting,
               COUNT(CASE WHEN qt.status='Serving' THEN 1 END) AS serving
        FROM departments d
        LEFT JOIN queue_tokens qt ON qt.department_id=d.id AND qt.queue_date=?
            AND qt.status IN ('Waiting','Serving')
        WHERE d.is_active=1
        GROUP BY d.id
    """, (today,)).fetchall()

    for row in dept_waits:
        waiting = row["waiting"] or 0
        serving = row["serving"] or 0
        avg_wait = (waiting + 1) * 8 if serving > 0 else waiting * 8
        if avg_wait >= 90:
            notifications.append({
                "level": "Critical",
                "department": row["name"],
                "message": f"{row['name']} wait time is critically high at {avg_wait} minutes. {waiting} patients waiting.",
                "type": "High Wait Alert",
            })
        elif avg_wait >= 60:
            notifications.append({
                "level": "High",
                "department": row["name"],
                "message": f"{row['name']} average wait is {avg_wait} minutes. {waiting} patients currently waiting.",
                "type": "Wait Time Alert",
            })
        elif waiting >= 15:
            notifications.append({
                "level": "Medium",
                "department": row["name"],
                "message": f"{row['name']} has high patient volume with {waiting} patients waiting (~{avg_wait} min).",
                "type": "Volume Alert",
            })

    # 3. Admin-created critical alerts
    backend_alerts = rows_to_list(conn.execute(
        """SELECT al.*, d.name AS department_name
           FROM alerts al
           LEFT JOIN departments d ON d.id = al.department_id
           WHERE al.is_active = 1 AND al.level IN ('Critical','High')
           ORDER BY al.created_at DESC LIMIT 5""",
    ).fetchall())
    for r in backend_alerts:
        notifications.append({
            "level": r.get("level"),
            "department": r.get("department_name") or "System",
            "message": r.get("message"),
            "type": "Admin Alert",
        })

    conn.close()
    return ok(notifications)


@app.route("/api/user/feedback", methods=["GET", "POST"])
@require_auth()
def user_feedback():
    conn = get_db()
    user = current_user() or {}
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        cur = conn.execute(
            """INSERT INTO feedback_entries (user_id, category, subject, message, rating)
               VALUES (?,?,?,?,?)""",
            (
                user.get("id"),
                data.get("category", "feedback"),
                data.get("subject", ""),
                data.get("message", ""),
                data.get("rating"),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM feedback_entries WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        return created(row_to_dict(row), "Feedback submitted")
    # GET — return this user's feedback
    rows = rows_to_list(conn.execute(
        "SELECT * FROM feedback_entries WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user.get("id"),)
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    data = request.get_json(silent=True) or {}
    conn = get_db()
    user = current_user() or {}
    cur = conn.execute(
        """INSERT INTO feedback_entries (user_id, category, subject, message, rating)
           VALUES (?,?,?,?,?)""",
        (
            user.get("id"),
            data.get("category", "feedback"),
            data.get("subject", ""),
            data.get("message", ""),
            data.get("rating"),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM feedback_entries WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return created(row_to_dict(row), "Feedback submitted")


@app.route("/api/feedback", methods=["GET"])
def list_feedback():
    conn = get_db()
    rows = rows_to_list(conn.execute(
        "SELECT * FROM feedback_entries ORDER BY created_at DESC LIMIT 50"
    ).fetchall())
    conn.close()
    return ok(rows, meta={"total": len(rows)})


# -----------------------------------------------------------------------
# QUEUE MUTATION HOOKS — broadcast SSE after status changes
# -----------------------------------------------------------------------

@app.after_request
def broadcast_on_queue_mutation(response):
    """After any queue-mutating call, push a queue_update SSE event.

    Uses _queue_snapshot() (all departments, consistent wait-time-based status
    thresholds) so the dashboard always receives a complete, authoritative picture
    of the queue — including both the source AND target department after a transfer.
    """
    if response.status_code in (200, 201) and request.method in ("POST", "PATCH", "PUT"):
        mutating_paths = (
            "/api/queue",           # covers /api/queue/<id>/transfer, /skip, /status …
            "/api/queue/simulate-arrival",
            "/api/queue/call-next",
            "/api/user/appointments",  # user booking creates a token → broadcast to all portals
            "/api/appointments",       # admin appointment changes
        )
        if any(request.path.startswith(p) for p in mutating_paths):
            try:
                _sse_broadcast("queue_update", {"queue": _queue_snapshot(), "source": request.path})
            except Exception:
                pass
    return response

if __name__ == "__main__":
    print("=" * 60)
    print("  Smart Hospital Queue System v2.1")
    print("  Backend: http://127.0.0.1:5000")
    print("  Open browser at: http://127.0.0.1:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
    app.run(debug=True, host="0.0.0.0", port=5000)