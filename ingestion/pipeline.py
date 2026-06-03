"""
End-to-end ingestion pipeline.

Given an Output Files folder:
  1. Parse the two active ISO movement txt files (Y, IR90) — static filenames.
     I and T are kept in ISO_TABLE for historical queries but are no longer ingested.
  2. Parse CMJ/PPU trial files (CMJ1.txt, CMJ2.txt, PPU1.txt, …) — Athletic Screen format.
     For each trial:
       a. Parse 5-column summary data.
       b. Load matching *_Power.txt and run full power-curve analysis (including phase metrics).
       c. Upsert row into f_readiness_screen_{cmj|ppu} with inline power and phase metrics.
       d. Persist curve metrics to f_readiness_screen_power_curve (scoring reads from there).
  3. Optionally upsert grip strength row from grip_payload (manual entry).
  4. Resolve athlete UUIDs — match-only, never create.
  5. Update has_readiness_screen_data flag in d_athletes.
  6. Compute composite readiness score.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

import numpy as np

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
    update_athlete_age_group,
    update_athlete_data_flag,
)
from .file_parsers import (
    ASCII_FILES,
    discover_txt_files,
    discover_cmj_ppu_trials,
    find_session_xml,
    normalize_gender,
    parse_session_xml,
    parse_txt_file,
    peek_file_date,
)
from .power_analysis import (
    analyze_phase_metrics_from_force,
    analyze_power_curve_advanced,
    load_power_txt,
)
from .units import inches_to_meters
from .scoring import score_session


# Fact tables for the four ISO movements.
ISO_TABLE = {
    "I":    "f_readiness_screen_i",
    "Y":    "f_readiness_screen_y",
    "T":    "f_readiness_screen_t",
    "IR90": "f_readiness_screen_ir90",
}

CMJ_PPU_TABLE = {
    "CMJ": "f_readiness_screen_cmj",
    "PPU": "f_readiness_screen_ppu",
}

# All power-curve columns written inline into the CMJ/PPU fact table.
POWER_CURVE_COLS = [
    "peak_power_w", "time_to_peak_s", "rpd_max_w_per_s", "time_to_rpd_max_s",
    "rise_time_10_90_s", "fwhm_s", "auc_j", "work_early_pct", "decay_90_10_s",
    "t_com_norm_0to1", "skewness", "kurtosis", "spectral_centroid_hz",
]

# Phase-analysis columns added in v2 — written to both CMJ/PPU and power_curve tables.
PHASE_COLS = [
    "contraction_time_s", "eccentric_duration_s", "concentric_duration_s",
    "ecc_con_duration_ratio", "eccentric_mean_power_w", "eccentric_peak_power_w",
    "eccentric_auc_j", "concentric_auc_j", "mrsi",
]

# Force-derived columns added in v2.1 — written to CMJ/PPU tables only.
FORCE_COLS = [
    "peak_grf_n", "peak_grf_bw_ratio", "rfd_0_100ms", "concentric_impulse_ns",
]


def _emit(log: Callable[[str], None], stage: str, msg: str) -> None:
    log(f"[{stage}] {msg}")


def _safe(v):
    """Convert numpy scalars / NaN to plain Python types for psycopg2."""
    if v is None:
        return None
    try:
        import math
        import numpy as np
        if isinstance(v, (np.integer, np.floating)):
            v = v.item()
        if isinstance(v, float) and math.isnan(v):
            return None
    except ImportError:
        pass
    return v


def run_ingestion(
    output_dir: str,
    power_dir: Optional[str] = None,
    fs_hz: float = 1000.0,
    log: Callable[[str], None] = print,
    athlete_uuid_override: Optional[str] = None,
    cancel_event=None,
    grip_payload: Optional[Dict] = None,
) -> Dict:
    """Run the full pipeline against `output_dir`.

    grip_payload (optional): {left_kg, right_kg, dominant_hand, notes} for manual
    grip-strength entry. When provided, upserts a row into f_readiness_screen_grip.
    """
    summary: Dict = {
        "output_dir":       output_dir,
        "files_found":      {},
        "rows_inserted":    0,
        "rows_updated":     0,
        "athletes":         [],
        "power_curve_rows": 0,
        "scores":           [],
        "errors":           [],
    }

    if power_dir is None:
        power_dir = output_dir

    if not os.path.isdir(output_dir):
        msg = f"Folder not found: {output_dir}"
        _emit(log, "ERROR", msg)
        summary["errors"].append(msg)
        return summary

    # ---- Discover files -----------------------------------------------
    iso_files = discover_txt_files(output_dir)
    cmj_ppu_trials = discover_cmj_ppu_trials(output_dir)

    summary["files_found"] = {
        **{m: os.path.basename(p) for m, p in iso_files.items()},
        **{t["trial_name"]: os.path.basename(t["file_path"]) for t in cmj_ppu_trials},
    }

    if not iso_files and not cmj_ppu_trials:
        _emit(log, "scan", "No movement files found. Nothing to do.")
        return summary

    _emit(log, "scan", f"Scanning {output_dir}")
    for m, p in iso_files.items():
        _emit(log, "scan", f"  found {m} -> {os.path.basename(p)}")
    for t in cmj_ppu_trials:
        _emit(log, "scan", f"  found {t['movement_type']} trial -> {os.path.basename(t['file_path'])}")

    # ---- Date filtering --------------------------------------------------
    # Peek line 0 of every discovered file to collect session dates cheaply.
    all_file_dates: Dict[str, Optional[str]] = {}
    for _fp in list(iso_files.values()) + [t["file_path"] for t in cmj_ppu_trials]:
        all_file_dates[_fp] = peek_file_date(_fp)

    unique_dates = {d for d in all_file_dates.values() if d}
    today_str = date.today().strftime("%Y-%m-%d")

    if today_str in unique_dates:
        target_date = today_str
        _emit(log, "filter", f"Today's date {target_date} found — processing only today's files")
    elif unique_dates:
        target_date = max(unique_dates)
        _emit(log, "filter", f"No files from today — processing most recent date: {target_date}")
    else:
        target_date = None
        _emit(log, "filter", "Could not determine any date from files — processing all")

    # Session.xml — used to default gender.
    session_gender = "Male"
    xml_path = find_session_xml(output_dir)
    if xml_path:
        try:
            xml_data = parse_session_xml(xml_path)
            session_gender = normalize_gender(xml_data.get("gender"))
            _emit(log, "session", f"Session.xml: name={xml_data.get('name')}, gender={session_gender}")
        except Exception as e:
            _emit(log, "session", f"Could not parse Session.xml ({e}); defaulting gender=Male")

    sessions_seen: set = set()
    athletes_meta: Dict[str, str] = {}  # uuid -> display name

    conn = get_connection()
    try:
        # ================================================================
        # Part 1: ISO movements (I, Y, T, IR90) — unchanged logic
        # ================================================================
        for movement, file_path in iso_files.items():
            try:
                if cancel_event and cancel_event.is_set():
                    _emit(log, "cancelled", "Run cancelled by user.")
                    break

                file_date = all_file_dates.get(file_path)
                if target_date is not None and file_date != target_date:
                    _emit(log, "skip",
                          f"{os.path.basename(file_path)}: date {file_date} does not match target {target_date}")
                    continue

                parsed = parse_txt_file(file_path, movement)
                if not parsed:
                    _emit(log, "parse", f"  {movement}: failed to parse {os.path.basename(file_path)}")
                    summary["errors"].append(f"parse failed: {file_path}")
                    continue

                name = parsed["name"]
                date_str = parsed["date"]

                athlete_uuid, date_str = _resolve_athlete(
                    name, date_str, athlete_uuid_override, session_gender,
                    athletes_meta, log, summary,
                )
                if athlete_uuid is None:
                    continue

                update_athlete_data_flag(athlete_uuid)

                age_at_collection, age_group, date_str = _calc_age(athlete_uuid, date_str)

                table = ISO_TABLE[movement]
                # When UUID override is active the file-path name may belong to a
                # different person. Use the resolved athlete's display name instead.
                resolved_name = athletes_meta.get(athlete_uuid, name)
                src_id = extract_source_athlete_id(resolved_name)

                insert_data = {
                    "athlete_uuid":      athlete_uuid,
                    "session_date":      date_str,
                    "source_system":     "readiness_screen",
                    "source_athlete_id": src_id,
                    "age_at_collection": age_at_collection,
                    "age_group":         age_group,
                    "avg_force":         parsed.get("Avg_Force"),
                    "avg_force_norm":    parsed.get("Avg_Force_Norm"),
                    "max_force":         parsed.get("Max_Force"),
                    "max_force_norm":    parsed.get("Max_Force_Norm"),
                    "time_to_max":       parsed.get("Time_to_Max"),
                }
                update_cols = [
                    "source_athlete_id",
                    "avg_force", "avg_force_norm", "max_force", "max_force_norm",
                    "time_to_max", "age_at_collection", "age_group",
                ]

                verb = _upsert(conn, table, insert_data, update_cols,
                               "athlete_uuid = %s AND session_date = %s",
                               (athlete_uuid, date_str))
                if verb == "inserted":
                    summary["rows_inserted"] += 1
                else:
                    summary["rows_updated"] += 1

                update_athlete_age_group(athlete_uuid, age_group)
                _emit(log, "upsert", f"  {table}: {verb} {date_str}")
                sessions_seen.add((athlete_uuid, date_str))

            except Exception as e:
                conn.rollback()
                msg = f"{movement} ({os.path.basename(file_path)}): {e}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)

        # ================================================================
        # Part 2: CMJ/PPU trials — Athletic Screen style
        # ================================================================

        # Pre-estimate body weight from the first available CMJ Force file.
        # PPU quiet standing ≠ full BW (upper body only on plate), so BW must
        # come from a CMJ trial.  Used by analyze_phase_metrics_from_force.
        athlete_body_weight_n: Optional[float] = None
        for _t in cmj_ppu_trials:
            if _t["movement_type"] == "CMJ":
                _cf = os.path.join(power_dir, f"{_t['trial_name']}_Force.txt")
                if os.path.isfile(_cf):
                    try:
                        _fa = load_power_txt(_cf)
                        _qn = max(10, int(0.05 * fs_hz))
                        athlete_body_weight_n = float(np.mean(_fa[:_qn]))
                        _emit(log, "power",
                              f"  BW estimated from {_t['trial_name']}_Force.txt: "
                              f"{athlete_body_weight_n:.1f} N "
                              f"({athlete_body_weight_n / 9.81:.1f} kg)")
                    except Exception:
                        pass
                    break

        for trial in cmj_ppu_trials:
            movement   = trial["movement_type"]
            trial_name = trial["trial_name"]
            file_path  = trial["file_path"]
            try:
                if cancel_event and cancel_event.is_set():
                    _emit(log, "cancelled", "Run cancelled by user.")
                    break

                file_date = all_file_dates.get(file_path)
                if target_date is not None and file_date != target_date:
                    _emit(log, "skip",
                          f"{os.path.basename(file_path)}: date {file_date} does not match target {target_date}")
                    continue

                parsed = parse_txt_file(file_path, movement, folder_path=output_dir)
                if not parsed:
                    _emit(log, "parse", f"  {trial_name}: failed to parse {os.path.basename(file_path)}")
                    summary["errors"].append(f"parse failed: {file_path}")
                    continue

                name     = parsed["name"]
                date_str = parsed["date"]

                athlete_uuid, date_str = _resolve_athlete(
                    name, date_str, athlete_uuid_override, session_gender,
                    athletes_meta, log, summary,
                )
                if athlete_uuid is None:
                    continue

                update_athlete_data_flag(athlete_uuid)

                age_at_collection, age_group, date_str = _calc_age(athlete_uuid, date_str)

                # Load and analyse the matching Power.txt file.
                power_metrics: Dict = {}
                phase_metrics: Dict = {}
                force_metrics: Dict = {}
                power_file = os.path.join(power_dir, f"{trial_name}_Power.txt")
                if os.path.isfile(power_file):
                    try:
                        pw_arr = load_power_txt(power_file)
                        jh_m = inches_to_meters(_safe(parsed.get("JH_IN")))
                        pa = analyze_power_curve_advanced(
                            pw_arr, fs_hz=fs_hz,
                            jump_height_m=jh_m,
                            movement_type=movement,
                        )
                        power_metrics = {k: _safe(pa.get(k)) for k in POWER_CURVE_COLS}
                        phase_metrics = {k: _safe(pa.get(k)) for k in PHASE_COLS}
                        _emit(log, "power", f"  {trial_name}: power curve analysed ({len(pw_arr)} samples)")
                    except Exception as pe:
                        _emit(log, "power", f"  {trial_name}: power analysis failed ({pe})")
                else:
                    _emit(log, "power", f"  {trial_name}: no Power.txt found — skipping curve")

                # Force file: overrides phase_metrics with GRF-derived values.
                # The Force.txt covers the full trial (quiet standing + eccentric + concentric)
                # so eccentric columns can be computed.  Reuses load_power_txt — same format.
                force_file = os.path.join(power_dir, f"{trial_name}_Force.txt")
                if os.path.isfile(force_file):
                    try:
                        frc_arr = load_power_txt(force_file)
                        jh_m_f = inches_to_meters(_safe(parsed.get("JH_IN")))
                        fp = analyze_phase_metrics_from_force(
                            frc_arr, fs_hz=fs_hz,
                            jump_height_m=jh_m_f,
                            movement_type=movement,
                            body_weight_n=athlete_body_weight_n,
                        )
                        phase_metrics = {k: _safe(fp.get(k)) for k in PHASE_COLS}
                        force_metrics = {k: _safe(fp.get(k)) for k in FORCE_COLS}
                        _emit(log, "power", f"  {trial_name}: force phase metrics computed ({len(frc_arr)} samples)")
                    except Exception as fe:
                        _emit(log, "power", f"  {trial_name}: force phase analysis failed ({fe})")

                table  = CMJ_PPU_TABLE[movement]
                # When UUID override is active the file-path name may belong to a
                # different person. Use the resolved athlete's display name instead.
                resolved_name = athletes_meta.get(athlete_uuid, name)
                src_id = extract_source_athlete_id(resolved_name)

                insert_data = {
                    "athlete_uuid":      athlete_uuid,
                    "session_date":      date_str,
                    "source_system":     "readiness_screen",
                    "source_athlete_id": src_id,
                    "trial_name":        trial_name,
                    "trial_id":          _trial_id_from_name(trial_name),
                    "age_at_collection": age_at_collection,
                    "age_group":         age_group,
                    "jump_height":       _safe(parsed.get("JH_IN")),
                    "peak_power":        _safe(parsed.get("Peak_Power")),
                    "peak_force":        None,
                    "pp_w_per_kg":       _safe(parsed.get("PP_W_per_kg")),
                    "pp_forceplate":     _safe(parsed.get("PP_FORCEPLATE")),
                    "force_at_pp":       _safe(parsed.get("Force_at_PP")),
                    "vel_at_pp":         _safe(parsed.get("Vel_at_PP")),
                    **power_metrics,
                    **phase_metrics,
                    **force_metrics,
                }
                update_cols = [
                    "source_athlete_id",
                    "jump_height", "peak_power", "peak_force",
                    "pp_w_per_kg", "pp_forceplate", "force_at_pp", "vel_at_pp",
                    "age_at_collection", "age_group", "trial_id",
                    *POWER_CURVE_COLS,
                    *PHASE_COLS,
                    *FORCE_COLS,
                ]
                # Only update columns that exist in insert_data.
                update_cols = [c for c in update_cols if c in insert_data]

                verb = _upsert(
                    conn, table, insert_data, update_cols,
                    # IS NOT DISTINCT FROM is NULL-safe: treats NULL = NULL as TRUE,
                    # preventing duplicate INSERTs when trial_name is NULL (pre-migration rows).
                    "athlete_uuid = %s AND session_date = %s AND trial_name IS NOT DISTINCT FROM %s",
                    (athlete_uuid, date_str, trial_name),
                )
                if verb == "inserted":
                    summary["rows_inserted"] += 1
                else:
                    summary["rows_updated"] += 1

                update_athlete_age_group(athlete_uuid, age_group)
                _emit(log, "upsert", f"  {table}: {verb} {date_str} (trial={trial_name})")
                sessions_seen.add((athlete_uuid, date_str))

                # Also write to f_readiness_screen_power_curve (scoring reads from it).
                if power_metrics:
                    trial_id = _trial_id_from_name(trial_name)
                    try:
                        pw_arr_full = load_power_txt(power_file)
                        jh_m_full = inches_to_meters(_safe(parsed.get("JH_IN")))
                        pa_full = analyze_power_curve_advanced(
                            pw_arr_full, fs_hz=fs_hz,
                            jump_height_m=jh_m_full,
                            movement_type=movement,
                        )
                        pa_full["source_file"] = power_file
                        pa_full["fs_hz"] = fs_hz
                        _persist_power_curve(athlete_uuid, date_str, movement, trial_id, pa_full)
                        summary["power_curve_rows"] += 1
                    except Exception as pce:
                        _emit(log, "power", f"  {trial_name}: power_curve table write failed ({pce})")

            except Exception as e:
                conn.rollback()
                msg = f"{trial_name} ({os.path.basename(file_path)}): {e}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)

        # ================================================================
        # Part 3: Grip strength (manual entry via grip_payload)
        # ================================================================
        if grip_payload and sessions_seen:
            try:
                left_kg  = grip_payload.get("left_kg")
                right_kg = grip_payload.get("right_kg")
                if left_kg is not None and right_kg is not None:
                    left_kg  = float(left_kg)
                    right_kg = float(right_kg)
                    avg_kg        = (left_kg + right_kg) / 2.0
                    max_kg        = max(left_kg, right_kg)
                    asymmetry_pct = 100.0 * abs(left_kg - right_kg) / max_kg if max_kg > 0 else None

                    # Use first athlete-session we resolved (grip is one row per session).
                    grip_uuid, grip_date = next(iter(sessions_seen))
                    age_at_collection, age_group, grip_date = _calc_age(grip_uuid, grip_date)
                    resolved_name = athletes_meta.get(grip_uuid, "")
                    src_id = extract_source_athlete_id(resolved_name)

                    grip_data = {
                        "athlete_uuid":      grip_uuid,
                        "session_date":      grip_date,
                        "source_system":     "readiness_screen",
                        "source_athlete_id": src_id,
                        "age_at_collection": age_at_collection,
                        "age_group":         age_group,
                        "left_kg":           left_kg,
                        "right_kg":          right_kg,
                        "avg_kg":            avg_kg,
                        "max_kg":            max_kg,
                        "asymmetry_pct":     asymmetry_pct,
                        "dominant_hand":     grip_payload.get("dominant_hand"),
                        "entry_source":      "manual",
                        "notes":             grip_payload.get("notes"),
                    }
                    grip_update_cols = [
                        "left_kg", "right_kg", "avg_kg", "max_kg", "asymmetry_pct",
                        "dominant_hand", "entry_source", "notes",
                        "age_at_collection", "age_group",
                    ]
                    verb = _upsert(
                        conn, "f_readiness_screen_grip", grip_data, grip_update_cols,
                        "athlete_uuid = %s AND session_date = %s",
                        (grip_uuid, grip_date),
                    )
                    if verb == "inserted":
                        summary["rows_inserted"] += 1
                    else:
                        summary["rows_updated"] += 1
                    _emit(log, "upsert", f"  f_readiness_screen_grip: {verb} {grip_date} "
                          f"(L={left_kg:.1f} R={right_kg:.1f} asym={asymmetry_pct:.1f}%)")
            except Exception as ge:
                conn.rollback()
                msg = f"grip upsert failed: {ge}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)

    finally:
        conn.close()

    # ---- Composite score ---------------------------------------------------
    for (athlete_uuid, date_str) in sessions_seen:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            score = score_session(athlete_uuid, d)
            summary["scores"].append({
                "athlete_uuid": athlete_uuid,
                "name":         athletes_meta.get(athlete_uuid, ""),
                "session_date": date_str,
                **score,
            })
            _emit(log, "score",
                  f"  {athletes_meta.get(athlete_uuid, athlete_uuid)} {date_str}: "
                  f"{score.get('band')} ({score.get('composite_score')})")
        except Exception as e:
            msg = f"score {athlete_uuid} {date_str}: {e}"
            _emit(log, "ERROR", msg)
            summary["errors"].append(msg)

    # Refresh all has_*_data boolean flags on d_athletes via warehouse stored procedure.
    if sessions_seen and not (cancel_event and cancel_event.is_set()):
        try:
            call_update_athlete_data_flags()
            _emit(log, "flags", "Athlete data flags refreshed")
        except Exception as e:
            _emit(log, "flags", f"Flag refresh skipped: {e}")

    summary["athletes"] = [{"uuid": u, "name": n} for u, n in athletes_meta.items()]
    _emit(
        log, "done",
        f"inserted={summary['rows_inserted']} updated={summary['rows_updated']} "
        f"power_rows={summary['power_curve_rows']} scored={len(summary['scores'])}",
    )
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_athlete(
    name: str,
    date_str: str,
    athlete_uuid_override: Optional[str],
    session_gender: str,
    athletes_meta: Dict[str, str],
    log: Callable,
    summary: Dict,
) -> tuple:
    """Return (athlete_uuid, date_str) or (None, date_str) on failure."""
    if athlete_uuid_override:
        athlete_uuid = athlete_uuid_override
    else:
        try:
            athlete_uuid, _ = get_or_create_athlete(
                name=name,
                source_system="readiness_screen",
                source_athlete_id=extract_source_athlete_id(name),
                gender=session_gender,
            )
        except ValueError as ve:
            _emit(log, "ERROR", f"  {name}: {ve}")
            summary["errors"].append(str(ve))
            return None, date_str

    if athlete_uuid not in athletes_meta:
        athletes_meta[athlete_uuid] = name
        _emit(log, "athlete", f"  {name} -> {athlete_uuid} (matched)")

    return athlete_uuid, date_str


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


def _upsert(conn, table: str, insert_data: Dict, update_cols: List[str],
            where_clause: str, where_params: tuple) -> str:
    """INSERT or UPDATE a row. Returns 'inserted' or 'updated'."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM public.{table} WHERE {where_clause} LIMIT 1",
            where_params,
        )
        exists = cur.fetchone() is not None

        if exists:
            set_clause = ", ".join(f"{c} = %s" for c in update_cols)
            params = [insert_data[c] for c in update_cols] + list(where_params)
            cur.execute(
                f"UPDATE public.{table} SET {set_clause} WHERE {where_clause}",
                params,
            )
            verb = "updated"
        else:
            cols = list(insert_data.keys())
            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO public.{table} ({', '.join(cols)}) VALUES ({placeholders})",
                [insert_data[c] for c in cols],
            )
            verb = "inserted"

    conn.commit()
    return verb


