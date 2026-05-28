"""
Dashboard routes:

  GET /dashboard                            — Render the page (athlete dropdown + chart shells).
  GET /api/dashboard/athletes               — List athletes with any readiness data.
  GET /api/dashboard/athlete/<uuid>         — All time-series + scores + peer scatter data
                                              for one athlete. Drives every chart on the page.

Layout (v2):
  Row 0: Composite readiness score gauge + per-group sub-scores (CMJ/PPU/ISO/Power/Grip)
  Row 1: ISO Y & IR90 time series (I/T available via "Show historical" toggle)
  Row 1.5: Grip strength panel (timeseries + L/R asymmetry)
  Row 2: CMJ — jump height timeseries + F-v scatter
  Row 2.5: Movement Strategy — CMJ: mRSI, contraction time, ecc:con ratio, eccentric mean power
                             — PPU: mRSI, contraction time only (still-start protocol)
  Row 3: PPU — same as CMJ
  Plus: Today vs Baseline horizontal bar, Trial Consistency panel, Metric Flag Heatmap
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request
from psycopg2.extras import RealDictCursor

from db.connection import get_connection
from ingestion.athlete_manager import list_athletes_with_readiness

bp = Blueprint("dashboard", __name__)

FLAG_HEATMAP_DAYS = 14


@bp.route("/dashboard")
def page():
    athletes = list_athletes_with_readiness()
    pre_select = request.args.get("athlete") or (athletes[0]["athlete_uuid"] if athletes else None)
    return render_template(
        "dashboard.html",
        athletes=athletes,
        selected_uuid=pre_select,
    )


@bp.route("/api/dashboard/athletes")
def athletes_api():
    return jsonify({"athletes": list_athletes_with_readiness()})


@bp.route("/api/dashboard/athlete/<uuid>")
def athlete_data(uuid: str):
    """Return everything needed for the dashboard for this athlete in one payload."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT athlete_uuid, name, age_group, gender FROM analytics.d_athletes WHERE athlete_uuid = %s",
                (uuid,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "athlete not found"}), 404
            athlete = dict(row)

            iso_series = _iso_series(cur, uuid)
            cmj_ts, cmj_scatter, cmj_peers = _cmj_or_ppu(cur, uuid, "cmj")
            ppu_ts, ppu_scatter, ppu_peers = _cmj_or_ppu(cur, uuid, "ppu")
            score_history = _score_history(cur, uuid)
            latest_score = score_history[-1] if score_history else None
            power_curves = _power_curve_history(cur, uuid)
            grip_series = _grip_series(cur, uuid)
            movement_strategy = _movement_strategy(cur, uuid)
            intra_session = _intra_session_trials(cur, uuid)

        # today_vs_baseline and flag_heatmap are derived from score_history flags_json.
        today_vs_baseline = _today_vs_baseline(latest_score)
        flag_heatmap = _flag_heatmap(score_history)

        return jsonify({
            "athlete":           athlete,
            "iso":               iso_series,
            "cmj":               {"timeseries": cmj_ts, "scatter": cmj_scatter, "peers": cmj_peers},
            "ppu":               {"timeseries": ppu_ts, "scatter": ppu_scatter, "peers": ppu_peers},
            "score_history":     score_history,
            "latest_score":      latest_score,
            "power_curves":      power_curves,
            "grip":              {"timeseries": grip_series},
            "movement_strategy": movement_strategy,
            "intra_session":     intra_session,
            "today_vs_baseline": today_vs_baseline,
            "flag_heatmap":      flag_heatmap,
        })
    finally:
        conn.close()


# ─── Helpers ────────────────────────────────────────────────────────────────

def _iso_series(cur, uuid: str) -> Dict[str, List[Dict]]:
    """{movement: [{date, avg_force, max_force, time_to_max}, ...]} for all four ISO tests.

    I and T are marked legacy=True for the dashboard toggle; Y and IR90 are active.
    """
    out = {}
    for movement, table, legacy in (
        ("I",    "f_readiness_screen_i",    True),
        ("Y",    "f_readiness_screen_y",    False),
        ("T",    "f_readiness_screen_t",    True),
        ("IR90", "f_readiness_screen_ir90", False),
    ):
        cur.execute(
            f"""
            SELECT session_date,
                   AVG(avg_force)::float    AS avg_force,
                   AVG(max_force)::float    AS max_force,
                   AVG(time_to_max)::float  AS time_to_max
              FROM public.{table}
             WHERE athlete_uuid = %s
             GROUP BY session_date
             ORDER BY session_date
            """,
            (uuid,),
        )
        out[movement] = {
            "legacy": legacy,
            "data": [
                {
                    "date":        r["session_date"].isoformat() if r["session_date"] else None,
                    "avg_force":   r["avg_force"],
                    "max_force":   r["max_force"],
                    "time_to_max": r["time_to_max"],
                }
                for r in cur.fetchall()
            ],
        }
    return out


