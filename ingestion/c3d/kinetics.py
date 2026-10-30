"""
Jump kinetics from vertical GRF, done the way that survives cropped trials.

Velocity is integrated BACKWARD from takeoff, where it is known from flight
time (v_to = g * t_flight / 2). Nothing depends on the file starting at rest,
so it works for cropped CMJ/SLV/PPU and for drop jumps (which always start at
contact). Body weight only matters across the contact window.

Body weight priority:
  1. this trial's own quiet window (steady stance before the movement)
  2. the session's quiet body weight (median of CMJ/SLV quiet windows)
  3. the mass typed into QTM (session.xml)
PPU uses its own quiet plank window, else the first 50 ms of contact
("effective mass" on the hands, same idea as the old V3D MASS_EFF).

Built-in QC
  v_quiet_resid_m_s  velocity over the quiet window - should be ~0. A large value
                     means body weight or flight time is off.
  implied_drop_cm    DJ only: drop height implied by the touchdown velocity.
"""
import numpy as np
from scipy.signal import butter, filtfilt

from .shape import curve_shape

G = 9.81
THR_N = 20.0          # same on/off threshold as the V3D pipeline
FILTER_HZ = 50.0      # only for derivatives (RFD / RPD)

QUIET_CV = {"CMJ": 0.015, "SLV": 0.025, "PPU": 0.02}


class TrialError(Exception):
    pass


def lowpass(x, fs, fc=FILTER_HZ):
    if len(x) < 30 or fc >= fs / 2:
        return x.copy()
    b, a = butter(4, fc / (fs / 2), btype="low")
    return filtfilt(b, a, x)


MIN_FLIGHT_S = {"PPU": 0.05}     # push-up flights can be very short
MAX_FLIGHT_S = 1.0               # ~48 in jump; longer = stepped off and back on (uncropped trial), not a jump


def unloaded_offset(fz, fs):
    """Plate reading when nobody is on it (median of clearly-unloaded samples)."""
    low = fz[np.abs(fz) < 100.0]
    return float(np.median(low)) if low.size >= 0.05 * fs else 0.0


def _unloaded_mask(fz, fs, thr=THR_N, max_blip_s=0.010, max_blip_n=150.0):
    """fz < thr, with short low-amplitude blips (noise, a brushed foot) filled in."""
    below = fz < thr
    n, i = len(fz), 0
    while i < n:
        if not below[i]:
            j = i
            while j < n and not below[j]:
                j += 1
            if 0 < i and j < n and (j - i) / fs <= max_blip_s and fz[i:j].max() < max_blip_n:
                below[i:j] = True
            i = j
        else:
            i += 1
    return below


def find_flight(fz, fs, movement=None, thr=THR_N):
    """Longest unloaded run (<= MAX_FLIGHT_S) that follows a real contact and ends in a landing.
    Works on offset-corrected force so a plate reading +20 N in the air still finds flight."""
    fzc = fz - unloaded_offset(fz, fs)
    below = _unloaded_mask(fzc, fs, thr)
    min_s = MIN_FLIGHT_S.get(movement, 0.12)
    n, best, i = len(fzc), None, 0
    while i < n:
        if below[i]:
            j = i
            while j < n and below[j]:
                j += 1
            loaded_before = np.sum(fzc[:i] > 100.0) / fs
            if min_s <= (j - i) / fs <= MAX_FLIGHT_S and j < n and loaded_before >= 0.1:
                if best is None or (j - i) > (best[1] - best[0]):
                    best = (i, j)
            i = j
        else:
            i += 1
    return best


