"""
Athlete UUID resolution against analytics.d_athletes.

This is a slimmed-down port of the backend's common.athlete_manager. It uses
the SAME rules — period/comma name normalization, 90% fuzzy similarity, exact
email match before name — so this app resolves to the SAME athlete the backend
would. We do not modify the backend; we just read/write the same dimension
table the backend writes.

Differences from the backend:
  - We do not consult verceldb (the app DB), we only use the warehouse.
    UUIDs minted here will still be picked up by the backend on its next run
    because matching keys on normalized_name.
  - We do not implement source_athlete_map (only kept by the backend; non-
    breaking to skip — a match is a match).
"""
from __future__ import annotations

import logging
import re
import uuid as uuid_pkg
from difflib import SequenceMatcher
from typing import Optional, Tuple

from psycopg2.extras import RealDictCursor

from db.connection import get_connection

NAME_SIMILARITY_THRESHOLD = 0.9
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Name normalization. Must mirror the backend's normalize_name_for_matching
# and normalize_name_for_display so we resolve to the same athlete row.
# ---------------------------------------------------------------------------

def _strip_dates(name: str) -> str:
    name = re.sub(r"\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", "", name)
    name = re.sub(r"\s*\d{4}[/-]\d{1,2}[/-]\d{1,2}", "", name)
    name = re.sub(r"\s*\d{1,2}[/-]\d{1,2}(?![/-]\d)", "", name)
    name = re.sub(r"\s*\d{4}", "", name)
    return name.strip()


def _last_first_to_first_last(name: str) -> str:
    """Treat both ',' and '.' as the last/first separator (backend rule)."""
    if "," in name:
        a, b = (name.split(",", 1) + [""])[:2]
        if a.strip() and b.strip():
            return f"{b.strip()} {a.strip()}"
    elif "." in name:
        a, b = (name.split(".", 1) + [""])[:2]
        if a.strip() and b.strip():
            return f"{b.strip()} {a.strip()}"
    return name


def normalize_name_for_display(name: str) -> str:
    if not name or not name.strip():
        return ""
    name = _strip_dates(name)
    name = _last_first_to_first_last(name)
    return " ".join(name.split())


def normalize_name_for_matching(name: str) -> str:
    if not name or not name.strip():
        return ""
    name = name.replace("_", " ")
    name = _strip_dates(name)
    name = _last_first_to_first_last(name)
    return " ".join(name.split()).upper()


def _strip_for_similarity(name: str) -> str:
    """Backend's _normalize_for_sim — drops trailing initials before fuzzy compare."""
    n = name.replace("_", " ")
    n = re.sub(r"\s+[A-Z]{2,3}\s*$", "", n.strip())
    return n.strip().lower()


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _strip_for_similarity(a), _strip_for_similarity(b)).ratio()


def extract_source_athlete_id(name: str) -> str:
    """Backend's extract_source_athlete_id — trailing UPPER initials -> id, else clean name."""
    if not name or not name.strip():
        return name
    m = re.search(r"\s+([A-Z]{2,3})\s*$", name)
    if m:
        return m.group(1)
    return name


# ---------------------------------------------------------------------------
# Lookup / create.
# ---------------------------------------------------------------------------

def find_existing_athlete(conn, normalized_name: str) -> Optional[dict]:
    """Exact normalized_name first, then 90% fuzzy match."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM analytics.d_athletes
            WHERE normalized_name = %s
            ORDER BY app_db_uuid NULLS LAST, created_at ASC
            LIMIT 1
            """,
            (normalized_name,),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

        cur.execute(
            "SELECT * FROM analytics.d_athletes WHERE normalized_name IS NOT NULL AND normalized_name <> ''"
        )
        best_ratio = NAME_SIMILARITY_THRESHOLD - 1e-6
        best_row = None
        for r in cur.fetchall():
            ratio = _name_similarity(normalized_name, r.get("normalized_name") or "")
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = r
        return dict(best_row) if best_row else None


def get_or_create_athlete(
    name: str,
    source_system: str = "readiness_screen",
    source_athlete_id: Optional[str] = None,
    gender: Optional[str] = None,
) -> Tuple[str, bool]:
    """
    Resolve `name` to an athlete_uuid, creating a row in analytics.d_athletes if needed.
    Returns (uuid, created_flag).
    """
    display_name = normalize_name_for_display(name)
    normalized = normalize_name_for_matching(name)
    if not normalized:
        raise ValueError("Empty name after normalization.")

    conn = get_connection()
    try:
        existing = find_existing_athlete(conn, normalized)
        if existing:
            return str(existing["athlete_uuid"]), False

        new_uuid = str(uuid_pkg.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analytics.d_athletes (
                    athlete_uuid, name, normalized_name,
                    gender, source_system, source_athlete_id
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (athlete_uuid) DO NOTHING
                """,
                (new_uuid, display_name, normalized, gender, source_system, source_athlete_id),
            )
        conn.commit()
        log.info("Created athlete %s (%s)", display_name, new_uuid)
        return new_uuid, True
    finally:
        conn.close()


def update_athlete_age_group(athlete_uuid: str, age_group: Optional[str]) -> None:
    """Set d_athletes.age_group to most-recent insert's age_group (matches backend behavior)."""
    if not age_group:
        return
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE analytics.d_athletes SET age_group = %s WHERE athlete_uuid = %s",
                (age_group, athlete_uuid),
            )
        conn.commit()
    finally:
        conn.close()


def get_athlete_dob(athlete_uuid: str):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date_of_birth FROM analytics.d_athletes WHERE athlete_uuid = %s",
                (athlete_uuid,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def list_athletes_with_readiness():
    """Return [{athlete_uuid, name, age_group}, ...] sorted by name. Used to populate the dashboard dropdown."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT da.athlete_uuid, da.name, da.age_group
                FROM analytics.d_athletes da
                WHERE EXISTS (
                    SELECT 1 FROM public.f_readiness_screen_cmj WHERE athlete_uuid = da.athlete_uuid
                    UNION ALL
                    SELECT 1 FROM public.f_readiness_screen_ppu WHERE athlete_uuid = da.athlete_uuid
                    UNION ALL
                    SELECT 1 FROM public.f_readiness_screen_i   WHERE athlete_uuid = da.athlete_uuid
                    UNION ALL
                    SELECT 1 FROM public.f_readiness_screen_y   WHERE athlete_uuid = da.athlete_uuid
                    UNION ALL
                    SELECT 1 FROM public.f_readiness_screen_t   WHERE athlete_uuid = da.athlete_uuid
                    UNION ALL
                    SELECT 1 FROM public.f_readiness_screen_ir90 WHERE athlete_uuid = da.athlete_uuid
                )
                ORDER BY da.name
                """
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def search_athletes(query: str, limit: int = 25):
    """Substring search for the maintenance page's existing-athlete picker."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT athlete_uuid, name, age_group
                FROM analytics.d_athletes
                WHERE name ILIKE %s OR normalized_name ILIKE %s
                ORDER BY name
                LIMIT %s
                """,
                (f"%{query}%", f"%{query.upper()}%", limit),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
