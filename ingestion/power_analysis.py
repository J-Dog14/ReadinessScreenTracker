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
from typing import Dict, List, Optional, Union

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
    auc = float(np.trapz(np.nan_to_num(p[a : b + 1], nan=0.0), dx=1.0 / fs_hz))

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


def detect_phases(
    power_array: Union[np.ndarray, list],
    fs_hz: float = 1000.0,
    movement_type: str = "CMJ",
) -> Dict:
    """Detect onset, eccentric-concentric split, peak, and takeoff indices.

    For CMJ: onset is where |power| first exceeds the threshold; bottom is the
    last zero-crossing (negative→positive) before the global peak, marking the
    eccentric→concentric transition; takeoff is the first near-zero sample after peak.

    For PPU (still-start, no eccentric phase): onset is where positive power
    first exceeds the threshold; bottom is set equal to onset (no eccentric);
    peak and takeoff are detected normally.

    Returns {onset_idx, bottom_idx, peak_idx, takeoff_idx}. All indices may be
    None if the signal is degenerate.
    """
    p = np.asarray(power_array, dtype=float)
    n = p.size
    if n < 10:
        return {"onset_idx": None, "bottom_idx": None, "peak_idx": None, "takeoff_idx": None}

    try:
        peak_abs = float(np.nanmax(np.abs(p)))
        if peak_abs < 1e-6:
            return {"onset_idx": None, "bottom_idx": None, "peak_idx": None, "takeoff_idx": None}

        onset_threshold = max(5.0, 0.02 * peak_abs)

        if movement_type == "PPU":
            # Onset: first sample where positive power exceeds threshold
            pos_mask = p > onset_threshold
            onset_idx = int(np.argmax(pos_mask)) if np.any(pos_mask) else None
            if onset_idx is None:
                return {"onset_idx": None, "bottom_idx": None, "peak_idx": None, "takeoff_idx": None}
            bottom_idx = onset_idx  # no eccentric phase
        else:
            # CMJ: onset uses absolute power
            abs_mask = np.abs(p) > onset_threshold
            onset_idx = int(np.argmax(abs_mask)) if np.any(abs_mask) else None
            if onset_idx is None:
                return {"onset_idx": None, "bottom_idx": None, "peak_idx": None, "takeoff_idx": None}

            # Peak (positive)
            pk_idx = int(np.nanargmax(p))

            # Bottom: last zero-crossing (negative→positive) before peak.
            # Scan backwards from peak for the last index where p transitions
            # from negative to positive (sign change: p[i] < 0 and p[i+1] >= 0).
            bottom_idx = onset_idx  # fallback if no zero-crossing found
            for i in range(pk_idx - 1, onset_idx, -1):
                if p[i] < 0 and p[i + 1] >= 0:
                    bottom_idx = i + 1
                    break

        peak_idx = int(np.nanargmax(p))

        # Takeoff: first sample after peak where |power| drops below onset_threshold
        post_peak = p[peak_idx:]
        near_zero = np.abs(post_peak) < onset_threshold
        if np.any(near_zero):
            takeoff_rel = int(np.argmax(near_zero))
            takeoff_idx = peak_idx + takeoff_rel
        else:
            takeoff_idx = n - 1

        return {
            "onset_idx":   onset_idx,
            "bottom_idx":  bottom_idx,
            "peak_idx":    peak_idx,
            "takeoff_idx": takeoff_idx,
        }
    except Exception:
        return {"onset_idx": None, "bottom_idx": None, "peak_idx": None, "takeoff_idx": None}


