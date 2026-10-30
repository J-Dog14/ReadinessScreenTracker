#!/usr/bin/env python3
"""
Readiness Screen v2 - session processor (vendored from OctaneBiomechBackend
uais/python/readinessScreen/v2/scan_sessions.py - keep the two in sync).

No database access. Walks one or more QTM Data folders:
  * D:/Athletic Screen 2.0/Data  - current. A readiness session is its own folder whose
                                   session.xml has Session Type="Readiness Screen".
  * D:/Readiness Screen 3/Data   - older project (session type "Test"); every session folder
                                   there is a readiness session.
CMJ / PPU use the Athletic Screen v2 kinetics (velocity integrated backward from takeoff,
quiet-window body weight). I / Y / T / IR90 use isometric.py.

Writes (default tmp/readiness_v2):
    sessions.csv                  one row per session folder (incl. skipped ones + why)
    trials_cmj.csv, trials_ppu.csv, trials_iso.csv
    curves/<root>__<athlete>__<session>__<trial>.npz
    errors.csv, qc_report.md

Usage (repo root):
    python ingestion/c3d/process.py ^
        --data "D:/Athletic Screen 2.0/Data" --data "D:/Readiness Screen 3/Data"
    ... --athlete "Andrew Mullis"     only athlete folders containing this text
"""
import argparse
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]                     # Readiness Screen Tracker root
if __package__ in (None, ""):              # run as a script: python ingestion/c3d/process.py
    sys.path.insert(0, str(REPO))
    __package__ = "ingestion.c3d"

from . import PROCESSOR_VERSION                                                   # noqa: E402
from .isometric import process_isometric, IsoError                                # noqa: E402
from .c3d_reader import read_plates, read_screen_fz, read_total_fz, C3DReadError  # noqa: E402
from .kinetics import process_trial, trial_quiet_bw, quiet_window, _prep, TrialError, G  # noqa: E402
from .session_meta import parse_session_xml                                       # noqa: E402

TRIAL_RE = re.compile(r"^(CMJ|PPU|EYE|TEE|WHY|IR90)\s*(\d+)\.c3d$", re.I)
CODE_TO_MOVEMENT = {"CMJ": "CMJ", "PPU": "PPU", "EYE": "I", "TEE": "T", "WHY": "Y", "IR90": "IR90"}
ISO = ("I", "T", "Y", "IR90")
SCALAR_SKIP = {"qc_flags"}


def trial_name(movement, num):
    """CMJ1 / PPU2 (same as the old readiness trial_name); isometrics get a separator so IR90 trial 1
    is 'IR90_1', not 'IR901'."""
    return f"{movement}{num}" if movement in ("CMJ", "PPU") else f"{movement}_{num}"


def root_label(path):
    p = str(path).lower()
    if "readiness screen 3" in p:
        return "RS3"
    if "athletic screen 2.0" in p:
        return "AS2"
    return Path(path).parent.name          # e.g. a copied tree <...>/RS3/Data


def athlete_from_folder(folder_name):
    if "_" in folder_name:
        name, _, sid = folder_name.rpartition("_")
        return name.strip(), sid.strip()
    return folder_name.strip(), None


def age_at(dob, sdate):
    try:
        d0 = datetime.strptime(dob, "%Y-%m-%d").date()
        d1 = datetime.strptime(sdate, "%Y-%m-%d").date()
        return round((d1 - d0).days / 365.25, 2)
    except Exception:
        return None


def read_iso_fz(path):
    """Isometric trials only load a hand on the plate (~20-200 N), below the jump reader's
    200 N 'loaded' test. Use FP3 when it is the plate with the push, else the plate that has it."""
    plates, fs, meta = read_plates(path)
    peaks = {k: float(np.nanmax(v)) for k, v in plates.items()}
    if not peaks or max(peaks.values()) < 15:
        raise C3DReadError(f"no push on any plate (max Fz {max(peaks.values(), default=0):.0f} N)")
    k = 3 if 3 in peaks and peaks[3] >= 15 else max(peaks, key=peaks.get)
    meta["plate_used"] = f"FP{k}"
    others = [p for p, v in peaks.items() if p != k and v >= 15]
    if others:
        meta["other_plates_loaded"] = others
    return plates[k], fs, meta


