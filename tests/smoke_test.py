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


# 1) parse_txt_file (CMJ shape)
print("[1] parse_txt_file CMJ")
cmj_text = (
    "\tD:\\Athletic Screen 2.0\\Data\\Test_Athlete\\2024-11-24__1\\Trial.cmj\n"
    "Header1\tHeader2\n"
    "metadata\n"
    "metadata\n"
    "metadata\n"
    "1\t12.5\t4500.2\t1850.0\t72.3\t1810.5\t1700.4\t2.85\n"
)
with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
    f.write(cmj_text)
    cmj_path = f.name
parsed = parse_txt_file(cmj_path, "CMJ")
os.unlink(cmj_path)
if parsed is None: fail("parse returned None")
if parsed["name"] != "Test_Athlete": fail("name=%r" % parsed["name"])
if parsed["date"] != "2024-11-24":   fail("date=%r" % parsed["date"])
if abs(parsed["JH_IN"] - 12.5) > 1e-6: fail("JH_IN=%r" % parsed["JH_IN"])
if abs(parsed["LEWIS_PEAK_POWER"] - 4500.2) > 1e-6: fail("LEWIS_PEAK_POWER")
if abs(parsed["Vel_at_PP"] - 2.85) > 1e-6: fail("Vel_at_PP")
ok("CMJ row parsed correctly")

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

print("\nALL SMOKE TESTS PASSED.")
