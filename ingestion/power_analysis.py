"""
Power-time curve analysis. Faithful port of backend's
uais/python/athleticScreen/power_analysis.py — exact same metric definitions
so the values produced here match what the backend's athletic screen produces
for CMJ/PPU power signals.

Inputs are *_Power.txt files (tab-separated, first column = sample index, second
column = instantaneous power in watts). Output is a dict of curve-shape metrics
that gets persisted to f_readiness_screen_power_curve.

Metrics (and their research basis):
  peak_power_w       — gold-standard explosive output (W)
  rise_time_10_90_s  — proxy for rate-of-force-development (RFD)
  rise_slope_w_per_s — = 0.8 * peak / rise_time, slope of the explosive phase
  fwhm_s             — power impulse duration (full-width-half-max)
  auc_j              — total work done across the contraction (∫P dt → joules)
  rpd_max_w_per_s    — peak rate of power development (max dP/dt)
  decay_90_10_s      — falling-limb time, sensitive to fatigue
  work_early_pct     — fraction of work generated BEFORE peak (concentric bias)
  skewness/kurtosis  — curve-shape statistics (asymmetry, tailedness)
  spectral_centroid  — frequency-weighted curve roughness
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Union

import numpy as np
from scipy import stats


def load_power_txt(txt_path: str) -> np.ndarray:
    """Read column 2 (power, W) from a Power.txt file."""
    vals: List[float] = []
    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        in_numeric = False
        for line in f:
            line = line.strip()
            if not line:
                continue
            if not in_numeric and re.match(r"^\d+\s+", line):
                in_numeric = True
            if in_numeric and re.match(r"^\d+\s+", line):
                parts = re.split(r"\s+", line)
                if len(parts) >= 2:
                    try:
                        vals.append(float(parts[1]))
                    except ValueError:
                        pass
    if not vals:
        raise ValueError(f"No numeric power values in {txt_path}")
    return np.asarray(vals, dtype=float)


def analyze_power_curve(power: Union[np.ndarray, list], fs_hz: float = 1000.0) -> dict:
    """Base curve-shape metrics. See module docstring."""
    p = np.asarray(power, dtype=float)
    n = p.size
    t = np.arange(n) / fs_hz

    pk_idx = int(np.nanargmax(p))
    pk_val = float(p[pk_idx])

    thr10 = 0.10 * pk_val
    thr50 = 0.50 * pk_val
    thr90 = 0.90 * pk_val

    onset_idx = int(np.argmax(p >= thr10)) if np.any(p >= thr10) else 0
    post = p[pk_idx:]
    off_rel = int(np.argmax(post < thr10)) if np.any(post < thr10) else (post.size - 1)
    offset_idx = pk_idx + off_rel

    rising = p[: pk_idx + 1]
    try:
        i10 = int(np.argmax(rising >= thr10))
        i90 = int(np.argmax(rising >= thr90))
        rise_time = (i90 - i10) / fs_hz if i90 > i10 else np.nan
        rise_slope = (0.8 * pk_val) / rise_time if rise_time and rise_time > 0 else np.nan
    except ValueError:
        i10 = i90 = None
        rise_time = np.nan
        rise_slope = np.nan

    try:
        left50 = int(np.argmax(rising >= thr50))
    except ValueError:
        left50 = pk_idx
    falling = p[pk_idx:]
    try:
        right_rel = int(np.argmax(falling <= thr50))
        right50 = pk_idx + right_rel
    except ValueError:
        right50 = pk_idx
    fwhm = (right50 - left50) / fs_hz if right50 > left50 else np.nan

    a = max(0, onset_idx)
    b = min(n - 1, max(offset_idx, pk_idx))
    auc = float(np.trapezoid(np.nan_to_num(p[a : b + 1], nan=0.0), dx=1.0 / fs_hz))

    weights = np.clip(p[a : b + 1], a_min=0, a_max=None)
    if np.sum(weights) > 0:
        t_window = t[a : b + 1]
        t_com = float(np.sum(t_window * weights) / np.sum(weights))
        t_com_norm = (t_com - t[a]) / max(1e-9, (t[b] - t[a]))
    else:
        t_com = np.nan
        t_com_norm = np.nan

    w = int(0.05 * fs_hz)
    lo = max(0, pk_idx - w)
    hi = min(n, pk_idx + w + 1)
    local = p[lo:hi]
    cv_local = float(np.std(local) / np.mean(local)) if np.mean(local) > 0 else np.nan

    return {
        "n_samples": n,
        "fs_hz": fs_hz,
        "peak_power_w": pk_val,
        "time_to_peak_s": float(t[pk_idx]),
        "rise_time_10_90_s": float(rise_time),
        "rise_slope_w_per_s": float(rise_slope),
        "fwhm_s": float(fwhm),
        "auc_j": auc,
        "onset_idx": a,
        "offset_idx": b,
        "peak_idx": pk_idx,
        "t_com_s": t_com,
        "t_com_norm_0to1": t_com_norm,
        "cv_local_peak": cv_local,
    }


def analyze_power_curve_advanced(power: Union[np.ndarray, list], fs_hz: float = 1000.0) -> dict:
    """Adds RPD, work distribution, decay, shape stats, and spectral centroid."""
    base = analyze_power_curve(power, fs_hz)
    p = np.asarray(power, dtype=float)

    dp = np.gradient(p, 1.0 / fs_hz)
    base["rpd_max_w_per_s"] = float(np.nanmax(dp))
    base["time_to_rpd_max_s"] = float(np.nanargmax(dp) / fs_hz)

    a, b, pk = base["onset_idx"], base["offset_idx"], base["peak_idx"]
    auc_pre = float(np.trapezoid(np.nan_to_num(p[a : pk + 1], nan=0.0), dx=1.0 / fs_hz)) if pk >= a else np.nan
    auc_post = float(np.trapezoid(np.nan_to_num(p[pk : b + 1], nan=0.0), dx=1.0 / fs_hz)) if b >= pk else np.nan
    total = (0 if not np.isfinite(auc_pre) else auc_pre) + (0 if not np.isfinite(auc_post) else auc_post)
    base["auc_pre_j"] = auc_pre
    base["auc_post_j"] = auc_post
    base["work_early_pct"] = float(100.0 * auc_pre / total) if total > 0 else np.nan

    fall = p[pk:]
    thr90 = 0.90 * p[pk]
    thr10 = 0.10 * p[pk]
    i90 = int(np.argmax(fall <= thr90)) if np.any(fall <= thr90) else 0
    i10 = int(np.argmax(fall <= thr10)) if np.any(fall <= thr10) else len(fall) - 1
    base["decay_90_10_s"] = (i10 - i90) / fs_hz if i10 > i90 else np.nan

    finite = np.isfinite(p)
    base["skewness"] = float(stats.skew(p[finite])) if np.any(finite) else np.nan
    base["kurtosis"] = float(stats.kurtosis(p[finite], fisher=True)) if np.any(finite) else np.nan

    x = p - np.nanmean(p)
    X = np.abs(np.fft.rfft(np.nan_to_num(x)))
    freqs = np.fft.rfftfreq(x.size, d=1.0 / fs_hz)
    base["spectral_centroid_hz"] = float(np.sum(freqs * X) / max(1e-12, np.sum(X)))

    return base


def find_power_files(power_dir: str, movement: str) -> List[str]:
    """
    Locate CMJ or PPU *_Power.txt files in a folder.
    Pattern: any file containing the movement token AND ending with Power.txt.
    Returns a sorted list of absolute paths (deterministic ordering for trial_id).
    """
    if not power_dir or not os.path.isdir(power_dir):
        return []
    movement_lower = movement.lower()
    out = []
    for fn in os.listdir(power_dir):
        full = os.path.join(power_dir, fn)
        if not os.path.isfile(full):
            continue
        low = fn.lower()
        if not low.endswith("power.txt"):
            continue
        if movement_lower in low:
            out.append(full)
    return sorted(out)


def analyze_session_power_files(
    power_dir: str,
    movement: str,
    fs_hz: float = 1000.0,
) -> List[dict]:
    """For every *_Power.txt belonging to `movement`, compute curve metrics. Skips files we can't read."""
    results = []
    for path in find_power_files(power_dir, movement):
        try:
            arr = load_power_txt(path)
            metrics = analyze_power_curve_advanced(arr, fs_hz=fs_hz)
            metrics["source_file"] = path
            results.append(metrics)
        except Exception as e:  # bad/short file — skip but keep going
            results.append({"source_file": path, "error": str(e)})
    return results
