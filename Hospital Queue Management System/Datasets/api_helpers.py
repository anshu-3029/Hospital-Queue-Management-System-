"""
api_helpers.py
==============
Shared utilities, response helpers, validators, and constants
for the Smart Hospital Queue Management System.
"""

import re
import secrets
import string
from datetime import datetime
from flask import jsonify


# ---------------------------------------------------------------------------
# Valid value lists (used by routes for validation)
# ---------------------------------------------------------------------------
URGENCY_VALUES  = ("Low", "Medium", "High")
STATUS_APPT     = ("Scheduled", "Active", "Completed", "Missed", "Cancelled")
STATUS_QUEUE    = ("Waiting", "Serving", "Completed", "Skipped", "Transferred", "Cancelled")
STATUS_DOCTOR   = ("On Duty", "Break", "Off Duty")
SHIFT_VALUES    = ("Morning", "Afternoon", "Evening", "Night")
GENDER_VALUES   = ("Male", "Female", "Other")
ALERT_TYPES     = ("overload", "understaffed", "peak_hour", "custom", "drift")
ALERT_LEVELS    = ("Low", "Medium", "High", "Critical")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _build(success: bool, data=None, message: str = "", meta: dict = None) -> dict:
    body: dict = {"success": success}
    if message:
        body["message"] = message
    if meta is not None:
        body["meta"] = meta
    if data is not None:
        body["data"] = data
    return body


def ok(data=None, message: str = "", meta: dict = None):
    """200 OK with optional message and meta."""
    return jsonify(_build(True, data, message, meta)), 200


def created(data=None, message: str = "Created"):
    """201 Created."""
    return jsonify(_build(True, data, message)), 201


def bad_request(message: str = "Bad request"):
    """400 Bad Request."""
    return jsonify(_build(False, None, message)), 400


def unauthorized(message: str = "Authentication required"):
    """401 Unauthorized."""
    return jsonify(_build(False, None, message)), 401


def forbidden(message: str = "Forbidden"):
    """403 Forbidden."""
    return jsonify(_build(False, None, message)), 403


def not_found(entity: str = "Resource"):
    """404 Not Found."""
    return jsonify(_build(False, None, f"{entity} not found")), 404


def conflict(message: str = "Conflict"):
    """409 Conflict."""
    return jsonify(_build(False, None, message)), 409


def server_error(message: str = "Internal server error"):
    """500 Internal Server Error."""
    return jsonify(_build(False, None, message)), 500


# ---------------------------------------------------------------------------
# Pagination helper
# ---------------------------------------------------------------------------

def paginate(rows: list, page: int, per_page: int) -> tuple[list, dict]:
    """
    Slice a list of rows and return (page_data, meta_dict).
    page is 1-indexed; per_page=0 means return everything.
    """
    page = max(1, page or 1)
    per_page = per_page or 0
    total = len(rows)
    if per_page <= 0:
        return rows, {"total": total, "page": 1, "per_page": total, "pages": 1}
    pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    end = start + per_page
    return rows[start:end], {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def require_fields(data: dict, fields: list) -> list:
    """Return list of missing required field names."""
    return [f for f in fields if not data.get(f)]


def validate_phone(phone: str) -> bool:
    """Basic phone validation: 7–15 digits with optional leading +."""
    if not phone:
        return True  # optional fields are allowed to be empty
    return bool(re.match(r"^\+?[0-9]{7,15}$", str(phone).strip()))


def validate_date(value: str) -> bool:
    """Validate YYYY-MM-DD format."""
    if not value:
        return False
    try:
        datetime.strptime(str(value).strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Type coercers
# ---------------------------------------------------------------------------

def coerce_int(value, default=0) -> int:
    """Safely cast to int, falling back to default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def coerce_float(value, default=0.0) -> float:
    """Safely cast to float, falling back to default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Code generators
# ---------------------------------------------------------------------------

def next_patient_code(last_code: str) -> str:
    """
    Increment a patient code like P001 → P002, P099 → P100.
    Falls back to a timestamped code if parsing fails.
    """
    try:
        prefix = "".join(c for c in last_code if c.isalpha()) or "P"
        num_str = "".join(c for c in last_code if c.isdigit())
        num = int(num_str) + 1 if num_str else 1
        width = max(3, len(num_str) if num_str else 3)
        return f"{prefix}{num:0{width}d}"
    except Exception:
        return f"P{int(datetime.now().timestamp())}"


def next_doctor_code(last_code: str) -> str:
    """
    Increment a doctor code like DR001 → DR002.
    """
    try:
        prefix = "".join(c for c in last_code if c.isalpha()) or "DR"
        num_str = "".join(c for c in last_code if c.isdigit())
        num = int(num_str) + 1 if num_str else 1
        width = max(3, len(num_str) if num_str else 3)
        return f"{prefix}{num:0{width}d}"
    except Exception:
        return f"DR{int(datetime.now().timestamp())}"


# ---------------------------------------------------------------------------
# Queue token generator
# ---------------------------------------------------------------------------

# Department → prefix letter mapping (keep in sync with seed data)
_DEPT_PREFIX = {
    "cardiology":       "A",
    "orthopedics":      "B",
    "general medicine": "C",
    "pediatrics":       "D",
    "dermatology":      "E",
    "neurology":        "F",
    "radiology":        "G",
    "oncology":         "H",
    "urology":          "I",
    "gynecology":       "J",
    "ent":              "K",
    "ophthalmology":    "L",
    "psychiatry":       "M",
    "emergency":        "Z",
}


def generate_token(department_name: str, existing_tokens: list) -> str:
    """
    Generate the next sequential token for a department.
    e.g. first token for Cardiology → A01, next → A02.

    Parameters
    ----------
    department_name : str   Name of the department.
    existing_tokens : list  All token_number strings already issued today.
    """
    dept_key = (department_name or "").lower().strip()
    prefix = _DEPT_PREFIX.get(dept_key)
    if not prefix:
        # Use first letter of department name (upper-cased) as fallback
        prefix = dept_key[0].upper() if dept_key else "X"

    # Find the highest number already issued for this prefix today
    highest = 0
    for tok in existing_tokens:
        if tok and tok.startswith(prefix):
            try:
                num = int(tok[len(prefix):])
                if num > highest:
                    highest = num
            except ValueError:
                pass

    next_num = highest + 1
    return f"{prefix}{next_num:02d}"