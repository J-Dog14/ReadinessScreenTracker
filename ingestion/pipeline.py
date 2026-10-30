"""
End-to-end ingestion pipeline (Readiness Screen v2 - straight from c3d).

The Visual3D Output Files step is retired: its CMJ/PPU power was wrong (velocity integrated forward
from frame 1). This reads each readiness session's c3d + session.xml directly - the same processor
the backend uses (ingestion/c3d, vendored from OctaneBiomechBackend readinessScreen/v2) - and writes
the same warehouse tables:

    public.f_readiness_screen_v2_cmj / _ppu    one row per jump trial
    public.f_readiness_screen_v2_iso           I / Y / T / IR90, one row per trial
    public.f_readiness_screen_v2_force_curve   offset-corrected FP3 Fz per trial

Readers (scoring, dashboard) use the v_readiness_screen_* views over those tables.

Given a folder:
  * a readiness session folder (has session.xml)            -> that session
  * the QTM Data root (D:/Athletic Screen 2.0/Data)          -> readiness sessions dated today,
    otherwise the most recent readiness date (same rule the Output Files flow used).
    With an athlete selected, only that athlete's folders are considered.
Then per session: process c3d -> resolve athlete (match only, never create) -> replace the session's
v2 rows + curves -> grip (manual entry) -> flags -> composite score.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from db.connection import get_connection
from .age_utils import (
    calculate_age_at_collection,
    calculate_age_group,
    normalize_session_date,
    parse_date,
)
from .athlete_manager import (
    call_update_athlete_data_flags,
    extract_source_athlete_id,
    get_athlete_dob,
    get_or_create_athlete,
    normalize_name_for_matching,
    update_athlete_age_group,
    update_athlete_data_flag,
)
from .c3d import PROCESSOR_VERSION, schema
from .c3d.process import ISO, process_session
from .c3d.session_meta import parse_session_xml
from .scoring import score_session

SESSION_TYPE = "readiness screen"
TEST_RE = re.compile(r"^\s*(?:test|sample)", re.I)
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def _emit(log: Callable[[str], None], stage: str, msg: str) -> None:
    log(f"[{stage}] {msg}")


def _safe(v):
    """Convert numpy scalars / NaN to plain Python types for psycopg2."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return float(v) if np.isfinite(v) else None
    if isinstance(v, np.bool_):
        return bool(v)
    return v


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------

def _is_readiness(folder: Path) -> bool:
    try:
        meta = parse_session_xml(folder / "session.xml")
    except Exception:
        return False
    return SESSION_TYPE in {t.lower() for t in meta["session_types"]}


def find_readiness_sessions(data_dir: str, athlete_name: Optional[str] = None,
                            log: Callable = print) -> tuple:
    """Return (session folders to ingest, data_root, target_date)."""
    root = Path(data_dir)
    if (root / "session.xml").exists():
        return [root], root.parent, None
    folders = []
    for ath in sorted(p for p in root.iterdir() if p.is_dir() and not TEST_RE.match(p.name)):
        if athlete_name:
            nm = normalize_name_for_matching(ath.name.rpartition("_")[0] or ath.name)
            if SequenceMatcher(None, nm, athlete_name).ratio() < 0.85:
                continue
        for ses in ath.iterdir():
            if ses.is_dir() and DATE_RE.match(ses.name) and (ses / "session.xml").exists():
                folders.append(ses)
    # newest first; only parse session.xml until the target date is settled
    folders.sort(key=lambda p: p.name[:10], reverse=True)
    target, picked = None, []
    for f in folders:
        d = f.name[:10]
        if target is not None and d < target:
            break
        if not _is_readiness(f):
            continue
        if target is None:
            target = d                     # newest readiness date (today's, when there is one)
        if d == target:
            picked.append(f)
    return picked, root, target


# ---------------------------------------------------------------------------
# Athlete + age
# ---------------------------------------------------------------------------