def _cmj_or_ppu(cur, uuid: str, kind: str):
    """Returns (timeseries_for_athlete, this_athlete_scatter_points, peer_scatter_points)."""
    rs_table  = f"f_readiness_screen_{kind}"
    ath_table = f"f_athletic_screen_{kind}"
    cur.execute(
        f"""
        SELECT session_date,
               AVG(jump_height)::float   AS jump_height,
               AVG(pp_w_per_kg)::float   AS pp_w_per_kg,
               AVG(pp_forceplate)::float AS pp_forceplate,
               AVG(force_at_pp)::float   AS force_at_pp,
               AVG(vel_at_pp)::float     AS vel_at_pp,
               source_system
          FROM (
            SELECT session_date, jump_height, pp_w_per_kg,
                   pp_forceplate, force_at_pp, vel_at_pp, source_system
              FROM public.{rs_table}
             WHERE athlete_uuid = %s
            UNION ALL
            SELECT session_date, jh_in AS jump_height, pp_w_per_kg,
                   pp_forceplate, force_at_pp, vel_at_pp, source_system
              FROM public.{ath_table}
             WHERE athlete_uuid = %s
          ) _u
         GROUP BY session_date, source_system
         ORDER BY session_date
        """,
        (uuid, uuid),
    )
    rows = cur.fetchall()
    timeseries = [
        {
            "date":          r["session_date"].isoformat() if r["session_date"] else None,
            "jump_height":   r["jump_height"],
            "pp_w_per_kg":   r["pp_w_per_kg"],
            "pp_forceplate": r["pp_forceplate"],
            "force_at_pp":   r["force_at_pp"],
            "vel_at_pp":     r["vel_at_pp"],
            "source":        r["source_system"],
        }
        for r in rows
    ]
    scatter = [
        {"date": p["date"], "force_at_pp": p["force_at_pp"], "vel_at_pp": p["vel_at_pp"]}
        for p in timeseries
        if p["force_at_pp"] is not None and p["vel_at_pp"] is not None
    ]

    cur.execute(
        f"""
        SELECT athlete_uuid, session_date,
               AVG(force_at_pp)::float AS f,
               AVG(vel_at_pp)::float   AS v
          FROM (
            SELECT athlete_uuid, session_date, force_at_pp, vel_at_pp
              FROM public.{rs_table}
             WHERE athlete_uuid <> %s
               AND force_at_pp IS NOT NULL AND vel_at_pp IS NOT NULL
            UNION ALL
            SELECT athlete_uuid, session_date, force_at_pp, vel_at_pp
              FROM public.{ath_table}
             WHERE athlete_uuid <> %s
               AND force_at_pp IS NOT NULL AND vel_at_pp IS NOT NULL
          ) _u
         GROUP BY athlete_uuid, session_date
         LIMIT 1500
        """,
        (uuid, uuid),
    )
    peers = [{"force_at_pp": r["f"], "vel_at_pp": r["v"]} for r in cur.fetchall()]
    return timeseries, scatter, peers


def _score_history(cur, uuid: str) -> List[Dict]:
    cur.execute(
        """
        SELECT session_date, composite_score, composite_z, band,
               cmj_z, ppu_z, iso_z, power_curve_z, grip_z,
               metrics_used, flags_json
          FROM public.f_readiness_screen_score
         WHERE athlete_uuid = %s
         ORDER BY session_date
        """,
        (uuid,),
    )
    return [
        {
            "date":            r["session_date"].isoformat() if r["session_date"] else None,
            "composite_score": float(r["composite_score"]) if r["composite_score"] is not None else None,
            "composite_z":     float(r["composite_z"]) if r["composite_z"] is not None else None,
            "band":            r["band"],
            "cmj_z":           float(r["cmj_z"]) if r["cmj_z"] is not None else None,
            "ppu_z":           float(r["ppu_z"]) if r["ppu_z"] is not None else None,
            "iso_z":           float(r["iso_z"]) if r["iso_z"] is not None else None,
            "power_curve_z":   float(r["power_curve_z"]) if r["power_curve_z"] is not None else None,
            "grip_z":          float(r["grip_z"]) if r["grip_z"] is not None else None,
            "metrics_used":    r["metrics_used"],
            "flags":           r["flags_json"],
        }
        for r in cur.fetchall()
    ]