def session_body_weight(folder, loaded, entered_mass_kg):
    """Plate body weight: CMJ quiet stance in this session, else a same-day sibling folder
    (e.g. the athletic screen done the same day), else the impulse from a standing start, else a
    Static trial, else None (process_trial then falls back to the typed-in mass)."""
    qbw = []
    for name, (mv, fz, fs, _) in loaded.items():
        if mv == "CMJ":
            q = trial_quiet_bw(fz, fs, "CMJ")
            if q:
                qbw.append(q["bw_n"])
    if qbw:
        return float(np.median(qbw)), "session_cmj_quiet", qbw
    prefix = folder.name[:10]
    for sib in sorted(folder.parent.iterdir()):
        if sib == folder or not sib.is_dir() or not sib.name.startswith(prefix):
            continue
        for f in sorted(list(sib.glob("CMJ*.c3d")) + list(sib.glob("SLV*.c3d"))):
            try:
                fz, fs, _ = read_screen_fz(f)
            except C3DReadError:
                continue
            q = trial_quiet_bw(fz, fs, "SLV" if f.name.upper().startswith("SLV") else "CMJ")
            if q and f.name.upper().startswith("CMJ"):
                qbw.append(q["bw_n"])
    if qbw:
        return float(np.median(qbw)), "same_day_other_session", qbw
    # CMJs that start standing but move before a 0.3 s quiet window exists (common in readiness):
    # BW = integral(F) / (T + v_takeoff/g), i.e. v = 0 at the first sample and v = v_takeoff at takeoff.
    # Same method as the athletic-screen scan (|error| median 0.5-0.9 % vs 1.7 % for the typed-in mass).
    if entered_mass_kg:
        ent = entered_mass_kg * G
        for name, (mv, fz, fs, _) in loaded.items():
            if mv != "CMJ":
                continue
            try:
                p = _prep(fz, fs, "CMJ")
            except TrialError:
                continue
            f, to, ld = p["fz"], p["to"], p["ld"]
            head = f[:int(0.15 * fs)]
            if p["starts_unloaded"] or head.mean() <= 0 or abs(head.mean() / ent - 1) > 0.10 \
                    or head.std() / head.mean() > 0.05:
                continue
            v_to = G * (ld - to) / fs / 2.0
            qbw.append(float(f[:to].sum() / fs / (to / fs + v_to / G)))
    if qbw:
        return float(np.median(qbw)), "impulse_from_start", qbw
    for f in sorted(folder.glob("Static*.c3d")):
        try:
            fz, fs, _ = read_total_fz(f)
        except C3DReadError:
            continue
        q = quiet_window(fz, fs, 0, len(fz), 0.02, win_s=0.5)
        if q:
            qbw.append(q["bw_n"])
    if qbw:
        return float(np.median(qbw)), "static_trial", qbw
    return None, None, []


def classify(meta, root, c3ds):
    """Is this folder a readiness session? Returns (kind or None, reason)."""
    types = {t.lower() for t in meta["session_types"]}
    if "readiness screen" in types:
        return "readiness_screen", None
    if root == "RS3":
        codes = {TRIAL_RE.match(f.name).group(1).upper() for f in c3ds}
        if codes:
            return "rs3_project", None
        return None, "no readiness c3d files"
    return None, "no Readiness Screen session in session.xml"