def _folder_history(conn, ath_folder: str) -> List[str]:
    """athlete_uuid(s) this QTM athlete folder is already linked to (readiness v2, then athletic screen v2)."""
    with conn.cursor() as cur:
        for sql in (
            " UNION ".join(f"SELECT DISTINCT athlete_uuid FROM public.f_readiness_screen_v2_{t} "
                           f"WHERE source_athlete_id = %s AND source_root <> 'legacy'" for t in ("cmj", "ppu", "iso")),
            " UNION ".join(f"SELECT DISTINCT athlete_uuid FROM public.f_athletic_screen_v2_{t} "
                           f"WHERE source_athlete_id = %s" for t in ("cmj", "dj", "ppu", "slv")),
        ):
            n = sql.count("%s")
            try:
                cur.execute(sql, (ath_folder,) * n)
                u = [r[0] for r in cur.fetchall()]
            except Exception:
                conn.rollback()
                u = []
            if len(u) == 1:
                return u
    return []


def _resolve_athlete(conn, srow: Dict, athlete_uuid_override: Optional[str],
                     athletes_meta: Dict[str, str], log: Callable, summary: Dict) -> Optional[str]:
    name = srow.get("xml_name") or srow["athlete_name"]
    if athlete_uuid_override:
        uuid, how = athlete_uuid_override, "selected"
    else:
        hist = _folder_history(conn, srow["athlete_folder"])
        if hist:
            uuid, how = hist[0], "linked folder"
        else:
            try:
                uuid, _ = get_or_create_athlete(name=name, source_system="readiness_screen",
                                                source_athlete_id=extract_source_athlete_id(name))
                how = "name match"
            except ValueError as ve:
                _emit(log, "ERROR", f"  {name}: {ve}")
                summary["errors"].append(str(ve))
                return None
    if uuid not in athletes_meta:
        athletes_meta[uuid] = name
        _emit(log, "athlete", f"  {name} -> {uuid} ({how})")
    return uuid