def analyze_phase_metrics(
    power_array: Union[np.ndarray, list],
    fs_hz: float = 1000.0,
    jump_height_m: Optional[float] = None,
    movement_type: str = "CMJ",
) -> Dict:
    """Compute phase-level metrics from a power-time signal.

    Returns a 9-key dict. For PPU (still-start protocol) all eccentric fields
    are None because bottom_idx == onset_idx. All fields return None gracefully
    on degenerate signals.
    """
    null_result = {
        "contraction_time_s":     None,
        "eccentric_duration_s":   None,
        "concentric_duration_s":  None,
        "ecc_con_duration_ratio": None,
        "eccentric_mean_power_w": None,
        "eccentric_peak_power_w": None,
        "eccentric_auc_j":        None,
        "concentric_auc_j":       None,
        "mrsi":                   None,
    }

    try:
        phases = detect_phases(power_array, fs_hz, movement_type)
        onset_idx   = phases["onset_idx"]
        bottom_idx  = phases["bottom_idx"]
        peak_idx    = phases["peak_idx"]
        takeoff_idx = phases["takeoff_idx"]

        if any(v is None for v in (onset_idx, bottom_idx, peak_idx, takeoff_idx)):
            return null_result

        p = np.asarray(power_array, dtype=float)

        contraction_time_s = (takeoff_idx - onset_idx) / fs_hz
        concentric_duration_s = (takeoff_idx - bottom_idx) / fs_hz

        if concentric_duration_s <= 0:
            return null_result

        # Eccentric phase exists only when bottom_idx > onset_idx + a few samples
        has_eccentric = bottom_idx > onset_idx + 5
        if has_eccentric:
            eccentric_duration_s   = (bottom_idx - onset_idx) / fs_hz
            ecc_seg = p[onset_idx:bottom_idx]
            eccentric_mean_power_w = float(np.mean(ecc_seg))
            eccentric_peak_power_w = float(np.min(ecc_seg))  # most negative
            eccentric_auc_j = float(abs(np.trapz(ecc_seg, dx=1.0 / fs_hz)))
            ecc_con_duration_ratio = eccentric_duration_s / concentric_duration_s
        else:
            eccentric_duration_s   = None
            eccentric_mean_power_w = None
            eccentric_peak_power_w = None
            eccentric_auc_j        = None
            ecc_con_duration_ratio = None

        con_seg = p[bottom_idx:takeoff_idx]
        concentric_auc_j = float(np.trapz(con_seg, dx=1.0 / fs_hz))

        if jump_height_m is not None and contraction_time_s > 0:
            mrsi = jump_height_m / contraction_time_s
        else:
            mrsi = None

        return {
            "contraction_time_s":     contraction_time_s,
            "eccentric_duration_s":   eccentric_duration_s,
            "concentric_duration_s":  concentric_duration_s,
            "ecc_con_duration_ratio": ecc_con_duration_ratio,
            "eccentric_mean_power_w": eccentric_mean_power_w,
            "eccentric_peak_power_w": eccentric_peak_power_w,
            "eccentric_auc_j":        eccentric_auc_j,
            "concentric_auc_j":       concentric_auc_j,
            "mrsi":                   mrsi,
        }
    except Exception:
        return null_result


