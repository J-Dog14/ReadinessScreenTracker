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
MIN_HISTORY = 2                 # need at least this many prior points to z-score (Tier 3)
SCORE_SD_TO_POINTS = 20.0       # ±1 SD ≈ ±20 points (increased from 15 — averaging ~35 metrics
                                #  naturally compresses composite_z toward 0, so a larger
                                #  multiplier is needed to produce meaningful score spread)
Z_CLAMP = 3.0                   # cap individual metric z-scores to ±3 SD
A_TO_B_Z_SCALE = 2.5            # A_TO_B composite_z is systematically compressed because it
                                #  divides session deltas by cohort SD (larger than personal SD).
                                #  Empirically ~3x tighter than READINESS tier. Scale it back up.
BAND_READY = 60
BAND_FATIGUED = 40

# (table, column, sign) — sign +1 means higher_is_better, -1 means lower_is_better.
# Sign is applied AFTER computing z; +1 → z stays, -1 → z is negated.
CMJ_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_cmj", "jump_height",          +1),
    ("f_readiness_screen_cmj", "pp_w_per_kg",          +1),
    ("f_readiness_screen_cmj", "force_at_pp",          +1),
    ("f_readiness_screen_cmj", "vel_at_pp",            +1),
    # v2 phase metrics
    ("f_readiness_screen_cmj", "mrsi",                 +1),
    ("f_readiness_screen_cmj", "contraction_time_s",   -1),  # longer = fatigued
    ("f_readiness_screen_cmj", "ecc_con_duration_ratio", -1),  # rises with fatigue
    # eccentric_mean_power_w is negative; more negative (faster braking) = better → sign -1
    ("f_readiness_screen_cmj", "eccentric_mean_power_w", -1),
    # v2.1 force-derived metrics
    ("f_readiness_screen_cmj", "peak_grf_bw_ratio",    +1),  # normalized peak force
    ("f_readiness_screen_cmj", "rfd_0_100ms",          +1),  # rate of force development
    ("f_readiness_screen_cmj", "concentric_impulse_ns", +1), # mechanical output
]
PPU_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_ppu", "jump_height",          +1),
    ("f_readiness_screen_ppu", "pp_w_per_kg",          +1),
    ("f_readiness_screen_ppu", "force_at_pp",          +1),
    ("f_readiness_screen_ppu", "vel_at_pp",            +1),
    # v2 phase metrics — eccentric metrics omitted (still-start protocol, always NULL)
    ("f_readiness_screen_ppu", "mrsi",                 +1),
    ("f_readiness_screen_ppu", "contraction_time_s",   -1),
    # v2.1 force-derived metrics
    ("f_readiness_screen_ppu", "peak_grf_bw_ratio",      +1),
    ("f_readiness_screen_ppu", "rfd_0_100ms",            +1),
    ("f_readiness_screen_ppu", "concentric_impulse_ns",  +1),
    # eccentric phase metrics — PPU loads before exploding (plyometric push-up)
    ("f_readiness_screen_ppu", "ecc_con_duration_ratio", -1),
    ("f_readiness_screen_ppu", "eccentric_mean_power_w", -1),
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
# I and T removed in v2 (lower throwing-specific signal). Historical rows in
# f_readiness_screen_i and f_readiness_screen_t remain queryable but are not scored.
ISO_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_y",    "max_force",    +1),
    ("f_readiness_screen_ir90", "max_force",    +1),
    ("f_readiness_screen_y",    "time_to_max",  -1),
    ("f_readiness_screen_ir90", "time_to_max",  -1),
]

