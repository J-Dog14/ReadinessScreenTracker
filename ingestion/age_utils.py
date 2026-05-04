"""
Age + age_group calculation. Mirrors backend's common.age_utils.

Canonical age groups (single source of truth, must match the backend):
  - YOUTH:        age < 14
  - HIGH SCHOOL:  14 <= age <= 18
  - COLLEGE:      18 < age <= 22
  - PRO:          age > 22
"""
from datetime import date, datetime
from typing import Optional


def calculate_age(date_of_birth: Optional[date], reference_date: Optional[date] = None) -> Optional[float]:
    if not date_of_birth:
        return None
    if reference_date is None:
        reference_date = date.today()
    try:
        return (reference_date - date_of_birth).days / 365.25
    except Exception:
        return None


def calculate_age_at_collection(session_date: Optional[date], date_of_birth: Optional[date]) -> Optional[float]:
    if not date_of_birth or not session_date:
        return None
    return calculate_age(date_of_birth, session_date)


def normalize_session_date(session_date: Optional[date], reference_date: Optional[date] = None) -> Optional[date]:
    """If a session date is more than two years from today, pin to today (matches backend behavior)."""
    if session_date is None:
        return None
    if reference_date is None:
        reference_date = date.today()
    try:
        delta_days = abs((session_date - reference_date).days)
        if delta_days > 730:
            return reference_date
        return session_date
    except Exception:
        return session_date


def calculate_age_group(age: Optional[float]) -> Optional[str]:
    if age is None:
        return None
    if age < 14:
        return "YOUTH"
    if 14 <= age <= 18:
        return "HIGH SCHOOL"
    if 18 < age <= 22:
        return "COLLEGE"
    return "PRO"


def parse_date(date_str) -> Optional[date]:
    if not date_str:
        return None
    if isinstance(date_str, date):
        return date_str
    if isinstance(date_str, datetime):
        return date_str.date()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(date_str).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None