def process_session(folder, data_root, out, since=None):
    root = root_label(data_root)
    meta = parse_session_xml(folder / "session.xml")
    ath_folder = folder.parent.name
    ath_name, ath_id = athlete_from_folder(ath_folder)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", folder.name)
    sdate = m.group(1) if m else meta.get("creation_date")
    srow = dict(source_root=root, athlete_folder=ath_folder, session_folder=folder.name, athlete_name=ath_name,
                athlete_id=ath_id, session_date=sdate, xml_name=meta["subject_name"],
                session_path=str(folder.relative_to(data_root)),
                weight_kg=meta["weight_kg"], height_m=meta["height_m"], dob=meta["dob"], gender=meta["gender"],
                email=meta["email"], age_at_collection=age_at(meta["dob"], sdate) if meta["dob"] and sdate else None,
                xml_session_types=";".join(meta["session_types"]), xml_measurements=";".join(meta["measurements"]))
    if since and sdate and sdate < since:
        srow["status"] = f"skipped: before {since}"
        return srow, [], []
    c3ds = sorted(f for f in folder.iterdir() if TRIAL_RE.match(f.name))
    srow["c3d_files"] = ";".join(f.name for f in c3ds)
    kind, why = classify(meta, root, c3ds)
    if kind is None:
        srow["status"] = f"skipped: {why}"
        return srow, [], []
    srow["readiness_kind"] = kind
    owner = meta["measurement_session_type"]

    trials = []
    for f in c3ds:
        stem = f.name.rsplit(".", 1)[0].strip().upper()
        o = owner.get(stem)
        if kind == "readiness_screen" and o is not None and o.lower() != "readiness screen":
            continue                    # belongs to another QTM session in the same folder
        mm = TRIAL_RE.match(f.name)
        code, num = mm.group(1).upper(), int(mm.group(2))
        mv = CODE_TO_MOVEMENT[code]
        trials.append(dict(path=f, movement=mv, num=num, trial_name=trial_name(mv, num),
                           in_session_xml=o is not None))
    if not trials:
        srow["status"] = "skipped: no readiness c3d files"
        return srow, [], []

    loaded, errors = {}, []
    for t in trials:
        try:
            fz, fs, cm = read_iso_fz(t["path"]) if t["movement"] in ISO else read_screen_fz(t["path"])
            loaded[t["trial_name"]] = (t["movement"], fz, fs, cm)
        except C3DReadError as e:
            errors.append(dict(source_root=root, athlete_folder=ath_folder, session_folder=folder.name,
                               session_date=sdate, trial_name=t["trial_name"], file=str(t["path"].relative_to(data_root)),
                               error=str(e)))
    plates = sorted({v[3]["plate_used"] for v in loaded.values()})
    srow["plates_used"] = ";".join(plates)
    off = sorted(f"{k} ({v[3]['plate_used']})" for k, v in loaded.items() if v[3]["plate_used"] != "FP3")
    if off:
        srow["status"] = "skipped: trials not on FP3 - " + ", ".join(off)
        return srow, [], errors

    sbw, sbw_src, qbw = session_body_weight(folder, loaded, meta["weight_kg"])
    srow.update(session_bw_n=sbw, session_bw_source=sbw_src, n_quiet_windows=len(qbw),
                session_bw_vs_entered_pct=(100 * (sbw / G / meta["weight_kg"] - 1)
                                           if sbw and meta["weight_kg"] else None))
    if sbw:
        iso_mass, iso_mass_src = sbw / G, sbw_src
    elif meta["weight_kg"]:
        iso_mass, iso_mass_src = meta["weight_kg"], "entered_mass"
    else:
        iso_mass, iso_mass_src = None, None

    rows = []
    for t in trials:
        if t["trial_name"] not in loaded:
            continue
        mv, fz, fs, cm = loaded[t["trial_name"]]
        base = dict(source_root=root, athlete_folder=ath_folder, athlete_name=ath_name, session_date=sdate,
                    session_folder=folder.name, readiness_kind=kind, movement=mv, trial_name=t["trial_name"],
                    trial_number=t["num"], in_session_xml=t["in_session_xml"],
                    source_c3d=str(t["path"].relative_to(data_root)), age_at_collection=srow["age_at_collection"],
                    processor_version=PROCESSOR_VERSION, c3d_first_frame=cm["first_frame"],
                    plate_used=cm["plate_used"], session_bw_source=sbw_src)
        key = f"{root}__{ath_folder}__{folder.name}__{t['trial_name']}"
        try:
            if mv in ISO:
                res, fzc = process_isometric(fz, fs, iso_mass)
                res["mass_source"] = iso_mass_src if iso_mass else None
                np.savez_compressed(out / "curves" / f"{key}.npz", fz_n=fzc.astype(np.float32), fs_hz=fs,
                                    bw_n=(iso_mass or np.nan) * G, plate_offset_n=res["plate_offset_n"],
                                    first_frame=cm["first_frame"], contact_start_idx=res["push_start_idx"],
                                    takeoff_idx=-1, landing_idx=-1, concentric_start_idx=-1,
                                    movement_onset_idx=-1, max_idx=res["max_idx"])
            else:
                res, arr = process_trial(fz, fs, mv, session_bw_n=sbw, entered_mass_kg=meta["weight_kg"])
                _readiness_extras(res, arr, fs)
                np.savez_compressed(out / "curves" / f"{key}.npz", fz_n=arr["fz"].astype(np.float32), fs_hz=fs,
                                    bw_n=res["bw_n"], takeoff_idx=res["takeoff_idx"], landing_idx=res["landing_idx"],
                                    contact_start_idx=res["contact_start_idx"],
                                    concentric_start_idx=res["concentric_start_idx"],
                                    movement_onset_idx=res.get("movement_onset_idx", -1),
                                    first_frame=cm["first_frame"], plate_offset_n=res["plate_offset_n"], max_idx=-1)
        except (TrialError, IsoError, Exception) as e:
            errors.append(dict(source_root=root, athlete_folder=ath_folder, session_folder=folder.name,
                               session_date=sdate, trial_name=t["trial_name"], file=base["source_c3d"],
                               error=f"{type(e).__name__}: {e}"))
            continue
        row = dict(base)
        row.update({k: v for k, v in res.items() if k not in SCALAR_SKIP})
        row["qc_flags"] = ";".join(res["qc_flags"])
        rows.append(row)

    # a CMJ far from the athlete's other CMJs that day usually means bad events
    cmj = [r for r in rows if r["movement"] == "CMJ"]
    if len(cmj) >= 3:
        for r in cmj:
            med = float(np.median([o["jh_in"] for o in cmj if o is not r]))
            if med > 0 and not (0.65 <= r["jh_in"] / med <= 1.40):
                r["qc_flags"] = ";".join([x for x in r["qc_flags"].split(";") if x] + ["jh_outlier_in_session"])
    srow["status"] = f"ok: {len(rows)} trials, {len(errors)} errors ({kind})"
    for mv in ("CMJ", "PPU") + ISO:
        srow[f"n_{mv.lower()}"] = sum(1 for r in rows if r["movement"] == mv)
    return srow, rows, errors


