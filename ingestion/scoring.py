"""
Composite readiness score computation.

Methodology — anchored in published athlete-monitoring literature:

  Buchheit M. (2014) "Monitoring training status with HR measures: do all
    roads lead to Rome?" Front. Physiol. — argues for INDIVIDUAL z-scoring
    of monitoring metrics against an athlete's own recent baseline rather
    than against a peer cohort. We follow this for the composite.

  Halson SL. (2014) "Monitoring training load to understand fatigue in
    athletes." Sports Med. 44 Suppl 2:S139-S147 — supports a battery
    rather than a single test, and the use of CMJ as the most robust
    field test of neuromuscular readiness.

  Hopkins WG. (2004) "How to interpret changes in an athletic performance
    test." Sportscience 8 — defines smallest worthwhile change (SWC):
    a meaningful change is one bigger than ~0.2 of the BETWEEN-subject SD
    or ~0.5 of the WITHIN-subject SD. We expose per-metric SWC flags as
    drilldown JSON next to the composite.

The pipeline:
  1. For each metric m where the athlete has at least MIN_HISTORY (=3) prior
     sessions in the rolling baseline window (28 days by default), compute
     z_m = (today_value - mean_baseline_m) / SD_baseline_m.
  2. Sign-correct: for metrics where higher = better (e.g. jump height, peak
     power, peak force, max isometric force, RPD, peak power), z stays as-is.
     For metrics where lower = better (e.g. time_to_max_force, rise_time —
     because lower means faster RFD), we flip sign so a "good day" yields
     positive z.
  3. composite_z = mean(z_m).
  4. score = clip(50 + 15 * composite_z, 0, 100).
  5. Bands: READY ≥ 60, CAUTION 40..60, FATIGUED < 40.

The score is INSUFFICIENT_HISTORY (None) until the athlete has ≥3 baseline
sessions in any one metric.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from db.connection import get_connection

log = logging.getLogger(__name__)

DEFAULT_BASELINE_DAYS = 28
MIN_HISTORY = 3                 # need at least this many prior points to z-score
SCORE_SD_TO_POINTS = 15.0       # ±1 SD ≈ ±15 points
BAND_READY = 60
BAND_FATIGUED = 40

# (table, column, sign) — sign +1 means higher_is_better, -1 means lower_is_better.
# Sign is applied AFTER computing z; +1 → z stays, -1 → z is negated.
CMJ_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_cmj", "jump_height",  +1),
    ("f_readiness_screen_cmj", "pp_w_per_kg",  +1),
    ("f_readiness_screen_cmj", "force_at_pp",  +1),
    ("f_readiness_screen_cmj", "vel_at_pp",    +1),
]
PPU_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_ppu", "jump_height",  +1),
    ("f_readiness_screen_ppu", "pp_w_per_kg",  +1),
    ("f_readiness_screen_ppu", "force_at_pp",  +1),
    ("f_readiness_screen_ppu", "vel_at_pp",    +1),
]

# Athletic screen supplemental sources (read-only).
# Maps (readiness_table, col) → (athletic_table, athletic_col) where the column
# exists under a different name or the same name in the athletic screen table.
_ATHLETIC_COMPANION: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("f_readiness_screen_cmj", "jump_height"): ("f_athletic_screen_cmj", "jh_in"),
    ("f_readiness_screen_cmj", "pp_w_per_kg"): ("f_athletic_screen_cmj", "pp_w_per_kg"),
    ("f_readiness_screen_cmj", "force_at_pp"): ("f_athletic_screen_cmj", "force_at_pp"),
    ("f_readiness_screen_cmj", "vel_at_pp"):   ("f_athletic_screen_cmj", "vel_at_pp"),
    ("f_readiness_screen_ppu", "jump_height"): ("f_athletic_screen_ppu", "jh_in"),
    ("f_readiness_screen_ppu", "pp_w_per_kg"): ("f_athletic_screen_ppu", "pp_w_per_kg"),
    ("f_readiness_screen_ppu", "force_at_pp"): ("f_athletic_screen_ppu", "force_at_pp"),
    ("f_readiness_screen_ppu", "vel_at_pp"):   ("f_athletic_screen_ppu", "vel_at_pp"),
}

# Power-curve columns stored inline in both athletic screen CMJ and PPU tables.
_ATHLETIC_POWER_COLS: frozenset = frozenset({
    "peak_power_w", "rpd_max_w_per_s", "rise_time_10_90_s", "fwhm_s",
    "auc_j", "decay_90_10_s", "t_com_norm_0to1",
    "skewness", "kurtosis", "spectral_centroid_hz",
})
ISO_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_i",    "max_force",    +1),
    ("f_readiness_screen_y",    "max_force",    +1),
    ("f_readiness_screen_t",    "max_force",    +1),
    ("f_readiness_screen_ir90", "max_force",    +1),
    ("f_readiness_screen_i",    "time_to_max",  -1),  # faster = better
    ("f_readiness_screen_y",    "time_to_max",  -1),
    ("f_readiness_screen_t",    "time_to_max",  -1),
    ("f_readiness_screen_ir90", "time_to_max",  -1),
]
POWER_CURVE_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_power_curve", "peak_power_w",       +1),
    ("f_readiness_screen_power_curve", "rpd_max_w_per_s",    +1),
    ("f_readiness_screen_power_curve", "rise_slope_w_per_s", +1),
    ("f_readiness_screen_power_curve", "auc_j",              +1),
    ("f_readiness_screen_power_curve", "rise_time_10_90_s",  -1),  # shorter = better
]


def _label(table: str, col: str) -> str:
    """Pretty key for the flags JSON, e.g. 'f_readiness_screen_cmj.jump_height' -> 'cmj.jump_height'."""
    short = table.replace("f_readiness_screen_", "").replace("_power_curve", "power")
    return f"{short}.{col}"


def _fetch_today_and_baseline(
    cur,
    athlete_uuid: str,
    table: str,
    col: str,
    session_date: date,
    baseline_days: int,
) -> Tuple[Optional[float], List[float]]:
    """Return (today_value, baseline_values). Today = exact session_date, baseline = strictly prior, within window.

    For CMJ/PPU metrics, UNIONs f_athletic_screen_cmj/ppu as supplemental sources so that
    athletic screen sessions contribute to the baseline even when no readiness screen CMJ/PPU
    data was collected that day.
    For power-curve metrics, also UNIONs the inline power columns from both athletic screen tables.
    """
    cutoff = session_date - timedelta(days=baseline_days)
    companion = _ATHLETIC_COMPANION.get((table, col))
    is_power_col = (table == "f_readiness_screen_power_curve" and col in _ATHLETIC_POWER_COLS)

    if companion:
        ath_table, ath_col = companion
        cur.execute(
            f"""
            SELECT AVG(v)::float FROM (
              SELECT {col} AS v FROM public.{table}
               WHERE athlete_uuid = %s AND session_date = %s
              UNION ALL
              SELECT {ath_col} AS v FROM public.{ath_table}
               WHERE athlete_uuid = %s AND session_date = %s
            ) _u
            """,
            (athlete_uuid, session_date, athlete_uuid, session_date),
        )
        today_row = cur.fetchone()
        today = today_row[0] if today_row and today_row[0] is not None else None

        cur.execute(
            f"""
            SELECT session_date, AVG(v)::float FROM (
              SELECT session_date, {col} AS v FROM public.{table}
               WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
              UNION ALL
              SELECT session_date, {ath_col} AS v FROM public.{ath_table}
               WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            ) _u
            GROUP BY session_date
            ORDER BY session_date
            """,
            (athlete_uuid, session_date, cutoff, athlete_uuid, session_date, cutoff),
        )
    elif is_power_col:
        cur.execute(
            f"""
            SELECT AVG(v)::float FROM (
              SELECT {col} AS v FROM public.f_readiness_screen_power_curve
               WHERE athlete_uuid = %s AND session_date = %s
              UNION ALL
              SELECT {col} AS v FROM public.f_athletic_screen_cmj
               WHERE athlete_uuid = %s AND session_date = %s
              UNION ALL
              SELECT {col} AS v FROM public.f_athletic_screen_ppu
               WHERE athlete_uuid = %s AND session_date = %s
            ) _u
            """,
            (athlete_uuid, session_date,
             athlete_uuid, session_date,
             athlete_uuid, session_date),
        )
        today_row = cur.fetchone()
        today = today_row[0] if today_row and today_row[0] is not None else None

        cur.execute(
            f"""
            SELECT session_date, AVG(v)::float FROM (
              SELECT session_date, {col} AS v FROM public.f_readiness_screen_power_curve
               WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
              UNION ALL
              SELECT session_date, {col} AS v FROM public.f_athletic_screen_cmj
               WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
              UNION ALL
              SELECT session_date, {col} AS v FROM public.f_athletic_screen_ppu
               WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            ) _u
            GROUP BY session_date
            ORDER BY session_date
            """,
            (athlete_uuid, session_date, cutoff,
             athlete_uuid, session_date, cutoff,
             athlete_uuid, session_date, cutoff),
        )
    else:
        # Single-table path for isometric and any other metrics.
        cur.execute(
            f"""
            SELECT AVG({col})::float
              FROM public.{table}
             WHERE athlete_uuid = %s AND session_date = %s
            """,
            (athlete_uuid, session_date),
        )
        today_row = cur.fetchone()
        today = today_row[0] if today_row and today_row[0] is not None else None

        cur.execute(
            f"""
            SELECT AVG({col})::float
              FROM public.{table}
             WHERE athlete_uuid = %s
               AND session_date <  %s
               AND session_date >= %s
             GROUP BY session_date
             ORDER BY session_date
            """,
            (athlete_uuid, session_date, cutoff),
        )

    baseline = [r[0] for r in cur.fetchall() if r and r[0] is not None]
    return today, baseline


def _zscore(value: float, baseline: List[float]) -> Optional[Tuple[float, float, float]]:
    """Compute (z, mean, sd). Returns None if not enough data or sd==0."""
    if value is None or len(baseline) < MIN_HISTORY:
        return None
    n = len(baseline)
    mean = sum(baseline) / n
    var = sum((x - mean) ** 2 for x in baseline) / (n - 1)  # sample SD
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (value - mean) / sd, mean, sd


def _flag(z: float) -> str:
    """SWC-style flag (Hopkins): >0.6 SD ≈ meaningful improvement; <-0.6 ≈ meaningful drop."""
    if z >= 0.6:
        return "rise"
    if z <= -0.6:
        return "drop"
    return "stable"


def compute_score_for_session(
    athlete_uuid: str,
    session_date: date,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
) -> Dict:
    """
    Compute the composite readiness score for one (athlete, session_date) and
    return a dict with the persistable columns + the per-metric flags JSON.
    Caller is responsible for upserting into f_readiness_screen_score.
    """
    groups = {
        "cmj": CMJ_METRICS,
        "ppu": PPU_METRICS,
        "iso": ISO_METRICS,
        "power_curve": POWER_CURVE_METRICS,
    }

    per_metric: Dict[str, dict] = {}
    group_zs: Dict[str, List[float]] = {k: [] for k in groups}
    all_zs: List[float] = []

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for group_name, metric_list in groups.items():
                for table, col, sign in metric_list:
                    today, baseline = _fetch_today_and_baseline(
                        cur, athlete_uuid, table, col, session_date, baseline_days
                    )
                    if today is None:
                        continue  # no data today, skip
                    z_result = _zscore(today, baseline)
                    if z_result is None:
                        continue
                    z, mean, sd = z_result
                    z_signed = sign * z
                    label = _label(table, col)
                    per_metric[label] = {
                        "today":   round(today, 4),
                        "mean":    round(mean, 4),
                        "sd":      round(sd, 4),
                        "z":       round(z_signed, 3),
                        "flag":    _flag(z_signed),
                        "n_history": len(baseline),
                        "sign":    sign,
                    }
                    group_zs[group_name].append(z_signed)
                    all_zs.append(z_signed)
    finally:
        conn.close()

    if not all_zs:
        return {
            "composite_score": None,
            "composite_z": None,
            "band": "INSUFFICIENT_HISTORY",
            "cmj_z": None,
            "ppu_z": None,
            "iso_z": None,
            "power_curve_z": None,
            "metrics_used": 0,
            "baseline_window_days": baseline_days,
            "flags_json": json.dumps({"per_metric": {}, "note": "Need ≥3 historical sessions in at least one metric."}),
        }

    composite_z = sum(all_zs) / len(all_zs)
    score = max(0.0, min(100.0, 50.0 + SCORE_SD_TO_POINTS * composite_z))
    band = "READY" if score >= BAND_READY else ("FATIGUED" if score < BAND_FATIGUED else "CAUTION")

    def _g_avg(arr):
        return round(sum(arr) / len(arr), 3) if arr else None

    return {
        "composite_score": round(score, 1),
        "composite_z": round(composite_z, 3),
        "band": band,
        "cmj_z": _g_avg(group_zs["cmj"]),
        "ppu_z": _g_avg(group_zs["ppu"]),
        "iso_z": _g_avg(group_zs["iso"]),
        "power_curve_z": _g_avg(group_zs["power_curve"]),
        "metrics_used": len(all_zs),
        "baseline_window_days": baseline_days,
        "flags_json": json.dumps({"per_metric": per_metric}),
    }


def upsert_score(athlete_uuid: str, session_date: date, score_dict: Dict) -> None:
    """Persist a score row. Re-running the same date overwrites."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.f_readiness_screen_score
                    (athlete_uuid, session_date, composite_score, composite_z, band,
                     cmj_z, ppu_z, iso_z, power_curve_z,
                     metrics_used, baseline_window_days, flags_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (athlete_uuid, session_date) DO UPDATE SET
                    composite_score      = EXCLUDED.composite_score,
                    composite_z          = EXCLUDED.composite_z,
                    band                 = EXCLUDED.band,
                    cmj_z                = EXCLUDED.cmj_z,
                    ppu_z                = EXCLUDED.ppu_z,
                    iso_z                = EXCLUDED.iso_z,
                    power_curve_z        = EXCLUDED.power_curve_z,
                    metrics_used         = EXCLUDED.metrics_used,
                    baseline_window_days = EXCLUDED.baseline_window_days,
                    flags_json           = EXCLUDED.flags_json
                """,
                (
                    athlete_uuid,
                    session_date,
                    score_dict["composite_score"],
                    score_dict["composite_z"],
                    score_dict["band"],
                    score_dict["cmj_z"],
                    score_dict["ppu_z"],
                    score_dict["iso_z"],
                    score_dict["power_curve_z"],
                    score_dict["metrics_used"],
                    score_dict["baseline_window_days"],
                    score_dict["flags_json"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def score_session(athlete_uuid: str, session_date: date) -> Dict:
    """Compute + persist in one call. Returns the score dict (suitable for the dashboard)."""
    result = compute_score_for_session(athlete_uuid, session_date)
    upsert_score(athlete_uuid, session_date, result)
    return result
