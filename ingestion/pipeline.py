"""
End-to-end ingestion pipeline.

Given an Output Files folder:
  1. Parse the four ISO movement txt files (I, Y, T, IR90) — static filenames.
  2. Parse CMJ/PPU trial files (CMJ1.txt, CMJ2.txt, PPU1.txt, …) — Athletic Screen format.
     For each trial:
       a. Parse 5-column summary data.
       b. Load matching *_Power.txt and run full power-curve analysis.
       c. Upsert row into f_readiness_screen_{cmj|ppu} with inline power metrics.
       d. Persist curve metrics to f_readiness_screen_power_curve (scoring reads from there).
  3. Resolve athlete UUIDs — match-only, never create.
  4. Update has_readiness_screen_data flag in d_athletes.
  5. Compute composite readiness score.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

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
from .power_analysis import analyze_power_curve_advanced, load_power_txt
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
) -> Dict:
    """Run the full pipeline against `output_dir`."""
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
                src_id = extract_source_athlete_id(name)

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
                power_file = os.path.join(output_dir, f"{trial_name}_Power.txt")
                if os.path.isfile(power_file):
                    try:
                        pw_arr = load_power_txt(power_file)
                        pa = analyze_power_curve_advanced(pw_arr, fs_hz=fs_hz)
                        power_metrics = {k: _safe(pa.get(k)) for k in POWER_CURVE_COLS}
                        _emit(log, "power", f"  {trial_name}: power curve analysed ({len(pw_arr)} samples)")
                    except Exception as pe:
                        _emit(log, "power", f"  {trial_name}: power analysis failed ({pe})")
                else:
                    _emit(log, "power", f"  {trial_name}: no Power.txt found — skipping curve")

                table  = CMJ_PPU_TABLE[movement]
                src_id = extract_source_athlete_id(name)

                insert_data = {
                    "athlete_uuid":      athlete_uuid,
                    "session_date":      date_str,
                    "source_system":     "readiness_screen",
                    "source_athlete_id": src_id,
                    "trial_name":        trial_name,
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
                }
                update_cols = [
                    "jump_height", "peak_power", "peak_force",
                    "pp_w_per_kg", "pp_forceplate", "force_at_pp", "vel_at_pp",
                    "age_at_collection", "age_group",
                    *POWER_CURVE_COLS,
                ]
                # Only update columns that exist in insert_data.
                update_cols = [c for c in update_cols if c in insert_data]

                verb = _upsert(
                    conn, table, insert_data, update_cols,
                    "athlete_uuid = %s AND session_date = %s AND trial_name = %s",
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
                    m_full = {**power_metrics}
                    # analyze_power_curve_advanced returns more keys than POWER_CURVE_COLS;
                    # _persist_power_curve needs source_file and fs_hz as well.
                    try:
                        pw_arr_full = load_power_txt(power_file)
                        pa_full = analyze_power_curve_advanced(pw_arr_full, fs_hz=fs_hz)
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
                    decay_90_10_s, skewness, kurtosis, spectral_centroid_hz
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                ON CONFLICT (athlete_uuid, session_date, movement_type, trial_id) DO UPDATE SET
                    source_file          = EXCLUDED.source_file,
                    fs_hz                = EXCLUDED.fs_hz,
                    n_samples            = EXCLUDED.n_samples,
                    peak_power_w         = EXCLUDED.peak_power_w,
                    time_to_peak_s       = EXCLUDED.time_to_peak_s,
                    rise_time_10_90_s    = EXCLUDED.rise_time_10_90_s,
                    rise_slope_w_per_s   = EXCLUDED.rise_slope_w_per_s,
                    fwhm_s               = EXCLUDED.fwhm_s,
                    auc_j                = EXCLUDED.auc_j,
                    t_com_s              = EXCLUDED.t_com_s,
                    t_com_norm_0to1      = EXCLUDED.t_com_norm_0to1,
                    cv_local_peak        = EXCLUDED.cv_local_peak,
                    rpd_max_w_per_s      = EXCLUDED.rpd_max_w_per_s,
                    time_to_rpd_max_s    = EXCLUDED.time_to_rpd_max_s,
                    auc_pre_j            = EXCLUDED.auc_pre_j,
                    auc_post_j           = EXCLUDED.auc_post_j,
                    work_early_pct       = EXCLUDED.work_early_pct,
                    decay_90_10_s        = EXCLUDED.decay_90_10_s,
                    skewness             = EXCLUDED.skewness,
                    kurtosis             = EXCLUDED.kurtosis,
                    spectral_centroid_hz = EXCLUDED.spectral_centroid_hz
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
