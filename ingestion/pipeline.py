"""
End-to-end ingestion pipeline.

Given an Output Files folder:
  1. Parse the six readiness movement txt files (CMJ, PPU, I, Y, T, IR90).
  2. Resolve / create the athlete UUID against analytics.d_athletes.
  3. Upsert rows into the existing f_readiness_screen_<movement> fact tables.
     (Same UPSERT pattern the backend uses — check exists, then UPDATE or INSERT.)
  4. For CMJ + PPU, walk the matching *_Power.txt files and persist curve
     metrics into f_readiness_screen_power_curve.
  5. Compute the composite readiness score and persist to f_readiness_screen_score.

Each stage prints to stdout with a uniform `[stage] message` prefix so the
maintenance page's SSE stream can render a tidy log.

Designed to be safe to re-run: existing rows are updated, not duplicated.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Callable, Dict, List, Optional

from db.connection import get_connection
from .age_utils import (
    calculate_age_at_collection,
    calculate_age_group,
    normalize_session_date,
    parse_date,
)
from .athlete_manager import (
    extract_source_athlete_id,
    get_athlete_dob,
    get_or_create_athlete,
    update_athlete_age_group,
    update_athlete_data_flag,
)
from .file_parsers import (
    ASCII_FILES,
    discover_txt_files,
    find_session_xml,
    normalize_gender,
    parse_session_xml,
    parse_txt_file,
)
from .power_analysis import analyze_session_power_files
from .scoring import score_session


# Existing fact tables; mapped from movement_type. We do not modify their
# schema — we only INSERT/UPDATE rows with the same columns the backend uses.
MOVEMENT_TO_TABLE = {
    "I":    "f_readiness_screen_i",
    "Y":    "f_readiness_screen_y",
    "T":    "f_readiness_screen_t",
    "IR90": "f_readiness_screen_ir90",
    "CMJ":  "f_readiness_screen_cmj",
    "PPU":  "f_readiness_screen_ppu",
}


def _emit(log: Callable[[str], None], stage: str, msg: str) -> None:
    log(f"[{stage}] {msg}")


def run_ingestion(
    output_dir: str,
    power_dir: Optional[str] = None,
    fs_hz: float = 1000.0,
    log: Callable[[str], None] = print,
    athlete_uuid_override: Optional[str] = None,
) -> Dict:
    """
    Run the full pipeline against `output_dir`. If `athlete_uuid_override` is
    provided (Existing Athlete flow), all data is attributed to that UUID — no
    new athletes are created.

    Returns a summary dict the maintenance page can render.
    """
    summary: Dict = {
        "output_dir":      output_dir,
        "files_found":     {},
        "rows_inserted":   0,
        "rows_updated":    0,
        "athletes":        [],
        "power_curve_rows": 0,
        "scores":          [],
        "errors":          [],
    }

    if power_dir is None:
        power_dir = output_dir

    if not os.path.isdir(output_dir):
        msg = f"Folder not found: {output_dir}"
        _emit(log, "ERROR", msg)
        summary["errors"].append(msg)
        return summary

    _emit(log, "scan", f"Scanning {output_dir}")
    txt_files = discover_txt_files(output_dir)
    summary["files_found"] = {m: os.path.basename(p) for m, p in txt_files.items()}
    if not txt_files:
        _emit(log, "scan", "No movement files found. Nothing to do.")
        return summary

    for movement, path in txt_files.items():
        _emit(log, "scan", f"  found {movement} -> {os.path.basename(path)}")

    # Session.xml — used to default gender and (eventually) verify name.
    session_gender = "Male"
    xml_path = find_session_xml(output_dir)
    if xml_path:
        try:
            xml_data = parse_session_xml(xml_path)
            session_gender = normalize_gender(xml_data.get("gender"))
            _emit(log, "session", f"Session.xml: name={xml_data.get('name')}, gender={session_gender}")
        except Exception as e:
            _emit(log, "session", f"Could not parse Session.xml ({e}); defaulting gender=Male")

    # Track unique (athlete, date) pairs across the run so we score each once.
    sessions_seen = set()
    athletes_meta: Dict[str, str] = {}  # uuid -> display name

    conn = get_connection()
    try:
        for movement, file_path in txt_files.items():
            try:
                parsed = parse_txt_file(file_path, movement)
                if not parsed:
                    _emit(log, "parse", f"  {movement}: failed to parse {os.path.basename(file_path)}")
                    summary["errors"].append(f"parse failed: {file_path}")
                    continue

                name = parsed["name"]
                date_str = parsed["date"]

                # Resolve athlete UUID.
                if athlete_uuid_override:
                    athlete_uuid = athlete_uuid_override
                else:
                    athlete_uuid, _ = get_or_create_athlete(
                        name=name,
                        source_system="readiness_screen",
                        source_athlete_id=extract_source_athlete_id(name),
                        gender=session_gender,
                    )
                update_athlete_data_flag(athlete_uuid)
                if athlete_uuid not in athletes_meta:
                    athletes_meta[athlete_uuid] = name
                    _emit(log, "athlete", f"  {name} -> {athlete_uuid} (matched)")

                # Age + age_group at session.
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

                table = MOVEMENT_TO_TABLE[movement]
                src_id = extract_source_athlete_id(name)

                if movement in ("CMJ", "PPU"):
                    insert_data = {
                        "athlete_uuid":      athlete_uuid,
                        "session_date":      date_str,
                        "source_system":     "readiness_screen",
                        "source_athlete_id": src_id,
                        "age_at_collection": age_at_collection,
                        "age_group":         age_group,
                        "jump_height":       parsed.get("JH_IN"),
                        "peak_power":        parsed.get("LEWIS_PEAK_POWER"),
                        "peak_force":        parsed.get("Max_Force"),
                        "pp_w_per_kg":       parsed.get("PP_W_per_kg"),
                        "pp_forceplate":     parsed.get("PP_FORCEPLATE"),
                        "force_at_pp":       parsed.get("Force_at_PP"),
                        "vel_at_pp":         parsed.get("Vel_at_PP"),
                    }
                    update_cols = [
                        "jump_height", "peak_power", "peak_force", "pp_w_per_kg",
                        "pp_forceplate", "force_at_pp", "vel_at_pp",
                        "age_at_collection", "age_group",
                    ]
                else:
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

                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT 1 FROM public.{table} WHERE athlete_uuid = %s AND session_date = %s LIMIT 1",
                        (athlete_uuid, date_str),
                    )
                    exists = cur.fetchone() is not None

                    if exists:
                        set_clause = ", ".join(f"{c} = %s" for c in update_cols)
                        params = [insert_data[c] for c in update_cols] + [athlete_uuid, date_str]
                        cur.execute(
                            f"UPDATE public.{table} SET {set_clause} "
                            f"WHERE athlete_uuid = %s AND session_date = %s",
                            params,
                        )
                        summary["rows_updated"] += 1
                        verb = "updated"
                    else:
                        cols = list(insert_data.keys())
                        placeholders = ", ".join(["%s"] * len(cols))
                        cur.execute(
                            f"INSERT INTO public.{table} ({', '.join(cols)}) VALUES ({placeholders})",
                            [insert_data[c] for c in cols],
                        )
                        summary["rows_inserted"] += 1
                        verb = "inserted"
                conn.commit()

                update_athlete_age_group(athlete_uuid, age_group)
                _emit(log, "upsert", f"  {table}: {verb} {date_str}")
                sessions_seen.add((athlete_uuid, date_str))

            except Exception as e:
                conn.rollback()
                msg = f"{movement} ({os.path.basename(file_path)}): {e}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)
    finally:
        conn.close()

    # ---- Power-curve analysis (CMJ, PPU) -----------------------------------
    if sessions_seen:
        _emit(log, "power", f"Looking for *_Power.txt files in {power_dir}")
        for movement in ("CMJ", "PPU"):
            try:
                results = analyze_session_power_files(power_dir, movement, fs_hz=fs_hz)
            except Exception as e:
                msg = f"power analysis {movement}: {e}"
                _emit(log, "ERROR", msg)
                summary["errors"].append(msg)
                continue

            if not results:
                _emit(log, "power", f"  {movement}: no power.txt files found")
                continue

            # Attribute to each (athlete_uuid, date) we just upserted for this movement.
            # In practice CMJ/PPU map 1:1 with a single (athlete, date) per run, but we
            # write a row per power file (trial).
            for (athlete_uuid, date_str) in sessions_seen:
                for trial_idx, m in enumerate(results, start=1):
                    if "error" in m:
                        _emit(log, "power", f"  {movement} skipped {os.path.basename(m['source_file'])}: {m['error']}")
                        continue
                    _persist_power_curve(athlete_uuid, date_str, movement, trial_idx, m)
                    summary["power_curve_rows"] += 1
                _emit(log, "power", f"  {movement}: persisted {len(results)} curve rows for {date_str}")

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
            band = score.get("band")
            comp = score.get("composite_score")
            _emit(log, "score", f"  {athletes_meta.get(athlete_uuid, athlete_uuid)} {date_str}: {band} ({comp})")
        except Exception as e:
            msg = f"score {athlete_uuid} {date_str}: {e}"
            _emit(log, "ERROR", msg)
            summary["errors"].append(msg)

    summary["athletes"] = [{"uuid": u, "name": n} for u, n in athletes_meta.items()]
    _emit(
        log,
        "done",
        f"inserted={summary['rows_inserted']} updated={summary['rows_updated']} "
        f"power_rows={summary['power_curve_rows']} scored={len(summary['scores'])}",
    )
    return summary


def _persist_power_curve(athlete_uuid: str, date_str: str, movement: str, trial_id: int, m: dict) -> None:
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
                    m.get("n_samples"), m.get("peak_power_w"), m.get("time_to_peak_s"),
                    m.get("rise_time_10_90_s"), m.get("rise_slope_w_per_s"),
                    m.get("fwhm_s"), m.get("auc_j"),
                    m.get("t_com_s"), m.get("t_com_norm_0to1"), m.get("cv_local_peak"),
                    m.get("rpd_max_w_per_s"), m.get("time_to_rpd_max_s"),
                    m.get("auc_pre_j"), m.get("auc_post_j"), m.get("work_early_pct"),
                    m.get("decay_90_10_s"), m.get("skewness"), m.get("kurtosis"),
                    m.get("spectral_centroid_hz"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# CLI entry — useful for testing without the Flask app.
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run readiness ingestion against an Output Files folder.")
    parser.add_argument("output_dir")
    parser.add_argument("--power-dir", default=None)
    parser.add_argument("--fs-hz", type=float, default=1000.0)
    parser.add_argument("--athlete-uuid", default=None, help="Existing Athlete flow override")
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
