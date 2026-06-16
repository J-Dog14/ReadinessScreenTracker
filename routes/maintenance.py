"""
Maintenance page routes:
  GET  /maintenance                — Render the page.
  GET  /api/scan?dir=...           — Discover which movement files are present.
  POST /api/run                    — Kick off an ingestion job; returns {job_id}.
  GET  /api/stream/<job_id>        — Server-Sent Events stream of log lines + final summary.
  POST /api/kill/<job_id>          — Mark job for cancellation (best-effort).
  GET  /api/athletes/search?q=...  — Search analytics.d_athletes for the existing-athlete picker.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid as uuid_pkg
from typing import Dict

from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context

from config import get_output_dir, get_power_dir, get_power_sample_rate_hz
from db.connection import get_connection
from ingestion.athlete_manager import search_athletes
from ingestion.file_parsers import ASCII_FILES, discover_cmj_ppu_trials, discover_txt_files, extract_name
from ingestion.pipeline import _calc_age, _upsert, run_ingestion

bp = Blueprint("maintenance", __name__)


# ─── In-process job table ───────────────────────────────────────────────────
# Each job is a queue of events the SSE endpoint drains. Jobs live until their
# stream is consumed; we don't purge them automatically since this is a
# single-user lab tool.
class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.cancelled = threading.Event()
        self.summary: Dict | None = None
        self.finished = threading.Event()


_jobs: Dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _new_job() -> Job:
    j = Job(uuid_pkg.uuid4().hex[:12])
    with _jobs_lock:
        _jobs[j.id] = j
    return j


def _get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ─── Page ───────────────────────────────────────────────────────────────────
@bp.route("/maintenance")
def page():
    return render_template(
        "maintenance.html",
        ascii_files=ASCII_FILES,
        default_output_dir=get_output_dir(),
        default_power_dir=get_power_dir(),
        default_fs_hz=int(get_power_sample_rate_hz()),
    )


# ─── Folder scan ────────────────────────────────────────────────────────────
@bp.route("/api/scan")
def scan():
    output_dir = request.args.get("dir", "").strip()
    if not output_dir:
        return jsonify({"error": "dir required"}), 400
    found = discover_txt_files(output_dir)
    for trial in discover_cmj_ppu_trials(output_dir):
        found[trial["trial_name"]] = trial["file_path"]

    files_out = {}
    for movement, file_path in found.items():
        athlete_name = None
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                athlete_name = extract_name(fh.readline())
        except OSError:
            pass
        files_out[movement] = {"path": file_path, "athlete_name": athlete_name}

    return jsonify({"dir": output_dir, "files": files_out})


# ─── Run ────────────────────────────────────────────────────────────────────
@bp.route("/api/run", methods=["POST"])
def run():
    body = request.get_json(silent=True) or {}
    output_dir = (body.get("output_dir") or "").strip()
    power_dir = (body.get("power_dir") or output_dir).strip()
    fs_hz = float(body.get("fs_hz") or 1000)
    athlete_uuid = body.get("athlete_uuid") or None
    is_hitter = bool(body.get("is_hitter", False))
    if not output_dir:
        return jsonify({"error": "output_dir required"}), 400

    job = _new_job()

    def log(msg: str):
        # The pipeline emits "[stage] body" lines; split for nicer SSE rendering.
        if isinstance(msg, str) and msg.startswith("[") and "]" in msg:
            close = msg.index("]")
            stage = msg[1:close]
            body_text = msg[close + 1 :].strip()
        else:
            stage = "log"
            body_text = msg
        job.events.put({"type": "log", "stage": stage, "msg": body_text})

    grip_raw = body.get("grip") or {}
    grip_payload = None
    if grip_raw.get("left_kg") or grip_raw.get("right_kg"):
        grip_payload = {
            "left_kg":      grip_raw.get("left_kg"),
            "right_kg":     grip_raw.get("right_kg"),
            "dominant_hand": grip_raw.get("dominant_hand") or None,
            "notes":         grip_raw.get("notes") or None,
        }

    def worker():
        try:
            summary = run_ingestion(
                output_dir,
                power_dir=power_dir,
                fs_hz=fs_hz,
                log=log,
                athlete_uuid_override=athlete_uuid,
                cancel_event=job.cancelled,
                grip_payload=grip_payload,
                is_hitter=is_hitter,
            )
            job.summary = summary
        except Exception as e:
            job.summary = {"errors": [str(e)], "rows_inserted": 0, "rows_updated": 0,
                           "power_curve_rows": 0, "athletes": [], "scores": []}
            job.events.put({"type": "log", "stage": "ERROR", "msg": str(e)})
        finally:
            job.events.put({"type": "done"})
            job.finished.set()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job.id})


@bp.route("/api/stream/<job_id>")
def stream(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404

    @stream_with_context
    def gen():
        # Heartbeat keeps proxies happy; not strictly needed for localhost.
        last_heartbeat = time.time()
        while True:
            try:
                ev = job.events.get(timeout=2.0)
            except queue.Empty:
                if job.finished.is_set():
                    # Drain any final events.
                    while not job.events.empty():
                        ev = job.events.get_nowait()
                        yield _sse_event(ev, job)
                    return
                if time.time() - last_heartbeat > 15:
                    yield ":\n\n"  # SSE comment heartbeat
                    last_heartbeat = time.time()
                continue
            yield _sse_event(ev, job)
            if ev.get("type") == "done":
                return

    return Response(gen(), mimetype="text/event-stream")


def _sse_event(ev: Dict, job: Job) -> str:
    if ev.get("type") == "log":
        payload = json.dumps({"stage": ev.get("stage", "log"), "msg": ev.get("msg", "")})
        return f"event: log\ndata: {payload}\n\n"
    if ev.get("type") == "done":
        payload = json.dumps(job.summary or {})
        return f"event: done\ndata: {payload}\n\n"
    return ""


@bp.route("/api/kill/<job_id>", methods=["POST"])
def kill(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    job.cancelled.set()
    # The pipeline doesn't currently check `cancelled`; this endpoint exists
    # as a hook for future cancellation support but won't actually stop the
    # run mid-flight today.
    return jsonify({"ok": True})


@bp.route("/api/athletes/search")
def athlete_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"results": []})
    try:
        return jsonify({"results": search_athletes(q)})
    except Exception:
        return jsonify({"results": []})


@bp.route("/api/athletes/lookup")
def athlete_lookup():
    """Resolve a raw file-extracted name to an athlete + their most recent dominant hand.

    Uses the same normalize→exact→fuzzy resolution as the ingestion pipeline so
    auto-detect mode reliably identifies the same athlete the pipeline would pick.
    """
    from ingestion.athlete_manager import find_existing_athlete, normalize_name_for_matching
    name = request.args.get("name", "").strip()
    if len(name) < 2:
        return jsonify({"athlete": None})
    normalized = normalize_name_for_matching(name)
    conn = get_connection()
    try:
        existing = find_existing_athlete(conn, normalized)
        if not existing:
            return jsonify({"athlete": None})
        athlete_uuid = str(existing["athlete_uuid"])
        dominant_hand = None
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT dominant_hand
                    FROM   analytics.f_readiness_screen_grip
                    WHERE  athlete_uuid = %s
                      AND  dominant_hand IS NOT NULL
                    ORDER  BY session_date DESC
                    LIMIT  1
                    """,
                    (athlete_uuid,),
                )
                row = cur.fetchone()
                dominant_hand = row[0] if row else None
            except Exception:
                conn.rollback()
        return jsonify({"athlete": {
            "athlete_uuid": athlete_uuid,
            "name":         existing.get("name"),
            "age_group":    existing.get("age_group"),
            "dominant_hand": dominant_hand,
        }})
    finally:
        conn.close()


