"""
Smoke test for the Readiness Screen Tracker app.

Run from the project root:
    python tests/smoke_test.py

No live database required — this exercises only the parsers, name normalization,
age group bands, power-curve math, and the pure-Python scoring helpers.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ingestion.file_parsers import extract_date, extract_name, parse_txt_file
from ingestion.age_utils import calculate_age_group
from ingestion.athlete_manager import (
    normalize_name_for_display,
    normalize_name_for_matching,
)
from ingestion.power_analysis import analyze_power_curve_advanced
from ingestion import scoring


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


def ok(msg):
    print("  ok:", msg)


# 1) parse_txt_file (CMJ shape — 5-column Athletic Screen format)
print("[1] parse_txt_file CMJ")
cmj_text = (
    "\tD:\\Readiness Screen 3\\Data\\Test_Athlete\\2024-11-24__1\\CMJ1.c3d\n"
    "Header1\tHeader2\n"
    "metadata\n"
    "metadata\n"
    "metadata\n"
    "1\t15.7\t374.8\t1648.0\t227.40\t4.47\n"
)
with tempfile.NamedTemporaryFile("w", suffix="CMJ1.txt", delete=False) as f:
    f.write(cmj_text)
    cmj_path = f.name
parsed = parse_txt_file(cmj_path, "CMJ")
os.unlink(cmj_path)
if parsed is None: fail("parse returned None")
if parsed["name"] != "Test_Athlete":          fail("name=%r" % parsed["name"])
if parsed["date"] != "2024-11-24":            fail("date=%r" % parsed["date"])
if abs(parsed["JH_IN"] - 15.7) > 1e-6:       fail("JH_IN=%r" % parsed["JH_IN"])
if abs(parsed["PP_FORCEPLATE"] - 374.8) > 1e-6: fail("PP_FORCEPLATE=%r" % parsed["PP_FORCEPLATE"])
if abs(parsed["Force_at_PP"] - 1648.0) > 1e-6:  fail("Force_at_PP=%r" % parsed["Force_at_PP"])
if abs(parsed["Vel_at_PP"] - 227.40) > 1e-6: fail("Vel_at_PP=%r" % parsed["Vel_at_PP"])
if abs(parsed["PP_W_per_kg"] - 4.47) > 1e-6: fail("PP_W_per_kg=%r" % parsed["PP_W_per_kg"])
ok("CMJ row parsed correctly (5-column format)")

# 2) extract_name / extract_date
print("[2] extract_name / extract_date")
line = "\tD:\\Athletic Screen 2.0\\Data\\Weiss, Ryan 11-25\\2025-04-12__2\\..."
n = extract_name(line)
d = extract_date(line)
if n != "Weiss, Ryan 11-25": fail("name=%r" % n)
if d != "2025-04-12": fail("date=%r" % d)
ok("name+date extracted: %r %r" % (n, d))

# 3) age groups
print("[3] age groups")
for age, expected in [(13, "YOUTH"), (14, "HIGH SCHOOL"), (18, "HIGH SCHOOL"),
                     (19, "COLLEGE"), (22, "COLLEGE"), (23, "PRO"), (35, "PRO")]:
    got = calculate_age_group(age)
    if got != expected: fail("age %s: got %s, expected %s" % (age, got, expected))
ok("age group bands correct")

# 4) name normalization
print("[4] name normalization")
cases = [
    ("Weiss, Ryan 11-25", "RYAN WEISS",   "Ryan Weiss"),
    ("Crider. Carson",    "CARSON CRIDER", "Carson Crider"),
    ("Ryan Weiss",        "RYAN WEISS",   "Ryan Weiss"),
    ("RYAN WEISS_CH",     "RYAN WEISS CH", "RYAN WEISS_CH"),
]
for raw, exp_norm, exp_disp in cases:
    nm = normalize_name_for_matching(raw)
    dp = normalize_name_for_display(raw)
    if nm != exp_norm:  fail("matching: %r -> %r (want %r)" % (raw, nm, exp_norm))
    if dp != exp_disp:  fail("display:  %r -> %r (want %r)" % (raw, dp, exp_disp))
ok("normalization rules match backend")

# 5) power curve
print("[5] power curve")
fs = 1000
n = 1000
t = np.arange(n) / fs
power = 3000 * np.exp(-((t - 0.5) ** 2) / (2 * 0.05 ** 2))
metrics = analyze_power_curve_advanced(power, fs_hz=fs)
if abs(metrics["peak_power_w"] - 3000.0) > 1.0: fail("peak_power_w=%r" % metrics["peak_power_w"])
if abs(metrics["time_to_peak_s"] - 0.5) > 0.01: fail("time_to_peak_s=%r" % metrics["time_to_peak_s"])
if not (metrics["fwhm_s"] > 0.05 and metrics["fwhm_s"] < 0.20): fail("fwhm_s=%r" % metrics["fwhm_s"])
if metrics["rpd_max_w_per_s"] <= 0: fail("rpd_max not positive")
if not math.isfinite(metrics["spectral_centroid_hz"]): fail("spectral_centroid_hz nonfinite")
ok("peak=%.0fW FWHM=%.3fs RPD=%.0fW/s spectral=%.1fHz" % (
    metrics["peak_power_w"], metrics["fwhm_s"], metrics["rpd_max_w_per_s"], metrics["spectral_centroid_hz"]))

# 6) scoring math
print("[6] scoring math")
res = scoring._zscore(11.5, [10.0, 9.5, 10.5, 9.8, 10.2])
if res is None: fail("zscore returned None on healthy input")
z, mean, sd = res
if abs(mean - 10.0) > 0.01: fail("mean=%r" % mean)
ok("z computed: z=%.3f (sd=%.3f)" % (z, sd))

composite_z = 1.0
expected_score = 50 + scoring.SCORE_SD_TO_POINTS * composite_z
if abs(expected_score - 65.0) > 0.0: fail("composite mapping %r" % expected_score)
ok("composite mapping: z=1.0 -> 65.0")

if scoring._zscore(10.0, [10.0]) is not None:
    fail("expected None for too-short baseline")
ok("insufficient history yields None")

# 7) pipeline dry-run with mocked DB — verifies INSERT reaches the right table
print("[7] pipeline dry-run (mocked DB)")
import threading
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

_ISO_TXT = (
    "\tD:\\Readiness Screen 3\\Data\\Smith_John\\2026-05-07__1\\i_data.txt\n"
    "h\n" "h\n" "h\n" "h\n"
    "1\t850.0\t12.5\t700.0\t10.2\t0.35\n"
)
with tempfile.TemporaryDirectory() as _tmpdir:
    with open(os.path.join(_tmpdir, "i_data.txt"), "w") as _f:
        _f.write(_ISO_TXT)

    _mock_cur = MagicMock()
    _mock_cur.fetchone.return_value = None   # row absent → INSERT path
    _mock_conn = MagicMock()
    _mock_conn.cursor.return_value.__enter__.return_value = _mock_cur
    _mock_conn.cursor.return_value.__exit__.return_value = False

    _inserted = []
    def _spy(sql, params=None):
        if "INSERT INTO" in sql:
            _inserted.append(sql.split("INSERT INTO")[1].split()[0].strip())
    _mock_cur.execute = _spy

    from ingestion import pipeline as _pl
    with ExitStack() as _s:
        _s.enter_context(patch("ingestion.pipeline.get_connection",           return_value=_mock_conn))
        _s.enter_context(patch("ingestion.pipeline.get_or_create_athlete",    return_value=("test-uuid-0001", False)))
        _s.enter_context(patch("ingestion.pipeline.get_athlete_dob",          return_value=None))
        _s.enter_context(patch("ingestion.pipeline.update_athlete_data_flag"))
        _s.enter_context(patch("ingestion.pipeline.update_athlete_age_group"))
        _s.enter_context(patch("ingestion.pipeline.call_update_athlete_data_flags"))
        _s.enter_context(patch("ingestion.pipeline.score_session",
                               return_value={"composite_score": 72, "band": "READY", "composite_z": 1.5}))
        _summary = _pl.run_ingestion(_tmpdir, log=lambda m: None)

    if "public.f_readiness_screen_i" not in _inserted:
        fail("Expected INSERT into public.f_readiness_screen_i, got: %r" % _inserted)
    if _summary["rows_inserted"] != 1:
        fail("Expected 1 row inserted, got rows_inserted=%r" % _summary["rows_inserted"])
    ok("pipeline dry-run: INSERT into %s confirmed, rows_inserted=%d" % (_inserted, _summary["rows_inserted"]))

# 8) cancel mid-run — no rows written when cancel_event is pre-set
print("[8] pipeline cancel (pre-set event)")
with tempfile.TemporaryDirectory() as _tmpdir2:
    with open(os.path.join(_tmpdir2, "i_data.txt"), "w") as _f:
        _f.write(_ISO_TXT)

    _mock_cur2 = MagicMock()
    _mock_cur2.fetchone.return_value = None
    _mock_conn2 = MagicMock()
    _mock_conn2.cursor.return_value.__enter__.return_value = _mock_cur2
    _mock_conn2.cursor.return_value.__exit__.return_value = False
    _mock_cur2.execute = lambda sql, params=None: None

    _cancel = threading.Event()
    _cancel.set()

    with ExitStack() as _s:
        _s.enter_context(patch("ingestion.pipeline.get_connection",           return_value=_mock_conn2))
        _s.enter_context(patch("ingestion.pipeline.get_or_create_athlete",    return_value=("test-uuid-0002", False)))
        _s.enter_context(patch("ingestion.pipeline.get_athlete_dob",          return_value=None))
        _s.enter_context(patch("ingestion.pipeline.update_athlete_data_flag"))
        _s.enter_context(patch("ingestion.pipeline.update_athlete_age_group"))
        _s.enter_context(patch("ingestion.pipeline.call_update_athlete_data_flags"))
        _s.enter_context(patch("ingestion.pipeline.score_session",            return_value={}))
        _summary2 = _pl.run_ingestion(_tmpdir2, log=lambda m: None, cancel_event=_cancel)

    if _summary2["rows_inserted"] != 0 or _summary2["rows_updated"] != 0:
        fail("Cancelled run should write 0 rows, got: inserted=%r updated=%r"
             % (_summary2["rows_inserted"], _summary2["rows_updated"]))
    ok("cancel: 0 rows written when run cancelled before first loop")

print("\nALL SMOKE TESTS PASSED.")