def quiet_window(fz, fs, lo, hi, max_cv, win_s=0.3, min_n=150.0):
    """Steadiest win_s window inside [lo, hi). Returns dict or None."""
    w = int(win_s * fs)
    if hi - lo < w:
        return None
    best = None
    for s in range(lo, hi - w + 1, max(1, w // 10)):
        seg = fz[s:s + w]
        m = seg.mean()
        if m < min_n:
            continue
        cv = seg.std() / m
        if best is None or cv < best["cv"]:
            best = dict(bw_n=float(m), sd_n=float(seg.std()), cv=float(cv), start=s, end=s + w)
    return best if best and best["cv"] <= max_cv else None


def _prep(fz, fs, movement):
    """Flight, plate zero offset, contact start and quiet windows for one trial."""
    fz = np.asarray(fz, float).copy()
    fl = find_flight(fz, fs, movement)
    if fl is None:
        raise TrialError("no flight phase found")
    to, ld = fl
    a, b = to + int(0.1 * (ld - to)), ld - int(0.1 * (ld - to))
    offset = float(np.median(fz[a:b])) if b > a else 0.0
    fz -= offset                                   # the plate should read 0 N in the air
    unl = _unloaded_mask(fz, fs)
    pre = np.flatnonzero(unl[:to])
    c0 = int(pre[-1]) + 1 if pre.size else 0
    cv = QUIET_CV.get(movement, 0.02)
    pre_q = post_q = None
    if movement != "DJ" and to - c0 > 0:
        pre_q = quiet_window(fz, fs, c0, c0 + int(0.6 * (to - c0)), cv)
    if movement != "PPU":
        post_q = quiet_window(fz, fs, ld + int(0.5 * fs), len(fz), max(cv, 0.02))
    return dict(fz=fz, to=to, ld=ld, offset=offset, c0=c0, starts_unloaded=bool(pre.size),
                pre_quiet=pre_q, post_quiet=post_q)


def trial_quiet_bw(fz, fs, movement):
    """Quiet-stance BW from one trial (before the movement, else after landing). For the session pass."""
    try:
        p = _prep(fz, fs, movement)
    except TrialError:
        return None
    return p["pre_quiet"] or p["post_quiet"]


def _cross_up(x, level, lo, hi):
    idx = np.flatnonzero(x[lo:hi] >= level)
    return lo + int(idx[0]) if idx.size else None


def process_trial(fz, fs, movement, session_bw_n=None, entered_mass_kg=None, box_height_cm=None):
    """Metrics dict + arrays for one trial. movement in {CMJ, DJ, PPU, SLV}."""
    qc = []
    p = _prep(fz, fs, movement)
    fz, to, ld, offset, c0 = p["fz"], p["to"], p["ld"], p["offset"], p["c0"]
    if abs(offset) > 15:
        qc.append("plate_offset_gt_15N")
    t_flight = (ld - to) / fs
    v_to = G * t_flight / 2.0
    jh_m = G * t_flight ** 2 / 8.0
    # event sanity (the V3D failure modes): an extra unloaded stretch right next to the flight means a
    # toe-tap / re-contact split it; a very short flight means takeoff/landing were picked wrong
    unl_all = _unloaded_mask(fz, fs)
    near = np.r_[unl_all[max(0, to - int(0.10 * fs)):max(0, to - int(0.005 * fs))],
                 unl_all[min(len(fz), ld + int(0.005 * fs)):min(len(fz), ld + int(0.10 * fs))]]
    if near.sum() >= int(0.02 * fs):
        qc.append("flight_split_suspect")
    if movement != "PPU" and t_flight < 0.25:
        qc.append("flight_time_lt_0.25s")
    if to - c0 < int(0.05 * fs):
        raise TrialError("contact before takeoff shorter than 50 ms")
    pre = np.arange(c0) if p["starts_unloaded"] else np.array([], int)

    # ---- body weight ------------------------------------------------------
    quiet = p["pre_quiet"]
    if quiet is not None:
        bw, bw_src = quiet["bw_n"], "trial_quiet"
    elif p["post_quiet"] is not None and movement != "PPU":
        bw, bw_src = p["post_quiet"]["bw_n"], "trial_post_landing_quiet"
    elif movement == "PPU":
        bw, bw_src = float(fz[c0:c0 + int(0.05 * fs)].mean()), "ppu_first_50ms"
        qc.append("ppu_no_quiet_window")
    elif session_bw_n:
        bw, bw_src = float(session_bw_n), "session_bw"
    elif entered_mass_kg:
        bw, bw_src = float(entered_mass_kg) * G, "entered_mass"
        qc.append("bw_from_entered_mass")
    else:
        raise TrialError("no body weight available")
    if movement != "PPU" and entered_mass_kg:
        if abs(bw / G - entered_mass_kg) / entered_mass_kg > 0.08:
            qc.append("plate_bw_vs_entered_mass_gt_8pct")
    m = bw / G

    # ---- velocity (backward from takeoff), power ----------------------------
    acc = (fz[:to] - bw) / m
    v = v_to - np.cumsum(acc[::-1])[::-1] / fs
    P = fz[:to] * v
    fz_f = lowpass(fz, fs)[:to]
    P_f = fz_f * v

    out = dict(movement=movement, fs_hz=fs, plate_offset_n=offset,
               takeoff_idx=int(to), landing_idx=int(ld), contact_start_idx=int(c0),
               flight_time_s=t_flight, jh_in=jh_m / 0.0254, jh_m=jh_m, takeoff_velocity_m_s=v_to,
               bw_n=bw, bw_source=bw_src, mass_kg=m,
               starts_unloaded=bool(pre.size > 0), v_at_contact_start_m_s=float(v[c0]))

    # quiet-start QC
    if quiet is not None:
        qs = quiet["start"]
        bw_imp = float(np.sum(fz[qs:to]) / fs / ((to - qs) / fs + v_to / G))   # v=0 at quiet AND v=v_to at takeoff
        out.update(has_quiet_start=True, quiet_start_idx=qs, quiet_cv=quiet["cv"],
                   v_quiet_resid_m_s=float(v[qs:quiet["end"]].mean()),
                   bw_impulse_n=bw_imp, bw_impulse_diff_pct=100 * (bw_imp / bw - 1))
        if movement != "PPU" and abs(out["bw_impulse_diff_pct"]) > 2:
            qc.append("bw_impulse_vs_quiet_gt_2pct")
        if movement != "PPU" and abs(out["v_quiet_resid_m_s"]) > 0.15:
            qc.append("v_quiet_resid_gt_0.15")
    else:
        out.update(has_quiet_start=False)

    # ---- phases -------------------------------------------------------------
    seg_v = v[c0:to]
    neg = np.flatnonzero(seg_v <= 0)
    con0 = c0 + int(neg[-1]) + 1 if neg.size else c0
    vmin_i = c0 + int(np.argmin(seg_v))
    out.update(concentric_start_idx=int(con0), vmin_m_s=float(v[vmin_i]),
               concentric_duration_s=(to - con0) / fs, time_to_takeoff_from_contact_s=(to - c0) / fs)

    onset = None
    if quiet is not None and movement in ("CMJ", "SLV", "PPU"):
        thr = max(5 * quiet["sd_n"], 0.05 * bw)
        dev = np.flatnonzero(np.abs(fz[quiet["end"]:con0] - bw) > thr)
        if dev.size:
            onset = max(quiet["end"], quiet["end"] + int(dev[0]) - int(0.03 * fs))
    if onset is not None and onset < con0:
        disp = np.cumsum(v[onset:con0]) / fs
        brk = slice(vmin_i, con0) if vmin_i < con0 else None
        out.update(
            movement_onset_idx=int(onset),
            eccentric_duration_s=(con0 - onset) / fs,
            ecc_con_duration_ratio=((con0 - onset) / fs) / ((to - con0) / fs) if to > con0 else np.nan,
            contraction_time_s=(to - onset) / fs,
            countermovement_depth_m=float(-disp.min()) if disp.size else np.nan,
            unweighting_min_force_bw=float(fz[onset:vmin_i + 1].min() / bw) if vmin_i >= onset else np.nan,
            braking_duration_s=(con0 - vmin_i) / fs if brk else np.nan,
            braking_impulse_ns=float(np.sum(fz[brk] - bw) / fs) if brk else np.nan,
            braking_mean_force_bw=float(fz[brk].mean() / bw) if brk else np.nan,
            eccentric_peak_power_w=float(P[onset:con0].min()),
            mrsi=jh_m / ((to - onset) / fs),
        )
    if movement == "DJ":
        ct = (to - c0) / fs
        vtd = float(v[c0])
        out.update(ct_s=ct, rsi=jh_m / ct, rsi_legacy=2 * jh_m / ct,
                   touchdown_velocity_m_s=vtd,
                   implied_drop_cm=float(vtd ** 2 / (2 * G) * 100) if vtd < 0 else np.nan,
                   braking_duration_s=(con0 - c0) / fs,
                   braking_impulse_ns=float(np.sum(fz[c0:con0] - bw) / fs) if con0 > c0 else np.nan)
        if not out["starts_unloaded"]:
            qc.append("dj_contact_start_not_in_file")
        if box_height_cm and np.isfinite(out["implied_drop_cm"]):
            if abs(out["implied_drop_cm"] - box_height_cm) > 10:
                qc.append("dj_implied_drop_vs_box_gt_10cm")

    # ---- force metrics --------------------------------------------------------
    ipf = c0 + int(np.argmax(fz[c0:to]))
    rfd = np.gradient(fz_f, 1.0 / fs)
    irfd = c0 + int(np.argmax(rfd[c0:ipf + 1])) if ipf > c0 else c0
    out.update(peak_force_n=float(fz[ipf]), peak_force_bw=float(fz[ipf] / bw),
               peak_force_before_takeoff_s=(to - ipf) / fs,
               peak_rfd_n_per_s=float(rfd[irfd]), peak_rfd_bw_per_s=float(rfd[irfd] / bw),
               concentric_impulse_ns=float(np.sum(fz[con0:to] - bw) / fs),
               concentric_mean_force_n=float(fz[con0:to].mean()) if to > con0 else np.nan)

    # ---- power metrics --------------------------------------------------------
    ipp = c0 + int(np.argmax(P[c0:to]))
    pp = float(P[ipp])
    out.update(peak_power_w=pp, pp_w_per_kg=pp / m, vel_at_pp_m_s=float(v[ipp]), force_at_pp_n=float(fz[ipp]),
               pp_before_takeoff_s=(to - ipp) / fs,
               concentric_mean_power_w=float(P[con0:to].mean()) if to > con0 else np.nan,
               concentric_work_j=float(np.sum(P[con0:to]) / fs))
    rpd = np.gradient(P_f, 1.0 / fs)
    lo = con0 if con0 < ipp else c0
    irpd = lo + int(np.argmax(rpd[lo:ipp + 1])) if ipp >= lo else ipp
    out.update(rpd_max_w_per_s=float(rpd[irpd]), rpd_max_before_takeoff_s=(to - irpd) / fs)
    i10 = _cross_up(P_f, 0.1 * pp, lo, ipp + 1)
    i90 = _cross_up(P_f, 0.9 * pp, lo, ipp + 1)
    if i10 is not None and i90 is not None and i90 > i10:
        out.update(rise_time_10_90_s=(i90 - i10) / fs, rpd_10_90_w_per_s=0.8 * pp / ((i90 - i10) / fs))
    fall = P_f[ipp:to]
    j90 = np.flatnonzero(fall <= 0.9 * pp)
    j10 = np.flatnonzero(fall <= 0.1 * pp)
    if j90.size and j10.size and j10[0] > j90[0]:
        out["decay_90_10_s"] = (j10[0] - j90[0]) / fs

    # ---- curve shape (defined windows, both kurtosis definitions) ------------
    out.update(curve_shape(P, fs, con0, to - 1, prefix="pwr_con_"))
    out.update(curve_shape(fz, fs, con0, to - 1, prefix="fz_con_"))
    start = onset if onset is not None else c0
    out.update(curve_shape(fz, fs, start, to - 1, prefix="fz_contact_"))

    if movement == "PPU":
        qc.append("ppu_point_mass_model")   # hands-only load, body rotates on feet: power is an index
    out["qc_flags"] = qc
    return out, dict(fz=fz, v=v, P=P)
