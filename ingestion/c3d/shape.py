"""
Curve-shape metrics shared by the kurtosis study (jump power curves and
pitching lead-leg force curves).

Two different things get called "kurtosis" for a time series:

  value_kurtosis   scipy.stats.kurtosis(samples) - treats every sample as a
                   draw from a bag and ignores WHEN it happened. This is what
                   power_analysis.py stores in f_athletic_screen_*.kurtosis.
                   It mostly measures "how many samples are near zero vs. how
                   many are near the peak", so it moves with the export window
                   (quiet stance before, flight after) as much as with shape.

  time_kurtosis    treats the curve as a distribution of force/power OVER
                   TIME (weights = the curve, x = time). High = the output is
                   concentrated in a narrow spike around its centre; low/negative
                   = spread out, plateau or two-humped. This is the
                   "shape of the force curve" meaning, and it does not care how
                   much quiet signal surrounds the window.

Both use Fisher (excess) convention: a normal bell = 0.
"""
import numpy as np
from scipy import stats
from scipy.signal import find_peaks


def _nan_dict(prefix):
    keys = ["n", "dur_s", "peak", "mean", "crest", "t_peak_norm", "impulse",
            "rise_10_90_s", "rise_slope", "fwhm_s", "fwhm_norm",
            "value_skew", "value_kurtosis", "time_com_norm", "time_sd_norm",
            "time_skew", "time_kurtosis", "n_peaks", "max_rate", "t_max_rate_norm"]
    return {f"{prefix}{k}": np.nan for k in keys}


def curve_shape(y, fs_hz, a=0, b=None, prefix=""):
    """Shape metrics of y[a:b] (inclusive b). Returns dict with prefixed keys."""
    y = np.asarray(y, dtype=float)
    b = len(y) - 1 if b is None else int(b)
    a = int(max(0, a))
    out = _nan_dict(prefix)
    if b - a < 5:
        return out
    seg = y[a:b + 1]
    seg = np.where(np.isfinite(seg), seg, 0.0)
    n = seg.size
    dt = 1.0 / fs_hz
    dur = (n - 1) * dt
    pk_i = int(np.argmax(seg))
    pk = float(seg[pk_i])
    out[f"{prefix}n"] = n
    out[f"{prefix}dur_s"] = dur
    out[f"{prefix}peak"] = pk
    out[f"{prefix}mean"] = float(np.mean(seg))
    out[f"{prefix}crest"] = pk / np.mean(seg) if np.mean(seg) > 0 else np.nan
    out[f"{prefix}t_peak_norm"] = pk_i / (n - 1)
    out[f"{prefix}impulse"] = float(np.trapezoid(seg, dx=dt))

    # rise 10-90 on the rising limb
    rising = seg[:pk_i + 1]
    if pk > 0 and rising.size > 1:
        i10 = int(np.argmax(rising >= 0.1 * pk))
        i90 = int(np.argmax(rising >= 0.9 * pk))
        if i90 > i10:
            rt = (i90 - i10) * dt
            out[f"{prefix}rise_10_90_s"] = rt
            out[f"{prefix}rise_slope"] = 0.8 * pk / rt
        l50 = int(np.argmax(rising >= 0.5 * pk))
        fall = seg[pk_i:]
        r50 = pk_i + (int(np.argmax(fall <= 0.5 * pk)) if np.any(fall <= 0.5 * pk) else fall.size - 1)
        if r50 > l50:
            out[f"{prefix}fwhm_s"] = (r50 - l50) * dt
            out[f"{prefix}fwhm_norm"] = (r50 - l50) / (n - 1)

    out[f"{prefix}value_skew"] = float(stats.skew(seg))
    out[f"{prefix}value_kurtosis"] = float(stats.kurtosis(seg, fisher=True))

    # time-domain moments, curve as weights (negative parts clipped)
    w = np.clip(seg, 0, None)
    if w.sum() > 0:
        t = np.arange(n) / (n - 1)          # normalised 0..1 so values compare across durations
        mu = np.sum(w * t) / w.sum()
        var = np.sum(w * (t - mu) ** 2) / w.sum()
        sd = np.sqrt(var)
        out[f"{prefix}time_com_norm"] = mu
        out[f"{prefix}time_sd_norm"] = sd
        if sd > 0:
            out[f"{prefix}time_skew"] = np.sum(w * (t - mu) ** 3) / w.sum() / sd ** 3
            out[f"{prefix}time_kurtosis"] = np.sum(w * (t - mu) ** 4) / w.sum() / sd ** 4 - 3.0

    if pk > 0:
        peaks, _ = find_peaks(seg, height=0.5 * pk, distance=max(1, int(0.03 * fs_hz)),
                              prominence=0.1 * pk)
        out[f"{prefix}n_peaks"] = int(len(peaks))
    d = np.gradient(seg, dt)
    out[f"{prefix}max_rate"] = float(np.max(d))
    out[f"{prefix}t_max_rate_norm"] = int(np.argmax(d)) / (n - 1)
    return out


def active_window(y, frac=0.10):
    """Onset/offset around the main peak where y crosses frac*peak (power curves)."""
    y = np.asarray(y, dtype=float)
    pk = int(np.nanargmax(y)); thr = frac * y[pk]
    pre = np.where(y[:pk] < thr)[0]
    a = int(pre[-1]) + 1 if pre.size else 0
    post = np.where(y[pk:] < thr)[0]
    b = pk + int(post[0]) - 1 if post.size else len(y) - 1
    return a, max(b, pk)