def _calc_age(athlete_uuid: str, date_str: str):
    """Return (age_at_collection, age_group, possibly_normalised_date_str)."""
    age_at_collection = None
    age_group = None
    dob = get_athlete_dob(athlete_uuid)
    try:
        session_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        session_date = normalize_session_date(session_date) or session_date
        date_str = session_date.strftime("%Y-%m-%d")
        if dob:
            dob_date = dob if hasattr(dob, "year") else parse_date(str(dob))
            if dob_date:
                age_at_collection = calculate_age_at_collection(session_date, dob_date)
                if age_at_collection is not None and not (0 <= age_at_collection <= 120):
                    age_at_collection = None
                age_group = calculate_age_group(age_at_collection)
    except Exception:
        pass
    return age_at_collection, age_group, date_str


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _upsert(conn, table: str, insert_data: Dict, update_cols: List[str],
            where_clause: str, where_params: tuple) -> str:
    """INSERT or UPDATE one row (used for grip). Returns 'inserted' or 'updated'."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM public.{table} WHERE {where_clause} LIMIT 1", where_params)
        exists = cur.fetchone() is not None
        if exists:
            set_clause = ", ".join(f"{c} = %s" for c in update_cols)
            cur.execute(f"UPDATE public.{table} SET {set_clause} WHERE {where_clause}",
                        [insert_data[c] for c in update_cols] + list(where_params))
            verb = "updated"
        else:
            cols = list(insert_data.keys())
            cur.execute(f"INSERT INTO public.{table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                        [insert_data[c] for c in cols])
            verb = "inserted"
    conn.commit()
    return verb


def _bulk_upsert(conn, table: str, rows: List[Dict], cols) -> int:
    from psycopg2.extras import execute_values
    if not rows:
        return 0
    names = [c for c, _ in cols]
    key = schema.UNIQUE_KEY
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in names if c not in key)
    extra = ", updated_at=now()" if "curve" not in table else ""
    data = [tuple(_safe(r.get(c)) for c in names) for r in rows]
    with conn.cursor() as cur:
        execute_values(cur, f"INSERT INTO {table} ({', '.join(names)}) VALUES %s "
                            f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}{extra}", data, page_size=200)
    return len(data)


def _curve_row(r: Dict, uuid: str) -> Optional[Dict]:
    p = r.get("_curve")
    if not p or not Path(p).exists():
        return None
    z = np.load(p)
    ix = lambda k: int(z[k]) if k in z and int(z[k]) >= 0 else None
    return dict(athlete_uuid=uuid, session_date=r["session_date"], session_folder=r["session_folder"],
                movement=r["movement"], trial_name=r["trial_name"], fs_hz=float(z["fs_hz"]),
                fz_n=[round(float(x), 2) for x in z["fz_n"]],
                bw_n=float(z["bw_n"]) if np.isfinite(z["bw_n"]) else None,
                plate_offset_n=float(z["plate_offset_n"]), first_frame=int(z["first_frame"]),
                takeoff_idx=ix("takeoff_idx"), landing_idx=ix("landing_idx"),
                contact_start_idx=ix("contact_start_idx"), concentric_start_idx=ix("concentric_start_idx"),
                movement_onset_idx=ix("movement_onset_idx"), max_idx=ix("max_idx"),
                processor_version=PROCESSOR_VERSION)


def _write_session(conn, uuid: str, srow: Dict, rows: List[Dict], log: Callable,
                   clear_date: bool = True) -> Dict[str, int]:
    """Replace one session's v2 rows + curves. Returns trials written per table.
    clear_date: drop everything this athlete has on that date first (first folder of the day), so a
    re-ingest never leaves rows from an earlier load - including the backend's merged same-day folders."""
    age, age_group, date_str = _calc_age(uuid, str(srow["session_date"]))
    frames: Dict[str, List[Dict]] = {"cmj": [], "ppu": [], "iso": []}
    for r in rows:
        r = dict(r)
        r.update(athlete_uuid=uuid, session_date=date_str, source_athlete_id=srow["athlete_folder"],
                 source_system="readiness_screen_v2", age_at_collection=age, age_group=age_group,
                 qc_flags=[x for x in str(r.get("qc_flags") or "").split(";") if x])
        frames["iso" if r["movement"] in ISO else r["movement"].lower()].append(r)
    counts = {}
    scope = "" if clear_date else " AND session_folder = %s"
    params = (uuid, date_str) if clear_date else (uuid, date_str, srow["session_folder"])
    with conn.cursor() as cur:
        for table in (schema.TABLE.format("cmj"), schema.TABLE.format("ppu"), schema.ISO_TABLE, schema.CURVE_TABLE):
            cur.execute(f"DELETE FROM {table} WHERE athlete_uuid=%s AND session_date=%s{scope}", params)
    for t, rs in frames.items():
        if not rs:
            continue
        table = schema.ISO_TABLE if t == "iso" else schema.TABLE.format(t)
        cols = schema.iso_columns() if t == "iso" else schema.jump_columns(t)
        counts[t] = _bulk_upsert(conn, table, rs, cols)
        curves = [c for c in (_curve_row(r, uuid) for r in rs) if c]
        _bulk_upsert(conn, schema.CURVE_TABLE, curves, schema.CURVE_COLS)
        flags = sorted({x for r in rs for x in r["qc_flags"]})
        desc = ", ".join(f"{m} {n}" for m, n in pd.Series([r["movement"] for r in rs]).value_counts().items())
        _emit(log, "upsert", f"  {table}: {desc} ({date_str})" + (f"  QC: {', '.join(flags)}" if flags else ""))
    conn.commit()
    update_athlete_age_group(uuid, age_group)
    return counts


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_ingestion(
    output_dir: str,
    power_dir: Optional[str] = None,          # unused (Output Files era); kept for the route signature
    fs_hz: float = 1000.0,                     # unused - the sample rate comes from each c3d
    log: Callable[[str], None] = print,
    athlete_uuid_override: Optional[str] = None,
    cancel_event=None,
    grip_payload: Optional[Dict] = None,
    is_hitter: bool = False,
) -> Dict:
    """Ingest readiness session(s) from c3d. `output_dir` is the QTM Data root or one session folder.

    grip_payload (optional): {left_kg, right_kg, dominant_hand, notes} - upserts f_readiness_screen_grip.
    is_hitter (optional): skip the isometrics (I/Y/T/IR90) and score without iso_z.
    """
    summary: Dict = {"output_dir": output_dir, "files_found": {}, "rows_inserted": 0, "rows_updated": 0,
                     "athletes": [], "power_curve_rows": 0, "scores": [], "errors": []}
    data_dir = output_dir
    if not os.path.isdir(data_dir):
        msg = f"Folder not found: {data_dir}"
        _emit(log, "ERROR", msg)
        summary["errors"].append(msg)
        return summary
    if "output files" in data_dir.lower():
        msg = (f"'{data_dir}' is a Visual3D Output Files folder - that step is retired. "
               f"Point the Tracker at the QTM Data folder (D:/Athletic Screen 2.0/Data) or a session folder.")
        _emit(log, "ERROR", msg)
        summary["errors"].append(msg)
        return summary

    sel_name = None
    if athlete_uuid_override:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM analytics.d_athletes WHERE athlete_uuid = %s", (athlete_uuid_override,))
                r = cur.fetchone()
                sel_name = normalize_name_for_matching(r[0]) if r else None
        finally:
            conn.close()

    _emit(log, "scan", f"Scanning {data_dir} for Readiness Screen sessions (processor {PROCESSOR_VERSION})")
    folders, data_root, target = find_readiness_sessions(data_dir, sel_name, log)
    if not folders:
        _emit(log, "scan", "No readiness sessions found. Nothing to do.")
        return summary
    if target:
        _emit(log, "filter", f"{'Today' if target == date.today().strftime('%Y-%m-%d') else 'Most recent date'}: "
                             f"{target} - {len(folders)} session(s)")

    work = Path(tempfile.mkdtemp(prefix="rs_tracker_"))
    (work / "curves").mkdir()
    sessions_seen: set = set()
    athletes_meta: Dict[str, str] = {}
    cleared: set = set()
    conn = get_connection()
    try:
        for f in folders:
            if cancel_event and cancel_event.is_set():
                _emit(log, "cancelled", "Run cancelled by user.")
                break
            try:
                srow, rows, errs = process_session(f, data_root, work)
                for e in errs:
                    summary["errors"].append(f"{e['trial_name']}: {e['error']}")
                    _emit(log, "c3d", f"  {e['trial_name']}: {e['error']}")
                if not str(srow.get("status", "")).startswith("ok"):
                    _emit(log, "skip", f"  {f.name}: {srow.get('status')}")
                    continue
                for r in rows:
                    r["_curve"] = work / "curves" / f"{srow['source_root']}__{f.parent.name}__{f.name}__{r['trial_name']}.npz"
                if is_hitter:
                    _emit(log, "hitter", "Hitter mode - skipping isometrics (I/Y/T/IR90)")
                    rows = [r for r in rows if r["movement"] not in ISO]
                summary["files_found"].update({f"{f.parent.name}/{f.name}/{r['trial_name']}": r["source_c3d"] for r in rows})
                _emit(log, "c3d", f"  {f.parent.name}/{f.name}: {srow['status']}")

                uuid = _resolve_athlete(conn, srow, athlete_uuid_override, athletes_meta, log, summary)
                if uuid is None:
                    continue
                day = (uuid, _calc_age(uuid, str(srow["session_date"]))[2])
                counts = _write_session(conn, uuid, srow, rows, log, clear_date=day not in cleared)
                cleared.add(day)
                n = sum(counts.values())
                summary["rows_inserted"] += n
                summary["power_curve_rows"] += counts.get("cmj", 0) + counts.get("ppu", 0)
                update_athlete_data_flag(uuid)
                sessions_seen.add(day)
            except Exception as e:
                conn.rollback()
                msg = f"{f.parent.name}/{f.name}: {type(e).__name__}: {e}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)

        # ---- Grip strength (manual entry) ----------------------------------
        if grip_payload and sessions_seen:
            try:
                left_kg, right_kg = grip_payload.get("left_kg"), grip_payload.get("right_kg")
                if left_kg is not None and right_kg is not None:
                    left_kg, right_kg = float(left_kg), float(right_kg)
                    max_kg = max(left_kg, right_kg)
                    asymmetry_pct = 100.0 * abs(left_kg - right_kg) / max_kg if max_kg > 0 else None
                    grip_uuid, grip_date = next(iter(sessions_seen))
                    age_at_collection, age_group, grip_date = _calc_age(grip_uuid, grip_date)
                    grip_data = {
                        "athlete_uuid": grip_uuid, "session_date": grip_date, "source_system": "readiness_screen",
                        "source_athlete_id": extract_source_athlete_id(athletes_meta.get(grip_uuid, "")),
                        "age_at_collection": age_at_collection, "age_group": age_group,
                        "left_kg": left_kg, "right_kg": right_kg, "avg_kg": (left_kg + right_kg) / 2.0,
                        "max_kg": max_kg, "asymmetry_pct": asymmetry_pct,
                        "dominant_hand": grip_payload.get("dominant_hand"), "entry_source": "manual",
                        "notes": grip_payload.get("notes"),
                    }
                    verb = _upsert(conn, "f_readiness_screen_grip", grip_data,
                                   ["left_kg", "right_kg", "avg_kg", "max_kg", "asymmetry_pct", "dominant_hand",
                                    "entry_source", "notes", "age_at_collection", "age_group"],
                                   "athlete_uuid = %s AND session_date = %s", (grip_uuid, grip_date))
                    summary["rows_inserted" if verb == "inserted" else "rows_updated"] += 1
                    _emit(log, "upsert", f"  f_readiness_screen_grip: {verb} {grip_date} "
                                         f"(L={left_kg:.1f} R={right_kg:.1f} asym={asymmetry_pct:.1f}%)")
            except Exception as ge:
                conn.rollback()
                summary["errors"].append(f"grip upsert failed: {ge}")
                _emit(log, "ERROR", f"grip upsert failed: {ge}")
    finally:
        conn.close()

    # ---- Composite score ----------------------------------------------------
    for athlete_uuid, date_str in sorted(sessions_seen, key=lambda x: x[1]):
        try:
            score = score_session(athlete_uuid, datetime.strptime(date_str, "%Y-%m-%d").date(), is_hitter=is_hitter)
            summary["scores"].append({"athlete_uuid": athlete_uuid, "name": athletes_meta.get(athlete_uuid, ""),
                                      "session_date": date_str, **score})
            _emit(log, "score", f"  {athletes_meta.get(athlete_uuid, athlete_uuid)} {date_str}: "
                                f"{score.get('band')} ({score.get('composite_score')})")
        except Exception as e:
            summary["errors"].append(f"score {athlete_uuid} {date_str}: {e}")
            _emit(log, "ERROR", f"score {athlete_uuid} {date_str}: {e}")

    if sessions_seen and not (cancel_event and cancel_event.is_set()):
        try:
            call_update_athlete_data_flags()
            _emit(log, "flags", "Athlete data flags refreshed")
        except Exception as e:
            _emit(log, "flags", f"Flag refresh skipped: {e}")

    summary["athletes"] = [{"uuid": u, "name": n} for u, n in athletes_meta.items()]
    _emit(log, "done", f"trials written={summary['rows_inserted']} sessions={len(sessions_seen)} "
                       f"scored={len(summary['scores'])}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Ingest readiness session(s) from c3d.")
    parser.add_argument("data_dir", help="QTM Data root (D:/Athletic Screen 2.0/Data) or one session folder")
    parser.add_argument("--athlete-uuid", default=None)
    parser.add_argument("--hitter", action="store_true")
    args = parser.parse_args()
    s = run_ingestion(args.data_dir, athlete_uuid_override=args.athlete_uuid, is_hitter=args.hitter)
    print("\nSUMMARY:", {k: v for k, v in s.items() if k != "files_found"})
    sys.exit(0 if not s["errors"] else 1)
