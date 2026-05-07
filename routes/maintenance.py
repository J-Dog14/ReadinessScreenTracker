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
from ingestion.athlete_manager import search_athletes
from ingestion.file_parsers import ASCII_FILES, discover_txt_files
from ingestion.pipeline import run_ingestion

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
    return jsonify({"dir": output_dir, "files": {k: v for k, v in found.items()}})


# ─── Run ────────────────────────────────────────────────────────────────────
@bp.route("/api/run", methods=["POST"])
def run():
    body = request.get_json(silent=True) or {}
    output_dir = (body.get("output_dir") or "").strip()
    power_dir = (body.get("power_dir") or output_dir).strip()
    fs_hz = float(body.get("fs_hz") or 1000)
    athlete_uuid = body.get("athlete_uuid") or None
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

    def worker():
        try:
            summary = run_ingestion(
                output_dir,
                power_dir=power_dir,
                fs_hz=fs_hz,
                log=log,
                athlete_uuid_override=athlete_uuid,
                cancel_event=job.cancelled,
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
    return jsonify({"results": search_athletes(q)})