def _power_curve_history(cur, uuid: str) -> Dict[str, List[Dict]]:
    """Merges f_readiness_screen_power_curve with inline power columns from f_athletic_screen_cmj/ppu."""
    out = {}
    for movement, ath_table in (("CMJ", "f_athletic_screen_cmj"), ("PPU", "f_athletic_screen_ppu")):
        cur.execute(
            f"""
            SELECT session_date,
                   AVG(peak_power_w)::float    AS peak_power_w,
                   AVG(rpd_max_w_per_s)::float AS rpd_max,
                   AVG(rise_slope)::float      AS rise_slope,
                   AVG(fwhm_s)::float          AS fwhm,
                   AVG(auc_j)::float           AS auc_j,
                   AVG(decay_90_10_s)::float   AS decay
              FROM (
                SELECT session_date, peak_power_w, rpd_max_w_per_s,
                       rise_slope_w_per_s AS rise_slope, fwhm_s, auc_j, decay_90_10_s
                  FROM public.f_readiness_screen_power_curve
                 WHERE athlete_uuid = %s AND movement_type = %s
                UNION ALL
                SELECT session_date, peak_power_w, rpd_max_w_per_s,
                       NULL::numeric AS rise_slope, fwhm_s, auc_j, decay_90_10_s
                  FROM public.{ath_table}
                 WHERE athlete_uuid = %s
                   AND peak_power_w IS NOT NULL
              ) _u
             GROUP BY session_date
             ORDER BY session_date
            """,
            (uuid, movement, uuid),
        )
        out[movement] = [
            {
                "date":         r["session_date"].isoformat() if r["session_date"] else None,
                "peak_power_w": r["peak_power_w"],
                "rpd_max":      r["rpd_max"],
                "rise_slope":   r["rise_slope"],
                "fwhm":         r["fwhm"],
                "auc_j":        r["auc_j"],
                "decay":        r["decay"],
            }
            for r in cur.fetchall()
        ]
    return out


def _grip_series(cur, uuid: str) -> List[Dict]:
    cur.execute(
        """
        SELECT session_date, left_kg, right_kg, avg_kg, max_kg, asymmetry_pct
          FROM public.f_readiness_screen_grip
         WHERE athlete_uuid = %s
         ORDER BY session_date
        """,
        (uuid,),
    )
    return [
        {
            "date":          r["session_date"].isoformat() if r["session_date"] else None,
            "left_kg":       float(r["left_kg"]) if r["left_kg"] is not None else None,
            "right_kg":      float(r["right_kg"]) if r["right_kg"] is not None else None,
            "avg_kg":        float(r["avg_kg"]) if r["avg_kg"] is not None else None,
            "max_kg":        float(r["max_kg"]) if r["max_kg"] is not None else None,
            "asymmetry_pct": float(r["asymmetry_pct"]) if r["asymmetry_pct"] is not None else None,
        }
        for r in cur.fetchall()
    ]


def _movement_strategy(cur, uuid: str) -> Dict[str, List[Dict]]:
    """Phase-analysis metrics per session (averaged across trials per day)."""
    out = {}
    for kind, table in (("cmj", "f_readiness_screen_cmj"), ("ppu", "f_readiness_screen_ppu")):
        cur.execute(
            f"""
            SELECT session_date,
                   AVG(mrsi)::float                    AS mrsi,
                   AVG(contraction_time_s)::float      AS contraction_time_s,
                   AVG(ecc_con_duration_ratio)::float  AS ecc_con_duration_ratio,
                   AVG(eccentric_mean_power_w)::float  AS eccentric_mean_power_w,
                   AVG(concentric_duration_s)::float   AS concentric_duration_s
              FROM public.{table}
             WHERE athlete_uuid = %s
               AND mrsi IS NOT NULL
             GROUP BY session_date
             ORDER BY session_date
            """,
            (uuid,),
        )
        out[kind] = [
            {
                "date":                 r["session_date"].isoformat() if r["session_date"] else None,
                "mrsi":                 r["mrsi"],
                "contraction_time_s":   r["contraction_time_s"],
                "ecc_con_duration_ratio": r["ecc_con_duration_ratio"],
                "eccentric_mean_power_w": r["eccentric_mean_power_w"],
                "concentric_duration_s":  r["concentric_duration_s"],
            }
            for r in cur.fetchall()
        ]
    return out