@bp.route("/api/grip-log", methods=["POST"])
def grip_log():
    """Insert a standalone grip strength row for an athlete. No scoring triggered."""
    from datetime import date as _date
    body = request.get_json(silent=True) or {}
    athlete_uuid = (body.get("athlete_uuid") or "").strip()
    session_date_str = (body.get("session_date") or "").strip()
    left_raw  = body.get("left_kg")
    right_raw = body.get("right_kg")

    if not athlete_uuid:
        return jsonify({"error": "athlete_uuid required"}), 400
    if left_raw is None and right_raw is None:
        return jsonify({"error": "at least one grip value required"}), 400

    try:
        session_date = _date.fromisoformat(session_date_str) if session_date_str else _date.today()
    except ValueError:
        return jsonify({"error": "invalid session_date"}), 400

    lkg = float(left_raw)  if left_raw  is not None else None
    rkg = float(right_raw) if right_raw is not None else None
    both = lkg is not None and rkg is not None
    avg_kg = (lkg + rkg) / 2.0 if both else None
    max_kg = max(v for v in (lkg, rkg) if v is not None) if (lkg or rkg) else None
    asym   = (100.0 * abs(lkg - rkg) / max_kg) if both and max_kg else None

    age_at_collection, age_group, date_str = _calc_age(athlete_uuid, session_date.isoformat())

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name FROM analytics.d_athletes WHERE athlete_uuid = %s",
                (athlete_uuid,),
            )
            row = cur.fetchone()
            athlete_name = row[0] if row else athlete_uuid

        grip_data = {
            "athlete_uuid":      athlete_uuid,
            "session_date":      date_str,
            "source_system":     "readiness_screen",
            "source_athlete_id": athlete_name,
            "age_at_collection": age_at_collection,
            "age_group":         age_group,
            "left_kg":           lkg,
            "right_kg":          rkg,
            "avg_kg":            avg_kg,
            "max_kg":            max_kg,
            "asymmetry_pct":     asym,
            "dominant_hand":     body.get("dominant_hand") or None,
            "entry_source":      "manual",
            "notes":             body.get("notes") or None,
        }
        update_cols = [
            "left_kg", "right_kg", "avg_kg", "max_kg", "asymmetry_pct",
            "dominant_hand", "entry_source", "notes", "age_at_collection", "age_group",
        ]
        verb = _upsert(
            conn, "f_readiness_screen_grip", grip_data, update_cols,
            "athlete_uuid = %s AND session_date = %s",
            (athlete_uuid, date_str),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

    return jsonify({"ok": True, "verb": verb, "date": date_str, "athlete_name": athlete_name})
