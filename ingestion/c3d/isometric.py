"""
Isometric shoulder tests (I / Y / T / IR90) from FP3 vertical force.

The hand pushes down on FP3 for a few seconds. Visual3D (Readiness_Screen.v3s / V3.v3s) computed:
    Begin_Push   first sample >= 10 N on ascent (3-frame window)
    Max_Force    global maximum of raw FP3 Z
    Time_to_Max  Begin_Push -> Max_Force
    Avg_Force    mean raw FP3 Z from Begin_Push to the end of the file
    *_Norm       value / typed-in mass (kg)  -> N/kg
Those definitions were fine (no integration involved); the rebuilt values match the old
ones within ~1-2 % (V3D worked at the 300 Hz point rate). v2 keeps them, adds a plate
zero offset, a fixed 3 s averaging window that does not depend on how long the file
runs, per-second splits, RFD and impulse, and normalises by plate-measured body mass
when the session has a CMJ quiet window.
"""
import numpy as np
from scipy.signal import butter, filtfilt

PUSH_THR_N = 10.0          # same as the V3D Begin_Push threshold
PUSH_WINDOW = 3            # consecutive samples above threshold
AVG_WINDOW_S = 3.0
FILTER_HZ = 20.0           # only for RFD


class IsoError(Exception):
    pass


def _lowpass(x, fs, fc=FILTER_HZ):
    if len(x) < 30 or fc >= fs / 2:
        return x.copy()
    b, a = butter(4, fc / (fs / 2), btype="low")
    return filtfilt(b, a, x)


def _push_start(fz, thr=PUSH_THR_N, win=PUSH_WINDOW):
    above = fz >= thr
    if win > 1:
        run = np.convolve(above.astype(int), np.ones(win, int), mode="valid") == win
        idx = np.flatnonzero(run)
    else:
        idx = np.flatnonzero(above)
    return int(idx[0]) if idx.size else None


def plate_offset(fz, fs):
    """Plate reading before the push (median of the quiet lead-in), 0 if there is none."""
    i0 = _push_start(fz)
    if i0 is None:
        return 0.0, False
    lead = fz[:max(0, i0 - int(0.05 * fs))]
    if lead.size < int(0.1 * fs) or np.abs(lead).max() > 30:
        return 0.0, False
    return float(np.median(lead)), True


def process_isometric(fz_raw, fs, mass_kg=None):
    """Metrics for one isometric trial. Returns (metrics dict, corrected Fz array)."""
    fz_raw = np.asarray(fz_raw, float)
    qc = []
    n = len(fz_raw)
    if n < int(0.5 * fs):
        raise IsoError("trial shorter than 0.5 s")

    # ---- V3D-equivalent (raw signal) - kept so the rebuild can be checked against the old values ----
    b0 = _push_start(fz_raw)
    if b0 is None:
        raise IsoError(f"force never reached {PUSH_THR_N:.0f} N")
    imax_raw = int(np.argmax(fz_raw))
    out = dict(max_force_raw_n=float(fz_raw[imax_raw]), avg_force_raw_n=float(fz_raw[b0:].mean()),
               time_to_max_raw_s=(imax_raw - b0) / fs)

    # ---- offset-corrected ------------------------------------------------------------------------
    off, has_lead = plate_offset(fz_raw, fs)
    if not has_lead:
        qc.append("no_quiet_lead_in")
    if abs(off) > 10:
        qc.append("plate_offset_gt_10N")
    fz = fz_raw - off
    i0 = _push_start(fz)
    if i0 is None:
        raise IsoError("no push after offset correction")
    if i0 < int(0.05 * fs):
        qc.append("push_at_file_start")
    imax = i0 + int(np.argmax(fz[i0:]))
    after = (n - i0) / fs
    if after < AVG_WINDOW_S:
        qc.append(f"lt_{AVG_WINDOW_S:.0f}s_after_push")
    if imax >= n - int(0.05 * fs):
        qc.append("max_at_file_end")
    w3 = fz[i0:min(n, i0 + int(AVG_WINDOW_S * fs))]
    pk = float(fz[imax])
    out.update(
        fs_hz=fs, plate_offset_n=off, push_start_s=i0 / fs, push_duration_s=after,
        max_force_n=pk, avg_force_n=float(fz[i0:].mean()), time_to_max_s=(imax - i0) / fs,
        avg_force_3s_n=float(w3.mean()), impulse_3s_ns=float(w3.sum() / fs),
    )
    for k in range(3):
        seg = fz[i0 + int(k * fs):i0 + int((k + 1) * fs)]
        out[f"avg_force_s{k + 1}_n"] = float(seg.mean()) if seg.size >= int(0.5 * fs) else np.nan
        out[f"pct_max_s{k + 1}"] = 100 * out[f"avg_force_s{k + 1}_n"] / pk if pk > 0 else np.nan
    # end-of-hold fatigue: last full second vs first full second of the 3 s window
    if np.isfinite(out["avg_force_s1_n"]) and np.isfinite(out["avg_force_s3_n"]) and out["avg_force_s1_n"] > 0:
        out["hold_drop_pct"] = 100 * (1 - out["avg_force_s3_n"] / out["avg_force_s1_n"])

    f_f = _lowpass(fz, fs)
    for ms in (100, 200):
        j = i0 + int(ms / 1000 * fs)
        out[f"rfd_0_{ms}ms_n_per_s"] = float((f_f[j] - f_f[i0]) / (ms / 1000)) if j < n else np.nan
    rfd = np.gradient(f_f, 1.0 / fs)
    ipk = i0 + int(np.argmax(rfd[i0:imax + 1])) if imax > i0 else i0
    out.update(peak_rfd_n_per_s=float(rfd[ipk]), time_to_peak_rfd_s=(ipk - i0) / fs)

    if mass_kg:
        out["mass_kg"] = float(mass_kg)
        for k in ("max_force_n", "avg_force_n", "avg_force_3s_n"):
            out[k.replace("_n", "_n_per_kg")] = out[k] / mass_kg
    out["qc_flags"] = qc
    out.update(push_start_idx=i0, max_idx=imax)
    return out, fz
