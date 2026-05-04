"""
Dashboard routes:

  GET /dashboard                            — Render the page (athlete dropdown + chart shells).
  GET /api/dashboard/athletes               — List athletes with any readiness data.
  GET /api/dashboard/athlete/<uuid>         — All time-series + scores + peer scatter data
                                              for one athlete. Drives every chart on the page.

The dashboard mirrors the original Dash-based readiness dashboard layout:
  Row 1: I/Y/T/IR90 isometric Avg Force time series + stat block
  Row 2: CMJ jump height time series + Force-vs-Velocity scatter (peers + this athlete)
  Row 3: PPU jump height time series + Force-vs-Velocity scatter (peers + this athlete)
With one new section above:
  Row 0: Composite readiness score gauge + sub-score breakdown + score history.
"""
from __future__ import annotations

from typing import Dict, List

from flask import Blueprint, jsonify, render_template, request
from psycopg2.extras import RealDictCursor

from db.connection import get_connection
from ingestion.athlete_manager import list_athletes_with_readiness

bp = Blueprint("dashboard", __name__)


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

        return jsonify({
            "athlete":       athlete,
            "iso":           iso_series,
            "cmj":           {"timeseries": cmj_ts, "scatter": cmj_scatter, "peers": cmj_peers},
            "ppu":           {"timeseries": ppu_ts, "scatter": ppu_scatter, "peers": ppu_peers},
            "score_history": score_history,
            "latest_score":  latest_score,
            "power_curves":  power_curves,
        })
    finally:
        conn.close()


# ─── Helpers ────────────────────────────────────────────────────────────────

def _iso_series(cur, uuid: str) -> Dict[str, List[Dict]]:
    """{movement: [{date, avg_force, max_force, time_to_max}, ...]} for I/Y/T/IR90."""
    out = {}
    for movement, table in (
        ("I", "f_readiness_screen_i"),
        ("Y", "f_readiness_screen_y"),
        ("T", "f_readiness_screen_t"),
        ("IR90", "f_readiness_screen_ir90"),
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
        out[movement] = [
            {
                "date":        r["session_date"].isoformat() if r["session_date"] else None,
                "avg_force":   r["avg_force"],
                "max_force":   r["max_force"],
                "time_to_max": r["time_to_max"],
            }
            for r in cur.fetchall()
        ]
    return out


def _cmj_or_ppu(cur, uuid: str, kind: str):
    """Returns (timeseries_for_athlete, this_athlete_scatter_points, peer_scatter_points).

    Merges f_readiness_screen_{kind} and f_athletic_screen_{kind} via UNION ALL.
    Rows carry source_system so the JS can render athletic screen points with distinct markers.
    """
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
               cmj_z, ppu_z, iso_z, power_curve_z, metrics_used, flags_json
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