def _intra_session_trials(cur, uuid: str) -> Dict[str, List[Dict]]:
    """Per-session individual trial values for CMJ and PPU (last 30 sessions)."""
    out = {}
    for kind, table in (("cmj", "f_readiness_screen_cmj"), ("ppu", "f_readiness_screen_ppu")):
        cur.execute(
            f"""
            SELECT session_date, trial_name,
                   jump_height, peak_power_w, mrsi
              FROM public.{table}
             WHERE athlete_uuid = %s
             ORDER BY session_date DESC, trial_name
             LIMIT 120
            """,
            (uuid,),
        )
        rows = cur.fetchall()
        by_date: Dict[str, List] = {}
        for r in rows:
            d = r["session_date"].isoformat() if r["session_date"] else None
            if d not in by_date:
                by_date[d] = []
            by_date[d].append({
                "trial":        r["trial_name"],
                "jump_height":  float(r["jump_height"]) if r["jump_height"] is not None else None,
                "peak_power_w": float(r["peak_power_w"]) if r["peak_power_w"] is not None else None,
                "mrsi":         float(r["mrsi"]) if r["mrsi"] is not None else None,
            })
        out[kind] = [
            {"date": d, "trials": trials}
            for d, trials in sorted(by_date.items())
        ]
    return out


def _today_vs_baseline(latest_score: Optional[Dict]) -> List[Dict]:
    """Flatten the latest session's per_metric flags_json into a list for the horizontal bar chart."""
    if not latest_score or not latest_score.get("flags"):
        return []
    try:
        flags = latest_score["flags"]
        if isinstance(flags, str):
            flags = json.loads(flags)
        per_metric = flags.get("per_metric", {})
        result = []
        for label, m in per_metric.items():
            if m.get("flag") == "insufficient_history":
                result.append({
                    "metric":    label,
                    "group":     label.split(".")[0] if "." in label else label,
                    "label":     label,
                    "today":     m.get("today"),
                    "mean":      None,
                    "sd":        None,
                    "z":         None,
                    "flag":      "insufficient_history",
                    "n_history": m.get("n_history", 0),
                })
            else:
                result.append({
                    "metric":    label,
                    "group":     label.split(".")[0] if "." in label else label,
                    "label":     label,
                    "today":     m.get("today"),
                    "mean":      m.get("mean"),
                    "sd":        m.get("sd"),
                    "z":         m.get("z"),
                    "flag":      m.get("flag"),
                    "n_history": m.get("n_history", 0),
                })
        return result
    except Exception:
        return []


def _flag_heatmap(score_history: List[Dict]) -> Dict:
    """Build the 14-day × all-metrics flag heatmap from score_history flags_json."""
    recent = score_history[-FLAG_HEATMAP_DAYS:] if len(score_history) > FLAG_HEATMAP_DAYS else score_history
    if not recent:
        return {"dates": [], "metrics": [], "cells": []}

    # Collect all metric keys across all days.
    all_metrics_seen: List[str] = []
    metric_set: set = set()
    date_flags: Dict[str, Dict[str, str]] = {}

    for session in recent:
        d = session.get("date")
        flags = session.get("flags") or {}
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except Exception:
                flags = {}
        per_metric = flags.get("per_metric", {})
        date_flags[d] = {k: v.get("flag", "stable") for k, v in per_metric.items()}
        for k in per_metric:
            if k not in metric_set:
                metric_set.add(k)
                all_metrics_seen.append(k)

    dates = [s["date"] for s in recent]
    cells = [
        [date_flags.get(d, {}).get(m, None) for m in all_metrics_seen]
        for d in dates
    ]

    return {"dates": dates, "metrics": all_metrics_seen, "cells": cells}
