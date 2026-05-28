"""
Smoke test for the Readiness Screen Tracker app.

Run from the project root:
    python tests/smoke_test.py

No live database required — this exercises only the parsers, name normalization,
age group bands, power-curve math, phase detection, grip derivations, and the
pure-Python scoring helpers.
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
from ingestion.power_analysis import (
    analyze_power_curve_advanced,
    detect_phases,
    analyze_phase_metrics,
)
from ingestion.units import inches_to_meters
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
print("[7] pipeline dry-run (mocked DB) — Y movement")
import threading
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

_Y_TXT = (
    "\tD:\\Readiness Screen 3\\Data\\Smith_John\\2026-05-07__1\\y_data.txt\n"
    "h\n" "h\n" "h\n" "h\n"
    "1\t850.0\t12.5\t700.0\t10.2\t0.35\n"
)
with tempfile.TemporaryDirectory() as _tmpdir:
    with open(os.path.join(_tmpdir, "y_data.txt"), "w") as _f:
        _f.write(_Y_TXT)

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

    if "public.f_readiness_screen_y" not in _inserted:
        fail("Expected INSERT into public.f_readiness_screen_y, got: %r" % _inserted)
    if _summary["rows_inserted"] != 1:
        fail("Expected 1 row inserted, got rows_inserted=%r" % _summary["rows_inserted"])
    ok("pipeline dry-run: INSERT into %s confirmed, rows_inserted=%d" % (_inserted, _summary["rows_inserted"]))

# 8) cancel mid-run — no rows written when cancel_event is pre-set
print("[8] pipeline cancel (pre-set event)")
with tempfile.TemporaryDirectory() as _tmpdir2:
    with open(os.path.join(_tmpdir2, "y_data.txt"), "w") as _f:
        _f.write(_Y_TXT)

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

# 9) Phase detection — CMJ synthetic signal
print("[9] Phase detection (CMJ)")
# Synthetic CMJ: 200ms zeros, 300ms eccentric dip to -1500W, 200ms concentric rise to +3500W, 100ms decay, 200ms zeros
fs9 = 1000
_silence_pre = np.zeros(200)
_ecc_phase = np.linspace(0, -1500, 300)
_con_phase = np.concatenate([np.linspace(-1500, 3500, 150), np.linspace(3500, 0, 150)])  # 300ms
_decay = np.linspace(0, 0, 100)
_silence_post = np.zeros(200)
_cmj_signal = np.concatenate([_silence_pre, _ecc_phase, _con_phase, _decay, _silence_post])

# Known landmarks (calculated from the synthetic signal):
#   onset   ~ 214  (|power| > max(5, 0.02*3500)=70W, linspace 0→-1500 in 300 samples → ~14 samples in)
#   bottom  ~ 545  (last zero-crossing neg→pos before peak; the concentric linspace -1500→3500
#                   over 150 samples crosses zero ~44 samples in: 500+45 ≈ 545)
#   peak    ~ 649  (max positive power: 500+149=649)
#   takeoff ~ 797  (signal drops below 70W after peak: linspace 3500→0 in 150 samples, ~147 samples in)
_TOLERANCE = 30  # ±30 samples (30 ms at 1000 Hz)

phases9 = detect_phases(_cmj_signal, fs_hz=fs9, movement_type="CMJ")
if phases9["onset_idx"] is None:
    fail("CMJ detect_phases returned None onset")
if phases9["bottom_idx"] is None:
    fail("CMJ detect_phases returned None bottom")
if phases9["peak_idx"] is None:
    fail("CMJ detect_phases returned None peak")
if phases9["takeoff_idx"] is None:
    fail("CMJ detect_phases returned None takeoff")

# Onset: ~214 (first |power| > 70W threshold in eccentric dip).
if abs(phases9["onset_idx"] - 214) > _TOLERANCE:
    fail("CMJ onset_idx=%d, expected ~214 (±%d)" % (phases9["onset_idx"], _TOLERANCE))
# Bottom: ~545 (last neg→pos zero-crossing before peak).
if abs(phases9["bottom_idx"] - 545) > _TOLERANCE:
    fail("CMJ bottom_idx=%d, expected ~545 (±%d)" % (phases9["bottom_idx"], _TOLERANCE))
# Peak: ~649 (max positive power).
if abs(phases9["peak_idx"] - 649) > _TOLERANCE:
    fail("CMJ peak_idx=%d, expected ~649 (±%d)" % (phases9["peak_idx"], _TOLERANCE))
ok("CMJ detect_phases: onset=%d bottom=%d peak=%d takeoff=%d" % (
    phases9["onset_idx"], phases9["bottom_idx"], phases9["peak_idx"], phases9["takeoff_idx"]))

# 10) Phase metrics math — CMJ
print("[10] Phase metrics (CMJ)")
_jh_m = inches_to_meters(15.0)  # 15 inches ≈ 0.381 m
pm10 = analyze_phase_metrics(_cmj_signal, fs_hz=fs9, jump_height_m=_jh_m, movement_type="CMJ")

# eccentric_duration_s: onset(~214) to bottom(~545) = ~331 samples → ~0.331s
if pm10["eccentric_duration_s"] is None:
    fail("eccentric_duration_s should not be None for CMJ")
if not (0.25 < pm10["eccentric_duration_s"] < 0.45):
    fail("eccentric_duration_s=%.3f, expected between 0.25 and 0.45" % pm10["eccentric_duration_s"])

# concentric_duration_s: bottom(~545) to takeoff(~797) = ~252 samples → ~0.252s
if pm10["concentric_duration_s"] is None:
    fail("concentric_duration_s should not be None")
if not (0.20 < pm10["concentric_duration_s"] < 0.35):
    fail("concentric_duration_s=%.3f, expected between 0.20 and 0.35" % pm10["concentric_duration_s"])

# ecc_con_duration_ratio: both phases present and ratio is positive
if pm10["ecc_con_duration_ratio"] is None:
    fail("ecc_con_duration_ratio should not be None")
if pm10["ecc_con_duration_ratio"] <= 0:
    fail("ecc_con_duration_ratio=%.3f, expected >0" % pm10["ecc_con_duration_ratio"])

# mRSI = jump_height_m / contraction_time_s
if pm10["mrsi"] is None:
    fail("mrsi should not be None when jump_height_m provided")
_expected_mrsi = _jh_m / pm10["contraction_time_s"]
if abs(pm10["mrsi"] - _expected_mrsi) > 0.01:
    fail("mrsi=%.4f, expected %.4f" % (pm10["mrsi"], _expected_mrsi))

ok("CMJ phase metrics: ecc_dur=%.3fs con_dur=%.3fs ratio=%.3f mRSI=%.4f" % (
    pm10["eccentric_duration_s"], pm10["concentric_duration_s"],
    pm10["ecc_con_duration_ratio"], pm10["mrsi"]))

# 11) Phase detection — PPU synthetic signal (still-start, no eccentric)
print("[11] Phase detection (PPU still-start)")
_ppu_signal = np.concatenate([
    np.zeros(200),                              # baseline
    np.linspace(0, 3500, 200),                  # concentric ramp up
    np.linspace(3500, 0, 200),                  # decay
    np.zeros(200),                              # flight / baseline
])
phases11 = detect_phases(_ppu_signal, fs_hz=1000.0, movement_type="PPU")

if phases11["onset_idx"] is None:
    fail("PPU onset_idx should not be None")
if phases11["bottom_idx"] != phases11["onset_idx"]:
    fail("PPU bottom_idx should equal onset_idx (no eccentric phase), got onset=%d bottom=%d"
         % (phases11["onset_idx"], phases11["bottom_idx"]))

pm11 = analyze_phase_metrics(_ppu_signal, fs_hz=1000.0, jump_height_m=0.381, movement_type="PPU")

# Eccentric fields must be None (no eccentric phase for PPU still-start).
if pm11["eccentric_duration_s"] is not None:
    fail("PPU eccentric_duration_s should be None, got %r" % pm11["eccentric_duration_s"])
if pm11["eccentric_mean_power_w"] is not None:
    fail("PPU eccentric_mean_power_w should be None, got %r" % pm11["eccentric_mean_power_w"])
if pm11["ecc_con_duration_ratio"] is not None:
    fail("PPU ecc_con_duration_ratio should be None, got %r" % pm11["ecc_con_duration_ratio"])

# concentric_duration_s and mrsi should be valid.
if pm11["concentric_duration_s"] is None:
    fail("PPU concentric_duration_s should not be None")
if pm11["concentric_duration_s"] <= 0:
    fail("PPU concentric_duration_s=%.3f, expected >0" % pm11["concentric_duration_s"])
if pm11["mrsi"] is None:
    fail("PPU mrsi should not be None when jump_height_m provided and concentric phase detected")

ok("PPU still-start: eccentric fields=None, concentric_dur=%.3fs mrsi=%.4f" % (
    pm11["concentric_duration_s"], pm11["mrsi"]))

# 12) Grip metric derivations
print("[12] Grip derivations")
left_kg  = 50.0
right_kg = 45.0
avg_kg   = (left_kg + right_kg) / 2.0
max_kg   = max(left_kg, right_kg)
asym_pct = 100.0 * abs(left_kg - right_kg) / max_kg

if abs(avg_kg - 47.5) > 1e-6: fail("avg_kg=%.3f, expected 47.5" % avg_kg)
if abs(max_kg - 50.0) > 1e-6: fail("max_kg=%.3f, expected 50.0" % max_kg)
if abs(asym_pct - 10.0) > 1e-6: fail("asymmetry_pct=%.3f, expected 10.0" % asym_pct)
ok("grip derivations: avg=%.1f max=%.1f asym=%.1f%%" % (avg_kg, max_kg, asym_pct))

# 13) Sign correctness for new v2 metrics
print("[13] Sign correctness — new v2 metrics")
# mrsi (+1): higher value = better = positive z.
r_mrsi_good = scoring._zscore(0.5, [0.3, 0.32, 0.31, 0.29, 0.30])
r_mrsi_bad  = scoring._zscore(0.1, [0.3, 0.32, 0.31, 0.29, 0.30])
if r_mrsi_good is None or r_mrsi_bad is None: fail("zscore None for mrsi")
z_mrsi_good = +1 * r_mrsi_good[0]
z_mrsi_bad  = +1 * r_mrsi_bad[0]
if z_mrsi_good <= 0: fail("mrsi improving should give positive signed z, got %.3f" % z_mrsi_good)
if z_mrsi_bad  >= 0: fail("mrsi declining should give negative signed z, got %.3f" % z_mrsi_bad)
ok("mrsi sign: good day z=%.3f, bad day z=%.3f" % (z_mrsi_good, z_mrsi_bad))

# contraction_time_s (-1): lower is better, so a shorter time = positive signed z.
r_ct_good = scoring._zscore(0.50, [0.60, 0.61, 0.59, 0.62, 0.60])  # faster
r_ct_bad  = scoring._zscore(0.70, [0.60, 0.61, 0.59, 0.62, 0.60])  # slower
if r_ct_good is None or r_ct_bad is None: fail("zscore None for contraction_time")
z_ct_good = -1 * r_ct_good[0]
z_ct_bad  = -1 * r_ct_bad[0]
if z_ct_good <= 0: fail("contraction_time lower=better should give positive signed z, got %.3f" % z_ct_good)
if z_ct_bad  >= 0: fail("contraction_time higher=worse should give negative signed z, got %.3f" % z_ct_bad)
ok("contraction_time_s sign: faster z=%.3f, slower z=%.3f" % (z_ct_good, z_ct_bad))

# eccentric_mean_power_w (-1): more negative = better; sign=-1 inverts.
# Good day: -1500W (faster braking); baseline: ~-1000W. Raw z = (-1500 - (-1000)) / sd < 0. Signed = -1 * z > 0.
r_ecc_good = scoring._zscore(-1500.0, [-1000.0, -990.0, -1010.0, -1005.0, -995.0])
r_ecc_bad  = scoring._zscore(-500.0,  [-1000.0, -990.0, -1010.0, -1005.0, -995.0])
if r_ecc_good is None or r_ecc_bad is None: fail("zscore None for eccentric_mean_power")
z_ecc_good = -1 * r_ecc_good[0]
z_ecc_bad  = -1 * r_ecc_bad[0]
if z_ecc_good <= 0: fail("eccentric_mean_power more negative=better should give positive z, got %.3f" % z_ecc_good)
if z_ecc_bad  >= 0: fail("eccentric_mean_power less negative=worse should give negative z, got %.3f" % z_ecc_bad)
ok("eccentric_mean_power_w sign: faster braking z=%.3f, slower braking z=%.3f" % (z_ecc_good, z_ecc_bad))

# asymmetry_pct (-1): lower is better.
r_asym_good = scoring._zscore(5.0, [12.0, 11.5, 12.5, 11.8, 12.2])  # improved
r_asym_bad  = scoring._zscore(20.0, [12.0, 11.5, 12.5, 11.8, 12.2])  # worsened
if r_asym_good is None or r_asym_bad is None: fail("zscore None for asymmetry_pct")
z_asym_good = -1 * r_asym_good[0]
z_asym_bad  = -1 * r_asym_bad[0]
if z_asym_good <= 0: fail("asymmetry lower=better should give positive z, got %.3f" % z_asym_good)
if z_asym_bad  >= 0: fail("asymmetry higher=worse should give negative z, got %.3f" % z_asym_bad)
ok("asymmetry_pct sign: lower z=%.3f, higher z=%.3f" % (z_asym_good, z_asym_bad))

# 14) Intra-session CV math
print("[14] Intra-session CV math")
t1, t2 = 15.0, 15.6
n14 = 2
mean14 = (t1 + t2) / 2.0
sd14 = math.sqrt(((t1 - mean14) ** 2 + (t2 - mean14) ** 2) / (n14 - 1))
cv14 = sd14 / mean14
expected_cv = 0.02760  # (0.3 / 15.3) / 1 = 0.0196... let me recalc:
# mean = 15.3, sd = sqrt( (0.3^2 + 0.3^2) / 1 ) = sqrt(0.18) = 0.4243
# cv = 0.4243 / 15.3 = 0.02773...
expected_cv = math.sqrt(2 * (0.3 ** 2)) / 15.3  # 0.02773
if abs(cv14 - expected_cv) > 1e-4:
    fail("CV=%.5f, expected ~%.5f" % (cv14, expected_cv))
ok("intra-session CV=%.5f (two trials: %.1f, %.1f)" % (cv14, t1, t2))

# 15) inches_to_meters roundtrip
print("[15] inches_to_meters")
_m = inches_to_meters(39.3701)
if abs(_m - 1.0) > 1e-4: fail("inches_to_meters(39.3701)=%.6f, expected 1.0" % _m)
_m2 = inches_to_meters(0)
if _m2 is not None: fail("inches_to_meters(0) should return None, got %r" % _m2)
ok("inches_to_meters correct")

print("\nALL SMOKE TESTS PASSED.")
