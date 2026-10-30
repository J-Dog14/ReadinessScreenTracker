"""
Read vertical force from athletic-screen c3d files.

Current protocol: everything on force plate 3 (FP3). Older sessions (before
~Sep 2025) did drop jumps with one foot on FP1 and one on FP2, FP3 empty - so
for each trial we use FP3 if it was loaded, otherwise the sum of the loaded
plates, and record which in `plate_used`.

ezc3d is used with extract_forceplat_data=False: its platform extractor crashes
on these files (FORCE_PLATFORM:USED can be 3 while only some plates have
channels). Plates are mapped through FORCE_PLATFORM:CHANNEL (column k = plate k+1).
"""
import re
import numpy as np
import ezc3d

LOADED_N = 200.0


class C3DReadError(Exception):
    pass


def _clean(x):
    x = x.astype(float).copy()
    bad = ~np.isfinite(x)
    if bad.all():
        return None
    if bad.any():
        idx = np.arange(x.size)
        x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])
    if np.nanmax(x) < -np.nanmin(x):          # loaded = positive
        x = -x
    return x


def read_plates(path):
    """Return ({plate_no: Fz_N}, fs_hz, meta). Trailing all-NaN samples trimmed."""
    try:
        c = ezc3d.c3d(str(path))
    except Exception as e:
        raise C3DReadError(f"ezc3d failed: {e}")
    p = c["parameters"]
    if "ANALOG" not in p or c["data"]["analogs"].size == 0:
        raise C3DReadError("no analog data")
    labels = list(p["ANALOG"]["LABELS"]["value"])
    fs = float(p["ANALOG"]["RATE"]["value"][0])
    an = c["data"]["analogs"][0]

    chans = {}
    fp = p.get("FORCE_PLATFORM", {})
    if "CHANNEL" in fp:
        ch = np.asarray(fp["CHANNEL"]["value"])
        if ch.ndim == 1:
            ch = ch.reshape(-1, 1)
        for k in range(ch.shape[1]):
            if ch.shape[0] >= 3 and 0 < ch[2, k] <= an.shape[0]:
                chans[k + 1] = int(ch[2, k]) - 1
    if not chans:
        fz = [i for i, l in enumerate(labels) if re.search(r"Fz$", l.strip())]
        chans = {i + 1: j for i, j in enumerate(fz)}
    if not chans:
        raise C3DReadError(f"no Fz channels (labels={labels})")

    # common valid range across the plates we have
    finite = np.zeros(an.shape[1], bool)
    for j in chans.values():
        finite |= np.isfinite(an[j])
    if finite.sum() < fs * 0.3:
        raise C3DReadError("Fz mostly NaN")
    first, last = np.flatnonzero(finite)[[0, -1]]
    plates = {}
    for k, j in chans.items():
        x = _clean(an[j, first:last + 1])
        if x is not None:
            plates[k] = x
    meta = dict(fs_hz=fs, first_frame=int(c["header"]["points"]["first_frame"]),
                point_rate_hz=float(p["POINT"]["RATE"]["value"][0]) if "POINT" in p else None,
                n_samples=int(last - first + 1), nan_trimmed=int(an.shape[1] - (last - first + 1)),
                plates_present=sorted(plates))
    return plates, fs, meta


def read_screen_fz(path):
    """Vertical force for a screen trial: FP3 if loaded, else sum of loaded plates."""
    plates, fs, meta = read_plates(path)
    loaded = {k: v for k, v in plates.items() if np.nanmax(v) > LOADED_N}
    if not loaded:
        raise C3DReadError("no plate loaded (max Fz < 200 N on every plate)")
    if 3 in loaded:
        fz, used = loaded[3], "FP3"
        if len(loaded) > 1:
            meta["other_plates_loaded"] = [k for k in loaded if k != 3]
    else:
        ks = sorted(loaded)
        fz, used = np.sum([loaded[k] for k in ks], axis=0), "+".join(f"FP{k}" for k in ks)
    meta["plate_used"] = used
    meta["fz_channel_how"] = used          # kept for the old column name
    return fz, fs, meta


def read_fp3_fz(path):          # backwards-compatible name
    return read_screen_fz(path)


def read_total_fz(path):
    """Sum of every plate (for static / standing trials)."""
    plates, fs, meta = read_plates(path)
    return np.sum(list(plates.values()), axis=0), fs, meta