def analyze_phase_metrics_from_force(
    force_array: Union[np.ndarray, list],
    fs_hz: float = 1000.0,
    jump_height_m: Optional[float] = None,
    movement_type: str = "CMJ",
    body_weight_n: Optional[float] = None,
) -> Dict:
    """Compute phase + force metrics from a full-trial GRF time-series (Newtons).

    body_weight_n: if provided, use it directly instead of estimating from quiet standing.
                   Required for PPU (athlete is not standing on plate during quiet phase).
    COM velocity is derived by integrating (GRF - BW) / mass.
    Signed power = GRF x v.  Eccentric phase detected via velocity zero-crossing.
    PPU (still-start) produces None for all eccentric fields.
    Returns 13 keys: 9 PHASE_COLS + 4 FORCE_COLS.
    """
    null_result = {
        "contraction_time_s":     None, "eccentric_duration_s":   None,
        "concentric_duration_s":  None, "ecc_con_duration_ratio": None,
        "eccentric_mean_power_w": None, "eccentric_peak_power_w": None,
        "eccentric_auc_j":        None, "concentric_auc_j":       None,
        "mrsi":                   None,
        "peak_grf_n": None, "peak_grf_bw_ratio": None,
        "rfd_0_100ms": None, "concentric_impulse_ns": None,
    }
    try:
        F = np.asarray(force_array, dtype=float)
        n = len(F)
        if n < 50:
            return null_result

        g = 9.81
        quiet_n = max(10, int(0.05 * fs_hz))

        # Signal resting force: always estimated from the first 50 ms of THIS file.
        # CMJ: plate has full body weight → signal_rest ≈ BW.
        # PPU: plate has upper body only → signal_rest ≈ upper-body weight.
        signal_rest_n = float(np.mean(F[:quiet_n]))
        if signal_rest_n < 10.0:
            return null_result

        # Mass always from signal resting force — correct for each movement type.
        # CMJ: signal_rest ≈ full BW → full body mass.
        # PPU: signal_rest ≈ upper body weight → upper body mass (correct for PPU dynamics).
        mass_kg = signal_rest_n / g

        # For peak_grf_bw_ratio, use full body weight if provided so the ratio is
        # comparable across CMJ and PPU.
        if body_weight_n is not None and body_weight_n > 10.0:
            bw_for_ratio = float(body_weight_n)
        else:
            bw_for_ratio = signal_rest_n

        # Velocity integrates against signal resting force — correct for both CMJ and PPU.
        v = np.cumsum(F - signal_rest_n) / (mass_kg * fs_hz)
        P = F * v

        # Same velocity-based detection for both CMJ and PPU.
        # PPU has a shallower eccentric dip than CMJ, so threshold is -0.02 m/s
        # (PPU with upper-body mass reaches ~-0.05 m/s; CMJ reaches ~-0.9 m/s).
        neg_mask = v < -0.02
        if not np.any(neg_mask):
            return null_result
        ecc_start_idx = int(np.argmax(neg_mask))

        # Bottom: first neg→pos velocity zero-crossing = eccentric→concentric transition
        pk_power_idx = int(np.argmax(P))
        bottom_idx = ecc_start_idx
        for i in range(ecc_start_idx, min(pk_power_idx, n - 1)):
            if v[i] <= 0 and v[i + 1] > 0:
                bottom_idx = i + 1
                break

        if bottom_idx <= ecc_start_idx + 5:
            return null_result

        # Takeoff: GRF drops below 5 % of signal resting force after peak power
        pk_power_idx = int(np.argmax(P))
        takeoff_idx = n - 1
        for i in range(pk_power_idx, n - 1):
            if F[i] < 0.05 * signal_rest_n:
                takeoff_idx = i
                break

        contraction_time_s = (takeoff_idx - ecc_start_idx) / fs_hz
        concentric_duration_s = (takeoff_idx - bottom_idx) / fs_hz
        if concentric_duration_s <= 0:
            return null_result

        if bottom_idx > ecc_start_idx + 5:
            eccentric_duration_s   = (bottom_idx - ecc_start_idx) / fs_hz
            ecc_seg                = P[ecc_start_idx:bottom_idx]
            eccentric_mean_power_w = float(np.mean(ecc_seg))
            eccentric_peak_power_w = float(np.min(ecc_seg))
            eccentric_auc_j        = float(abs(np.trapz(ecc_seg, dx=1.0 / fs_hz)))
            ecc_con_duration_ratio = eccentric_duration_s / concentric_duration_s
        else:
            eccentric_duration_s = eccentric_mean_power_w = None
            eccentric_peak_power_w = eccentric_auc_j = ecc_con_duration_ratio = None

        con_seg = P[bottom_idx:takeoff_idx]
        concentric_auc_j = float(np.trapz(con_seg, dx=1.0 / fs_hz))

        mrsi = (jump_height_m / contraction_time_s) if (
            jump_height_m and contraction_time_s > 0) else None

        # Force-specific metrics
        peak_grf_n = float(np.max(F))
        # Ratio uses full body weight (bw_for_ratio) so it's comparable across movements
        peak_grf_bw_ratio = (peak_grf_n / bw_for_ratio) if bw_for_ratio > 0 else None

        # RFD: slope of GRF from concentric onset (bottom_idx) over first 100 ms
        rfd_end = min(bottom_idx + max(1, int(0.1 * fs_hz)), n - 1)
        rfd_0_100ms = float((F[rfd_end] - F[bottom_idx]) / 0.1) if rfd_end > bottom_idx else None

        # Net concentric impulse above signal resting force (N·s)
        # Uses signal_rest_n so PPU impulse is measured above upper-body weight baseline
        con_force_seg = F[bottom_idx:takeoff_idx] - signal_rest_n
        concentric_impulse_ns = float(np.trapz(con_force_seg, dx=1.0 / fs_hz))

        return {
            "contraction_time_s":     contraction_time_s,
            "eccentric_duration_s":   eccentric_duration_s,
            "concentric_duration_s":  concentric_duration_s,
            "ecc_con_duration_ratio": ecc_con_duration_ratio,
            "eccentric_mean_power_w": eccentric_mean_power_w,
            "eccentric_peak_power_w": eccentric_peak_power_w,
            "eccentric_auc_j":        eccentric_auc_j,
            "concentric_auc_j":       concentric_auc_j,
            "mrsi":                   mrsi,
            "peak_grf_n":             peak_grf_n,
            "peak_grf_bw_ratio":      peak_grf_bw_ratio,
            "rfd_0_100ms":            rfd_0_100ms,
            "concentric_impulse_ns":  concentric_impulse_ns,
        }
    except Exception:
        return null_result


def analyze_power_curve_advanced(
    power: Union[np.ndarray, list],
    fs_hz: float = 1000.0,
    jump_height_m: Optional[float] = None,
    movement_type: str = "CMJ",
) -> dict:
    """Adds RPD, work distribution, decay, shape stats, spectral centroid, and phase metrics."""
    base = analyze_power_curve(power, fs_hz)
    p = np.asarray(power, dtype=float)

    dp = np.gradient(p, 1.0 / fs_hz)
    base["rpd_max_w_per_s"] = float(np.nanmax(dp))
    base["time_to_rpd_max_s"] = float(np.nanargmax(dp) / fs_hz)

    a, b, pk = base["onset_idx"], base["offset_idx"], base["peak_idx"]
    auc_pre = float(np.trapz(np.nan_to_num(p[a : pk + 1], nan=0.0), dx=1.0 / fs_hz)) if pk >= a else np.nan
    auc_post = float(np.trapz(np.nan_to_num(p[pk : b + 1], nan=0.0), dx=1.0 / fs_hz)) if b >= pk else np.nan
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

    phase = analyze_phase_metrics(power, fs_hz, jump_height_m=jump_height_m, movement_type=movement_type)
    base.update(phase)

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
    mvt_upper = movement.upper()
    for path in find_power_files(power_dir, movement):
        try:
            arr = load_power_txt(path)
            metrics = analyze_power_curve_advanced(arr, fs_hz=fs_hz, movement_type=mvt_upper)
            metrics["source_file"] = path
            results.append(metrics)
        except Exception as e:  # bad/short file — skip but keep going
            results.append({"source_file": path, "error": str(e)})
    return results