def _trial_id_from_name(trial_name: str) -> int:
    """Extract trailing integer from trial name, e.g. 'CMJ1' -> 1, 'PPU2' -> 2."""
    m = re.search(r"(\d+)\s*$", trial_name)
    return int(m.group(1)) if m else 1


def _persist_power_curve(athlete_uuid: str, date_str: str, movement: str,
                          trial_id: int, m: dict) -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.f_readiness_screen_power_curve (
                    athlete_uuid, session_date, movement_type, trial_id, source_file, fs_hz,
                    n_samples, peak_power_w, time_to_peak_s, rise_time_10_90_s,
                    rise_slope_w_per_s, fwhm_s, auc_j, t_com_s, t_com_norm_0to1, cv_local_peak,
                    rpd_max_w_per_s, time_to_rpd_max_s, auc_pre_j, auc_post_j, work_early_pct,
                    decay_90_10_s, skewness, kurtosis, spectral_centroid_hz,
                    contraction_time_s, eccentric_duration_s, concentric_duration_s,
                    ecc_con_duration_ratio, eccentric_mean_power_w, eccentric_peak_power_w,
                    eccentric_auc_j, concentric_auc_j, mrsi
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (athlete_uuid, session_date, movement_type, trial_id) DO UPDATE SET
                    source_file             = EXCLUDED.source_file,
                    fs_hz                   = EXCLUDED.fs_hz,
                    n_samples               = EXCLUDED.n_samples,
                    peak_power_w            = EXCLUDED.peak_power_w,
                    time_to_peak_s          = EXCLUDED.time_to_peak_s,
                    rise_time_10_90_s       = EXCLUDED.rise_time_10_90_s,
                    rise_slope_w_per_s      = EXCLUDED.rise_slope_w_per_s,
                    fwhm_s                  = EXCLUDED.fwhm_s,
                    auc_j                   = EXCLUDED.auc_j,
                    t_com_s                 = EXCLUDED.t_com_s,
                    t_com_norm_0to1         = EXCLUDED.t_com_norm_0to1,
                    cv_local_peak           = EXCLUDED.cv_local_peak,
                    rpd_max_w_per_s         = EXCLUDED.rpd_max_w_per_s,
                    time_to_rpd_max_s       = EXCLUDED.time_to_rpd_max_s,
                    auc_pre_j               = EXCLUDED.auc_pre_j,
                    auc_post_j              = EXCLUDED.auc_post_j,
                    work_early_pct          = EXCLUDED.work_early_pct,
                    decay_90_10_s           = EXCLUDED.decay_90_10_s,
                    skewness                = EXCLUDED.skewness,
                    kurtosis                = EXCLUDED.kurtosis,
                    spectral_centroid_hz    = EXCLUDED.spectral_centroid_hz,
                    contraction_time_s      = EXCLUDED.contraction_time_s,
                    eccentric_duration_s    = EXCLUDED.eccentric_duration_s,
                    concentric_duration_s   = EXCLUDED.concentric_duration_s,
                    ecc_con_duration_ratio  = EXCLUDED.ecc_con_duration_ratio,
                    eccentric_mean_power_w  = EXCLUDED.eccentric_mean_power_w,
                    eccentric_peak_power_w  = EXCLUDED.eccentric_peak_power_w,
                    eccentric_auc_j         = EXCLUDED.eccentric_auc_j,
                    concentric_auc_j        = EXCLUDED.concentric_auc_j,
                    mrsi                    = EXCLUDED.mrsi
                """,
                (
                    athlete_uuid, date_str, movement, trial_id,
                    m.get("source_file"), m.get("fs_hz"),
                    _safe(m.get("n_samples")), _safe(m.get("peak_power_w")),
                    _safe(m.get("time_to_peak_s")), _safe(m.get("rise_time_10_90_s")),
                    _safe(m.get("rise_slope_w_per_s")), _safe(m.get("fwhm_s")),
                    _safe(m.get("auc_j")), _safe(m.get("t_com_s")),
                    _safe(m.get("t_com_norm_0to1")), _safe(m.get("cv_local_peak")),
                    _safe(m.get("rpd_max_w_per_s")), _safe(m.get("time_to_rpd_max_s")),
                    _safe(m.get("auc_pre_j")), _safe(m.get("auc_post_j")),
                    _safe(m.get("work_early_pct")), _safe(m.get("decay_90_10_s")),
                    _safe(m.get("skewness")), _safe(m.get("kurtosis")),
                    _safe(m.get("spectral_centroid_hz")),
                    _safe(m.get("contraction_time_s")), _safe(m.get("eccentric_duration_s")),
                    _safe(m.get("concentric_duration_s")), _safe(m.get("ecc_con_duration_ratio")),
                    _safe(m.get("eccentric_mean_power_w")), _safe(m.get("eccentric_peak_power_w")),
                    _safe(m.get("eccentric_auc_j")), _safe(m.get("concentric_auc_j")),
                    _safe(m.get("mrsi")),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# CLI entry
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run readiness ingestion against an Output Files folder.")
    parser.add_argument("output_dir")
    parser.add_argument("--power-dir", default=None)
    parser.add_argument("--fs-hz", type=float, default=1000.0)
    parser.add_argument("--athlete-uuid", default=None)
    args = parser.parse_args()

    summary = run_ingestion(
        args.output_dir,
        power_dir=args.power_dir,
        fs_hz=args.fs_hz,
        athlete_uuid_override=args.athlete_uuid,
    )
    print()
    print("SUMMARY:", summary)
    sys.exit(0 if not summary["errors"] else 1)