GRIP_METRICS: List[Tuple[str, str, int]] = [
    ("f_readiness_screen_grip", "left_kg",       +1),
    ("f_readiness_screen_grip", "right_kg",      +1),
    ("f_readiness_screen_grip", "max_kg",        +1),
    ("f_readiness_screen_grip", "asymmetry_pct", -1),  # higher asymmetry = worse
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

    baseline = [r[-1] for r in cur.fetchall() if r and r[-1] is not None]
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


def _count_prior_sessions(
    cur,
    athlete_uuid: str,
    session_date: date,
    baseline_days: int,
) -> int:
    """Count distinct session dates within the baseline window that precede session_date."""
    cutoff = session_date - timedelta(days=baseline_days)
    cur.execute(
        """
        SELECT COUNT(DISTINCT session_date) FROM (
            SELECT session_date FROM public.f_readiness_screen_cmj
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_readiness_screen_ppu
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_readiness_screen_y
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_readiness_screen_ir90
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_readiness_screen_grip
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_readiness_screen_power_curve
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_athletic_screen_cmj
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
            UNION
            SELECT session_date FROM public.f_athletic_screen_ppu
             WHERE athlete_uuid = %s AND session_date < %s AND session_date >= %s
        ) _all_sessions
        """,
        (
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
            athlete_uuid, session_date, cutoff,
        ),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _fetch_cohort_stats(
    cur,
    table: str,
    col: str,
    exclude_athlete_uuid: str,
) -> Optional[Tuple[float, float, int]]:
    """Return (cohort_mean, cohort_sd, n) across all athletes except the target, or None.

    Averages trials within each session first (matching _fetch_today_and_baseline), then
    computes population statistics across per-session averages.
    Returns None if fewer than 2 data points or sd == 0.
    """
    companion = _ATHLETIC_COMPANION.get((table, col))
    is_power_col = (table == "f_readiness_screen_power_curve" and col in _ATHLETIC_POWER_COLS)

    if companion:
        ath_table, ath_col = companion
        cur.execute(
            f"""
            SELECT AVG(session_avg)::float, STDDEV_SAMP(session_avg)::float, COUNT(*)
              FROM (
                SELECT athlete_uuid, session_date, AVG(v)::float AS session_avg
                  FROM (
                    SELECT athlete_uuid, session_date, {col} AS v
                      FROM public.{table}
                     WHERE athlete_uuid <> %s AND {col} IS NOT NULL
                    UNION ALL
                    SELECT athlete_uuid, session_date, {ath_col} AS v
                      FROM public.{ath_table}
                     WHERE athlete_uuid <> %s AND {ath_col} IS NOT NULL
                  ) _combined
                 GROUP BY athlete_uuid, session_date
              ) _sessions
            """,
            (exclude_athlete_uuid, exclude_athlete_uuid),
        )
    elif is_power_col:
        cur.execute(
            f"""
            SELECT AVG(session_avg)::float, STDDEV_SAMP(session_avg)::float, COUNT(*)
              FROM (
                SELECT athlete_uuid, session_date, AVG(v)::float AS session_avg
                  FROM (
                    SELECT athlete_uuid, session_date, {col} AS v
                      FROM public.f_readiness_screen_power_curve
                     WHERE athlete_uuid <> %s AND {col} IS NOT NULL
                    UNION ALL
                    SELECT athlete_uuid, session_date, {col} AS v
                      FROM public.f_athletic_screen_cmj
                     WHERE athlete_uuid <> %s AND {col} IS NOT NULL
                    UNION ALL
                    SELECT athlete_uuid, session_date, {col} AS v
                      FROM public.f_athletic_screen_ppu
                     WHERE athlete_uuid <> %s AND {col} IS NOT NULL
                  ) _combined
                 GROUP BY athlete_uuid, session_date
              ) _sessions
            """,
            (exclude_athlete_uuid, exclude_athlete_uuid, exclude_athlete_uuid),
        )
    else:
        cur.execute(
            f"""
            SELECT AVG(session_avg)::float, STDDEV_SAMP(session_avg)::float, COUNT(*)
              FROM (
                SELECT athlete_uuid, session_date, AVG({col})::float AS session_avg
                  FROM public.{table}
                 WHERE athlete_uuid <> %s AND {col} IS NOT NULL
                 GROUP BY athlete_uuid, session_date
              ) _sessions
            """,
            (exclude_athlete_uuid,),
        )

    row = cur.fetchone()
    if not row or row[0] is None or row[2] is None or int(row[2]) < 2:
        return None
    mean_val, sd_val, n = float(row[0]), row[1], int(row[2])
    if sd_val is None or float(sd_val) == 0:
        return None
    return mean_val, float(sd_val), n


_SCORE_GROUPS = {
    "cmj":         CMJ_METRICS,
    "ppu":         PPU_METRICS,
    "iso":         ISO_METRICS,
    "power_curve": POWER_CURVE_METRICS,
    "grip":        GRIP_METRICS,
}

# Hitter variant — omits ISO (Y / IR90 not collected for position players).
_HITTER_SCORE_GROUPS = {
    "cmj":         CMJ_METRICS,
    "ppu":         PPU_METRICS,
    "power_curve": POWER_CURVE_METRICS,
    "grip":        GRIP_METRICS,
}


def _build_null_result(per_metric: Dict, baseline_days: int, scoring_tier: str, note: str) -> Dict:
    return {
        "composite_score":    None,
        "composite_z":        None,
        "band":               "INSUFFICIENT_HISTORY",
        "cmj_z":              None,
        "ppu_z":              None,
        "iso_z":              None,
        "power_curve_z":      None,
        "grip_z":             None,
        "metrics_used":       0,
        "baseline_window_days": baseline_days,
        "scoring_tier":       scoring_tier,
        "flags_json":         json.dumps({"per_metric": per_metric, "note": note}),
    }


def _score_first_run(
    cur,
    athlete_uuid: str,
    session_date: date,
    baseline_days: int,
    score_groups: Dict = None,
) -> Dict:
    """Tier 1 (0 prior sessions): z-score vs cohort population mean/SD."""
    if score_groups is None:
        score_groups = _SCORE_GROUPS
    per_metric: Dict[str, dict] = {}
    group_zs: Dict[str, List[float]] = {k: [] for k in score_groups}
    all_zs: List[float] = []

    for group_name, metric_list in score_groups.items():
        for table, col, sign in metric_list:
            today, _ = _fetch_today_and_baseline(
                cur, athlete_uuid, table, col, session_date, baseline_days
            )
            label = _label(table, col)
            if today is None:
                continue
            cohort = _fetch_cohort_stats(cur, table, col, athlete_uuid)
            if cohort is None:
                per_metric[label] = {
                    "today":        round(today, 4),
                    "flag":         "insufficient_history",
                    "n_history":    0,
                    "sign":         sign,
                    "cohort_basis": True,
                }
                continue
            cohort_mean, cohort_sd, n = cohort
            z_signed = sign * (today - cohort_mean) / cohort_sd
            z_signed = max(-Z_CLAMP, min(Z_CLAMP, z_signed))
            per_metric[label] = {
                "today":        round(today, 4),
                "mean":         round(cohort_mean, 4),
                "sd":           round(cohort_sd, 4),
                "z":            round(z_signed, 3),
                "flag":         _flag(z_signed),
                "n_history":    n,
                "sign":         sign,
                "cohort_basis": True,
            }
            group_zs[group_name].append(z_signed)
            all_zs.append(z_signed)

    if not all_zs:
        return _build_null_result(per_metric, baseline_days, "FIRST_RUN",
                                  "No cohort data available for peer comparison.")

    def _g_avg(arr):
        return round(sum(arr) / len(arr), 3) if arr else None

    composite_z = sum(all_zs) / len(all_zs)
    score = max(0.0, min(100.0, 50.0 + SCORE_SD_TO_POINTS * composite_z))
    band = "READY" if score >= BAND_READY else ("FATIGUED" if score < BAND_FATIGUED else "CAUTION")
    return {
        "composite_score":    round(score, 1),
        "composite_z":        round(composite_z, 3),
        "band":               band,
        "cmj_z":              _g_avg(group_zs.get("cmj", [])),
        "ppu_z":              _g_avg(group_zs.get("ppu", [])),
        "iso_z":              _g_avg(group_zs.get("iso", [])),
        "power_curve_z":      _g_avg(group_zs.get("power_curve", [])),
        "grip_z":             _g_avg(group_zs.get("grip", [])),
        "metrics_used":       len(all_zs),
        "baseline_window_days": baseline_days,
        "scoring_tier":       "FIRST_RUN",
        "flags_json":         json.dumps({"per_metric": per_metric}),
    }


def _score_a_to_b(
    cur,
    athlete_uuid: str,
    session_date: date,
    baseline_days: int,
    score_groups: Dict = None,
) -> Dict:
    """Tier 2 (1 prior session): delta z-score (today − prior) scaled by cohort SD."""
    if score_groups is None:
        score_groups = _SCORE_GROUPS
    per_metric: Dict[str, dict] = {}
    group_zs: Dict[str, List[float]] = {k: [] for k in score_groups}
    all_zs: List[float] = []

    for group_name, metric_list in score_groups.items():
        for table, col, sign in metric_list:
            today, baseline = _fetch_today_and_baseline(
                cur, athlete_uuid, table, col, session_date, baseline_days
            )
            label = _label(table, col)
            if today is None or not baseline:
                continue
            prior_value = baseline[0]  # exactly 1 prior session
            cohort = _fetch_cohort_stats(cur, table, col, athlete_uuid)
            if cohort is None:
                per_metric[label] = {
                    "today":         round(today, 4),
                    "mean":          round(prior_value, 4),
                    "flag":          "insufficient_history",
                    "n_history":     1,
                    "sign":          sign,
                    "a_to_b_basis":  True,
                }
                continue
            _, cohort_sd, _ = cohort
            z_signed = sign * (today - prior_value) / cohort_sd
            z_signed = max(-Z_CLAMP, min(Z_CLAMP, z_signed))
            per_metric[label] = {
                "today":         round(today, 4),
                "mean":          round(prior_value, 4),
                "sd":            round(cohort_sd, 4),
                "z":             round(z_signed, 3),
                "flag":          _flag(z_signed),
                "n_history":     1,
                "sign":          sign,
                "a_to_b_basis":  True,
            }
            group_zs[group_name].append(z_signed)
            all_zs.append(z_signed)

    if not all_zs:
        return _build_null_result(per_metric, baseline_days, "A_TO_B",
                                  "No comparable data from prior session.")

    def _g_avg(arr):
        return round(sum(arr) / len(arr), 3) if arr else None

    composite_z = sum(all_zs) / len(all_zs)
    # A_TO_B normalizes by cohort SD, which is larger than personal SD, so the
    # composite_z runs ~3x tighter than READINESS tier. Scale it up before scoring.
    composite_z_scaled = composite_z * A_TO_B_Z_SCALE
    score = max(0.0, min(100.0, 50.0 + SCORE_SD_TO_POINTS * composite_z_scaled))
    band = "READY" if score >= BAND_READY else ("FATIGUED" if score < BAND_FATIGUED else "CAUTION")
    return {
        "composite_score":    round(score, 1),
        "composite_z":        round(composite_z, 3),  # stored unscaled for transparency
        "band":               band,
        "cmj_z":              _g_avg(group_zs.get("cmj", [])),
        "ppu_z":              _g_avg(group_zs.get("ppu", [])),
        "iso_z":              _g_avg(group_zs.get("iso", [])),
        "power_curve_z":      _g_avg(group_zs.get("power_curve", [])),
        "grip_z":             _g_avg(group_zs.get("grip", [])),
        "metrics_used":       len(all_zs),
        "baseline_window_days": baseline_days,
        "scoring_tier":       "A_TO_B",
        "flags_json":         json.dumps({"per_metric": per_metric}),
    }


def _score_readiness(
    cur,
    athlete_uuid: str,
    session_date: date,
    baseline_days: int,
    score_groups: Dict = None,
) -> Dict:
    """Tier 3 (≥2 prior sessions): personal z-score against rolling baseline."""
    if score_groups is None:
        score_groups = _SCORE_GROUPS
    per_metric: Dict[str, dict] = {}
    group_zs: Dict[str, List[float]] = {k: [] for k in score_groups}
    all_zs: List[float] = []

    for group_name, metric_list in score_groups.items():
        for table, col, sign in metric_list:
            today, baseline = _fetch_today_and_baseline(
                cur, athlete_uuid, table, col, session_date, baseline_days
            )
            if today is None:
                continue
            label = _label(table, col)
            if len(baseline) < MIN_HISTORY:
                per_metric[label] = {
                    "today":     round(today, 4),
                    "flag":      "insufficient_history",
                    "n_history": len(baseline),
                    "sign":      sign,
                }
                continue
            z_result = _zscore(today, baseline)
            if z_result is None:
                continue
            z, mean, sd = z_result
            z_signed = sign * z
            z_signed = max(-Z_CLAMP, min(Z_CLAMP, z_signed))
            per_metric[label] = {
                "today":     round(today, 4),
                "mean":      round(mean, 4),
                "sd":        round(sd, 4),
                "z":         round(z_signed, 3),
                "flag":      _flag(z_signed),
                "n_history": len(baseline),
                "sign":      sign,
            }
            group_zs[group_name].append(z_signed)
            all_zs.append(z_signed)

    if not all_zs:
        return _build_null_result(per_metric, baseline_days, "READINESS",
                                  "Need ≥2 historical sessions in at least one metric.")

    def _g_avg(arr):
        return round(sum(arr) / len(arr), 3) if arr else None

    composite_z = sum(all_zs) / len(all_zs)
    score = max(0.0, min(100.0, 50.0 + SCORE_SD_TO_POINTS * composite_z))
    band = "READY" if score >= BAND_READY else ("FATIGUED" if score < BAND_FATIGUED else "CAUTION")
    return {
        "composite_score":    round(score, 1),
        "composite_z":        round(composite_z, 3),
        "band":               band,
        "cmj_z":              _g_avg(group_zs.get("cmj", [])),
        "ppu_z":              _g_avg(group_zs.get("ppu", [])),
        "iso_z":              _g_avg(group_zs.get("iso", [])),
        "power_curve_z":      _g_avg(group_zs.get("power_curve", [])),
        "grip_z":             _g_avg(group_zs.get("grip", [])),
        "metrics_used":       len(all_zs),
        "baseline_window_days": baseline_days,
        "scoring_tier":       "READINESS",
        "flags_json":         json.dumps({"per_metric": per_metric}),
    }


def compute_intra_session_cv(
    athlete_uuid: str,
    session_date: date,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
) -> Dict:
    """Compute intra-session coefficient of variation across the 2 CMJ/PPU trials.

    For each of: cmj.jump_height, cmj.mrsi, cmj.peak_power_w,
                 ppu.jump_height, ppu.mrsi, ppu.peak_power_w:
      1. Load the individual trial values for today.
      2. Compute today_cv = sd([trial1, trial2]) / mean([trial1, trial2]).
      3. Compare to rolling baseline mean CV (same window).
      4. Flag "elevated" when today_cv > 2× baseline_mean_cv.

    Returns {label: {today_cv, baseline_mean_cv, flag, n_trials}}.
    """
    cv_metrics = [
        ("f_readiness_screen_cmj", "jump_height"),
        ("f_readiness_screen_cmj", "mrsi"),
        ("f_readiness_screen_cmj", "peak_power_w"),
        ("f_readiness_screen_ppu", "jump_height"),
        ("f_readiness_screen_ppu", "mrsi"),
        ("f_readiness_screen_ppu", "peak_power_w"),
    ]
    cutoff = session_date - timedelta(days=baseline_days)
    result: Dict = {}

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for table, col in cv_metrics:
                label = _label(table, col)
                try:
                    cur.execute(
                        f"SELECT {col} FROM public.{table}"
                        f" WHERE athlete_uuid = %s AND session_date = %s"
                        f"   AND {col} IS NOT NULL",
                        (athlete_uuid, session_date),
                    )
                    today_vals = [r[0] for r in cur.fetchall() if r[0] is not None]
                    if len(today_vals) < 2:
                        continue

                    n = len(today_vals)
                    mean_v = sum(today_vals) / n
                    if mean_v == 0:
                        continue
                    sd_v = math.sqrt(sum((x - mean_v) ** 2 for x in today_vals) / (n - 1))
                    today_cv = sd_v / abs(mean_v)

                    # Baseline: compute per-session CVs over rolling window.
                    cur.execute(
                        f"""
                        SELECT session_date,
                               STDDEV({col}) / NULLIF(ABS(AVG({col})), 0) AS cv
                          FROM public.{table}
                         WHERE athlete_uuid = %s
                           AND session_date < %s
                           AND session_date >= %s
                           AND {col} IS NOT NULL
                         GROUP BY session_date
                        HAVING COUNT(*) >= 2
                        """,
                        (athlete_uuid, session_date, cutoff),
                    )
                    baseline_cvs = [r[1] for r in cur.fetchall() if r[1] is not None]
                    baseline_mean_cv = (sum(baseline_cvs) / len(baseline_cvs)) if baseline_cvs else None

                    if baseline_mean_cv is not None and baseline_mean_cv > 0:
                        flag = "elevated" if today_cv > 2.0 * baseline_mean_cv else "stable"
                    else:
                        flag = "stable"

                    result[label] = {
                        "today_cv":        round(today_cv, 4),
                        "baseline_mean_cv": round(baseline_mean_cv, 4) if baseline_mean_cv is not None else None,
                        "flag":            flag,
                        "n_trials":        n,
                    }
                except Exception:
                    pass
    finally:
        conn.close()

    return result


def compute_score_for_session(
    athlete_uuid: str,
    session_date: date,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
    is_hitter: bool = False,
) -> Dict:
    """
    Compute the composite readiness score for one (athlete, session_date) and
    return a dict with the persistable columns + the per-metric flags JSON.

    Routes through three tiers based on prior session count:
      Tier 1 (0 prior): Peer-comparison z-score vs cohort.
      Tier 2 (1 prior): A-to-B delta z-score scaled by cohort SD.
      Tier 3 (2+ prior): Personal rolling z-score (current methodology).

    When is_hitter=True, ISO metrics (Y/IR90) are excluded from scoring.

    Caller is responsible for upserting into f_readiness_screen_score.
    """
    score_groups = _HITTER_SCORE_GROUPS if is_hitter else _SCORE_GROUPS
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            n_prior = _count_prior_sessions(cur, athlete_uuid, session_date, baseline_days)
            if n_prior == 0:
                result = _score_first_run(cur, athlete_uuid, session_date, baseline_days, score_groups)
            elif n_prior == 1:
                result = _score_a_to_b(cur, athlete_uuid, session_date, baseline_days, score_groups)
            else:
                result = _score_readiness(cur, athlete_uuid, session_date, baseline_days, score_groups)
    finally:
        conn.close()

    intra_session = compute_intra_session_cv(athlete_uuid, session_date, baseline_days)
    flags = json.loads(result["flags_json"])
    flags["intra_session"] = intra_session
    result["flags_json"] = json.dumps(flags)
    return result


def upsert_score(athlete_uuid: str, session_date: date, score_dict: Dict) -> None:
    """Persist a score row. Re-running the same date overwrites."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.f_readiness_screen_score
                    (athlete_uuid, session_date, composite_score, composite_z, band,
                     cmj_z, ppu_z, iso_z, power_curve_z, grip_z,
                     metrics_used, baseline_window_days, flags_json, scoring_tier)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (athlete_uuid, session_date) DO UPDATE SET
                    composite_score      = EXCLUDED.composite_score,
                    composite_z          = EXCLUDED.composite_z,
                    band                 = EXCLUDED.band,
                    cmj_z                = EXCLUDED.cmj_z,
                    ppu_z                = EXCLUDED.ppu_z,
                    iso_z                = EXCLUDED.iso_z,
                    power_curve_z        = EXCLUDED.power_curve_z,
                    grip_z               = EXCLUDED.grip_z,
                    metrics_used         = EXCLUDED.metrics_used,
                    baseline_window_days = EXCLUDED.baseline_window_days,
                    flags_json           = EXCLUDED.flags_json,
                    scoring_tier         = EXCLUDED.scoring_tier
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
                    score_dict.get("grip_z"),
                    score_dict["metrics_used"],
                    score_dict["baseline_window_days"],
                    score_dict["flags_json"],
                    score_dict.get("scoring_tier"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def score_session(athlete_uuid: str, session_date: date, is_hitter: bool = False) -> Dict:
    """Compute + persist in one call. Returns the score dict (suitable for the dashboard)."""
    result = compute_score_for_session(athlete_uuid, session_date, is_hitter=is_hitter)
    upsert_score(athlete_uuid, session_date, result)
    return result