def _readiness_extras(res, arr, fs):
    """Columns the old readiness tables had that kinetics.py does not produce."""
    P, fz = arr["P"], arr["fz"]
    con0, to = res["concentric_start_idx"], res["takeoff_idx"]
    on = res.get("movement_onset_idx")
    if on is not None and on < con0:
        seg = P[on:con0]
        res["eccentric_mean_power_w"] = float(seg.mean())
        res["eccentric_work_j"] = float(abs(seg.sum()) / fs)
    j = con0 + int(0.1 * fs)
    res["rfd_con_0_100ms_n_per_s"] = float((fz[j] - fz[con0]) / 0.1) if j < to else np.nan


def _md(df):
    try:
        return df.to_markdown()
    except ImportError:
        return "```\n" + df.to_string() + "\n```"


def qc_report(out, sessions, frames, errors):
    L = [f"# Readiness Screen v2 scan - {datetime.now():%Y-%m-%d %H:%M}\n"]
    s = pd.DataFrame(sessions)
    L.append(f"Session folders seen: {len(s)}")
    if len(s):
        st = s.assign(st=s.status.astype(str).str.split(":").str[0])
        L.append("\n" + _md(st.groupby(["source_root", "st"]).size().unstack(fill_value=0)))
        sk = s[s.status.astype(str).str.startswith("skipped")]
        if len(sk):
            L.append("\nSkip reasons:\n\n" + _md(sk.status.str[:70].value_counts().head(12).to_frame("n")))
        ok = s[s.status.astype(str).str.startswith("ok")]
        if len(ok):
            L.append(f"\nReadiness sessions: {len(ok)} ({ok.athlete_folder.nunique()} athlete folders), "
                     f"{ok.session_date.min()} -> {ok.session_date.max()}")
            dup = ok.groupby(["athlete_folder", "session_date"]).source_root.nunique()
            L.append(f"Athlete/date pairs present in both roots: {int((dup > 1).sum())}")
            if "session_bw_source" in ok:
                L.append(f"Session body-weight source: {ok.session_bw_source.fillna('none').value_counts().to_dict()}")
    for name, df in frames.items():
        if df.empty:
            continue
        L.append(f"\n## {name}: {len(df)} trials, {df.athlete_folder.nunique()} athletes")
        flags = df.qc_flags.fillna("").str.split(";").explode()
        flags = flags[flags != ""]
        L.append(f"- QC flags: {flags.value_counts().to_dict() if len(flags) else 'none'}")
        if name in ("CMJ", "PPU"):
            L.append(f"- body weight source: {df.bw_source.value_counts().to_dict()}")
            L.append(f"- JH median {df.jh_in.median():.1f} in; PP W/kg median {df.pp_w_per_kg.median():.1f} "
                     f"(IQR {df.pp_w_per_kg.quantile(.25):.1f}-{df.pp_w_per_kg.quantile(.75):.1f})")
            if name == "CMJ":
                r = df.vel_at_pp_m_s / df.takeoff_velocity_m_s
                L.append(f"- vel@PP / takeoff velocity median {r.median():.2f} (expect ~0.9-0.97)")
        else:
            for mv, g in df.groupby("movement"):
                d = (g.max_force_n - g.max_force_raw_n).abs()
                L.append(f"- {mv}: n={len(g)}, max force median {g.max_force_n.median():.1f} N, "
                         f"time to max median {g.time_to_max_s.median():.2f} s, "
                         f"|offset| median {g.plate_offset_n.abs().median():.1f} N, "
                         f"|corrected-raw max| median {d.median():.1f} N")
    L.append(f"\nTrial errors: {len(errors)}")
    if errors:
        L.append("\n" + _md(pd.DataFrame(errors).error.str[:70].value_counts().head(15).to_frame("n")))
    (out / "qc_report.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", action="append", required=True,
                    help='QTM Data folder; repeat for several (e.g. Athletic Screen 2.0 and Readiness Screen 3)')
    ap.add_argument("--out", default=str(REPO / "tmp" / "readiness_v2"))
    ap.add_argument("--athlete", help="only athlete folders containing this text")
    ap.add_argument("--since", help="skip sessions before this date (default: no cutoff)")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "curves").mkdir(parents=True, exist_ok=True)
    if not args.athlete:
        for old in (out / "curves").glob("*.npz"):
            old.unlink()

    sessions, frames, errors = [], {"CMJ": [], "PPU": [], "ISO": []}, []
    for d in args.data:
        data_root = Path(d)
        xmls = sorted(Path(dp) / "session.xml" for dp, _, fns in os.walk(data_root, followlinks=True)
                      if "session.xml" in fns)
        if args.athlete:
            xmls = [x for x in xmls if args.athlete.lower() in x.parent.parent.name.lower()]
        print(f"{len(xmls)} session folders under {data_root} ({root_label(data_root)})")
        for i, x in enumerate(xmls, 1):
            try:
                srow, rows, errs = process_session(x.parent, data_root, out, args.since)
            except Exception as e:
                srow, rows, errs = dict(source_root=root_label(data_root),
                                        session_path=str(x.parent.relative_to(data_root)),
                                        status=f"error: {type(e).__name__}: {e}"), [], []
                traceback.print_exc()
            sessions.append(srow)
            errors += errs
            for r in rows:
                frames["ISO" if r["movement"] in ISO else r["movement"]].append(r)
            if not str(srow.get("status", "")).startswith("skipped"):
                print(f"  [{i}/{len(xmls)}] {srow.get('session_path')}: {srow.get('status')}", flush=True)

    pd.DataFrame(sessions).to_csv(out / "sessions.csv", index=False)
    pd.DataFrame(errors).to_csv(out / "errors.csv", index=False)
    dfs = {}
    for name, rows in frames.items():
        dfs[name] = pd.DataFrame(rows)
        dfs[name].to_csv(out / f"trials_{name.lower()}.csv", index=False)
    qc_report(out, sessions, dfs, errors)
    print(f"Done -> {out}  (see qc_report.md)")


if __name__ == "__main__":
    main()
