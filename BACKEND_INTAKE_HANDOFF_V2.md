# Backend Intake Handoff — Readiness Screen v2

This document describes every change made in the **Readiness Screen Tracker** (the local Flask app)
as part of the v2 upgrade, explained in terms of what each file does and what data it produces,
so the equivalent changes can be made in the **backend app's intake pipeline** for this same screen.

The two repos share the same Neon warehouse tables (`f_readiness_screen_*`). The local app is the
source of truth for the v1 column definitions and ingestion logic; the backend mirrors that logic
for its own intake path. Wherever you see a file described below, look for its counterpart in
the backend — they may not have identical names, but they do the same job.

The schema migration (new table + new columns) was already applied separately via
`BACKEND_READINESS_HANDOFF.md`. This document is only about the **code** changes needed so
the intake pipeline populates those new columns correctly.

---

## Table of changes

| This repo's file | What it does | What changed |
|---|---|---|
| `ingestion/file_parsers.py` | Discovers which movement files exist; defines column layout | Removed I and T from active discovery |
| `ingestion/units.py` | Unit conversion helpers | Added `inches_to_meters` |
| `ingestion/power_analysis.py` | Analyzes the raw power-time signal from `*_Power.txt` files | Added phase detection + 9 new derived metrics |
| `ingestion/pipeline.py` | End-to-end orchestrator: discovers files → parses → resolves athlete → upserts to DB → scores | Passes jump height (in meters) and movement type into power analysis; writes phase metrics to CMJ/PPU and power_curve tables; accepts and upserts grip data |
| `ingestion/scoring.py` | Computes composite readiness z-score from the day's fact rows | Dropped I and T from metric list; added phase metrics for CMJ; added PPU phase metrics (subset); added grip group; added grip_z to score row; added intra-session CV; added insufficient_history flag |

---

## 1. File discovery — `ingestion/file_parsers.py`

**What this file does:** Defines which movement files to look for in the output folder and how to
parse them. Isometric tests (Y, IR90, and historically I and T) use static filenames. CMJ and PPU
trials are discovered dynamically by filename pattern (CMJ1.txt, CMJ2.txt, etc.) with matching
power-time series files (CMJ1_Power.txt, etc.).

**What changed:**

```
Before: actively ingested I, Y, T, IR90
After:  actively ingests Y, IR90 only

ASCII_FILES = {
    "Y":    "y_data.txt",
    "IR90": "ir90_data.txt",
}
```

I (`i_data.txt`) and T (`t_data.txt`) are still in the DB tables for historical reads but no
new rows should be written. The backend's equivalent of this file — wherever it defines the list
of ISO movements to ingest — should be updated the same way: remove I and T from the active list.

**Data affected:** No new columns. This is purely a gate: stop writing to `f_readiness_screen_i`
and `f_readiness_screen_t` going forward.

---

## 2. Unit conversion — `ingestion/units.py`

**What this file does:** Thin helpers for converting between SI and US units used throughout the
pipeline. Session XML provides height in meters; the DB stores jump height in inches. The backend
equivalent is likely a shared `units` or `conversions` module.

**What changed:** Added `inches_to_meters`. This is needed to convert the parsed `JH_IN` field
(jump height in inches) to meters before computing mRSI (`jump_height_m / contraction_time_s`).

```python
def inches_to_meters(inches):
    # None/0 → None
    return inches / 39.3701
```

**Data affected:** The converted meters value is passed into the power analysis function — it does
not itself get stored in the DB. The DB still stores `jump_height` in inches (unchanged). The
conversion only exists to produce correct mRSI values.

---

## 3. Power-time signal analysis — `ingestion/power_analysis.py`

**What this file does:** Takes the raw power-time signal from a `*_Power.txt` file (one column of
watts at 1000 Hz) and computes curve-shape metrics. This is the most computationally intensive
part of the pipeline. The backend almost certainly has an equivalent module — look for something
that reads the power signal and returns metrics like `peak_power_w`, `rpd_max_w_per_s`, etc.

**What changed:** Added two new functions and extended the main analysis function.

### New function: `detect_phases(power_array, fs_hz, movement_type)`

Identifies four indices in the power-time signal:
- `onset_idx` — where movement begins (first sample where |power| > threshold)
- `bottom_idx` — the eccentric→concentric transition (last zero-crossing before the global peak)
- `peak_idx` — sample of maximum positive power
- `takeoff_idx` — first sample after peak where power returns near zero (athlete leaves ground / hands leave)

**Movement-type logic:**
- `CMJ`: uses absolute power threshold; finds the last negative→positive zero-crossing before peak for `bottom_idx`
- `PPU`: skip eccentric detection entirely — `bottom_idx = onset_idx` because the athletes perform PPU from a still plank position (no drop-catch, no meaningful eccentric phase)

### New function: `analyze_phase_metrics(power_array, fs_hz, jump_height_m, movement_type)`

Returns a 9-key dict. These are the values written to the new columns:

| Key | Definition | Unit | CMJ | PPU |
|-----|------------|------|-----|-----|
| `contraction_time_s` | `(takeoff_idx - onset_idx) / fs_hz` | seconds | ✓ | ✓ |
| `eccentric_duration_s` | `(bottom_idx - onset_idx) / fs_hz` | seconds | ✓ | NULL |
| `concentric_duration_s` | `(takeoff_idx - bottom_idx) / fs_hz` | seconds | ✓ | ✓ |
| `ecc_con_duration_ratio` | `eccentric_duration_s / concentric_duration_s` | ratio | ✓ | NULL |
| `eccentric_mean_power_w` | `mean(power[onset:bottom])` — will be negative | watts | ✓ | NULL |
| `eccentric_peak_power_w` | `min(power[onset:bottom])` — most negative | watts | ✓ | NULL |
| `eccentric_auc_j` | `abs(trapz(power[onset:bottom])) / fs_hz` | joules | ✓ | NULL |
| `concentric_auc_j` | `trapz(power[bottom:takeoff]) / fs_hz` | joules | ✓ | ✓ |
| `mrsi` | `jump_height_m / contraction_time_s` | m/s | ✓ | ✓ |

PPU `NULL` entries: because `bottom_idx == onset_idx` for still-start PPU, the eccentric window
is empty → all eccentric fields return `None`. `contraction_time_s`, `concentric_duration_s`,
`concentric_auc_j`, and `mrsi` are still populated for PPU.

All metrics return `None` gracefully on degenerate signals (no zero-crossing found, concentric
duration = 0, jump_height_m not provided, etc.).

### Change to `analyze_power_curve_advanced(power, fs_hz, jump_height_m, movement_type)`

This is the main function the pipeline calls. It now accepts two new parameters:
- `jump_height_m` (float | None) — jump height converted to meters, needed for mRSI
- `movement_type` ("CMJ" | "PPU") — controls phase detection behavior

At the end, it calls `analyze_phase_metrics` and merges the 9 phase keys into the return dict.
The backend equivalent of this function should receive the same two new parameters.

---

## 4. Ingestion orchestrator — `ingestion/pipeline.py`

**What this file does:** The end-to-end pipeline. It discovers files, parses them, resolves each
athlete UUID against `d_athletes`, upserts rows into the fact tables, and triggers scoring.
The backend equivalent is wherever the readiness screen intake is orchestrated — likely a service,
controller, or pipeline function that drives the same sequence.

### Change A — Pass jump height in meters and movement type to power analysis

Before v2, the power analysis call was:
```python
pa = analyze_power_curve_advanced(pw_arr, fs_hz=fs_hz)
```

After v2:
```python
jh_m = inches_to_meters(parsed.get("JH_IN"))
pa = analyze_power_curve_advanced(
    pw_arr,
    fs_hz=fs_hz,
    jump_height_m=jh_m,
    movement_type=movement,   # "CMJ" or "PPU"
)
```

This is the only change needed to produce mRSI and the other phase metrics. If the backend's
power analysis call doesn't pass these, all phase columns will be None.

### Change B — Write phase columns to `f_readiness_screen_cmj` / `f_readiness_screen_ppu`

The 9 phase columns (same names as in the DB schema) are now included in the upsert:

```python
PHASE_COLS = [
    "contraction_time_s", "eccentric_duration_s", "concentric_duration_s",
    "ecc_con_duration_ratio", "eccentric_mean_power_w", "eccentric_peak_power_w",
    "eccentric_auc_j", "concentric_auc_j", "mrsi",
]
```

These come from `analyze_power_curve_advanced`'s return dict (same keys). The backend's CMJ/PPU
upsert just needs to add these 9 columns to its column list and pull the values from the analysis
result the same way it already pulls `peak_power_w`, `rpd_max_w_per_s`, etc.

### Change C — Write phase columns to `f_readiness_screen_power_curve`

The same 9 phase columns are also written to the per-trial power curve table. The backend
equivalent — wherever it persists per-trial curve metrics — should include these too.

### Change D — Grip strength upsert

Grip data is manually entered (not from a file), so this change is specific to how the local app
receives it. The backend's intake source for grip may be different (API payload, separate form,
etc.). What matters for the backend is the **data shape** being written to `f_readiness_screen_grip`:

| Column | Source | Notes |
|--------|--------|-------|
| `athlete_uuid` | resolved from athlete name | same as every other row |
| `session_date` | same session date as CMJ/PPU | one row per athlete per session |
| `left_kg` | raw input, converted from lbs if needed | max value from 3 reps |
| `right_kg` | same | |
| `avg_kg` | `(left_kg + right_kg) / 2` | computed in code, not a DB trigger |
| `max_kg` | `max(left_kg, right_kg)` | computed in code |
| `asymmetry_pct` | `100 * abs(L - R) / max(L, R)` | computed in code |
| `dominant_hand` | "R", "L", or None | optional |
| `entry_source` | "manual" | hardcoded for now |
| `notes` | free text | optional |

The unique key is `(athlete_uuid, session_date)` — one grip row per session, not per trial.

---

## 5. Scoring — `ingestion/scoring.py`

**What this file does:** After the fact rows are upserted, this module z-scores each metric
against the athlete's own rolling 28-day baseline, computes a composite score, and writes one
row to `f_readiness_screen_score`. The backend equivalent handles the same computation — look
for wherever it reads from the readiness screen fact tables, computes z-scores, and writes a
composite score.

### Change A — ISO metric list (remove I and T)

```python
# Before:
ISO_METRICS = [
    ("f_readiness_screen_i",    "max_force",   +1),
    ("f_readiness_screen_y",    "max_force",   +1),
    ("f_readiness_screen_t",    "max_force",   +1),
    ("f_readiness_screen_ir90", "max_force",   +1),
    ("f_readiness_screen_i",    "time_to_max", -1),
    ("f_readiness_screen_y",    "time_to_max", -1),
    ("f_readiness_screen_t",    "time_to_max", -1),
    ("f_readiness_screen_ir90", "time_to_max", -1),
]

# After:
ISO_METRICS = [
    ("f_readiness_screen_y",    "max_force",   +1),
    ("f_readiness_screen_ir90", "max_force",   +1),
    ("f_readiness_screen_y",    "time_to_max", -1),
    ("f_readiness_screen_ir90", "time_to_max", -1),
]
```

### Change B — CMJ metric list (add phase metrics)

```python
CMJ_METRICS = [
    # existing
    ("f_readiness_screen_cmj", "jump_height",            +1),
    ("f_readiness_screen_cmj", "pp_w_per_kg",            +1),
    ("f_readiness_screen_cmj", "force_at_pp",            +1),
    ("f_readiness_screen_cmj", "vel_at_pp",              +1),
    # new in v2
    ("f_readiness_screen_cmj", "mrsi",                   +1),  # higher = better
    ("f_readiness_screen_cmj", "contraction_time_s",     -1),  # lower = better (faster)
    ("f_readiness_screen_cmj", "ecc_con_duration_ratio", -1),  # lower = better
    ("f_readiness_screen_cmj", "eccentric_mean_power_w", -1),  # more negative = better; sign=-1 flips
]
```

Sign note on `eccentric_mean_power_w`: the stored value is negative (energy absorption). A
larger-magnitude negative (e.g., -1500 W vs -1000 W) means faster eccentric loading which is
better. Because the raw value is more negative on a good day, `sign = -1` so the z-score is
positive on a good day.

### Change C — PPU metric list (add only the two concentric metrics)

```python
PPU_METRICS = [
    # existing
    ("f_readiness_screen_ppu", "jump_height",        +1),
    ("f_readiness_screen_ppu", "pp_w_per_kg",        +1),
    ("f_readiness_screen_ppu", "force_at_pp",        +1),
    ("f_readiness_screen_ppu", "vel_at_pp",          +1),
    # new in v2 — eccentric metrics omitted (still-start protocol, always NULL for PPU)
    ("f_readiness_screen_ppu", "mrsi",               +1),
    ("f_readiness_screen_ppu", "contraction_time_s", -1),
]
```

Do NOT include `ecc_con_duration_ratio` or `eccentric_mean_power_w` for PPU — they are always
NULL and would just produce no-op entries in the score.

### Change D — New GRIP_METRICS group

```python
GRIP_METRICS = [
    ("f_readiness_screen_grip", "left_kg",       +1),
    ("f_readiness_screen_grip", "right_kg",      +1),
    ("f_readiness_screen_grip", "max_kg",        +1),
    ("f_readiness_screen_grip", "asymmetry_pct", -1),  # lower asymmetry = better
]
```

### Change E — `grip_z` added to score row

The scoring function now computes a per-group mean z for grip and writes it alongside the
existing `cmj_z`, `ppu_z`, `iso_z`, `power_curve_z`:

```sql
-- New column in the INSERT:
grip_z   NUMERIC   -- mean z-score across GRIP_METRICS for this session
```

When no grip data exists (no row in `f_readiness_screen_grip` for this session), `grip_z` is
NULL. Historical sessions where grip was not collected should not be backfilled — leave NULL.

### Change F — Intra-session CV computation

After scoring, the pipeline computes the coefficient of variation between the two trials for each
of: `cmj.jump_height`, `cmj.mrsi`, `cmj.peak_power_w`, `ppu.jump_height`, `ppu.mrsi`,
`ppu.peak_power_w`. It then compares today's CV to the 28-day rolling baseline CV for that metric.

Result is stored as JSON under `flags_json.intra_session` on the score row:

```json
{
  "per_metric": { ... },
  "intra_session": {
    "cmj.jump_height": {"today_cv": 0.012, "baseline_mean_cv": 0.008, "flag": "stable"},
    "cmj.mrsi":        {"today_cv": 0.083, "baseline_mean_cv": 0.020, "flag": "elevated"},
    ...
  }
}
```

`flag` is `"elevated"` when `today_cv > 2 × baseline_mean_cv`, otherwise `"stable"`.

### Change G — `insufficient_history` flag in `flags_json.per_metric`

Previously, metrics with fewer than 3 baseline sessions were silently skipped (no z-score
computed, no entry in flags). Now they get an explicit entry:

```json
{
  "cmj.mrsi": {
    "today":     0.42,
    "flag":      "insufficient_history",
    "n_history": 1,
    "sign":      1
  }
}
```

This lets the dashboard distinguish "no data today" from "data exists but not enough history yet
to z-score" — important especially at rollout for new metrics like grip and mRSI.

---

## Data flow summary

For each readiness screen session, the complete v2 data path is:

```
*_Power.txt (1000 Hz signal)
    └─► analyze_power_curve_advanced(power, fs_hz, jump_height_m=JH_in_meters, movement_type)
            ├─ POWER_CURVE_COLS  ──────────────────────────────────────────►  f_readiness_screen_cmj / ppu (inline)
            ├─ PHASE_COLS        ──────────────────────────────────────────►  f_readiness_screen_cmj / ppu (inline)
            └─ full dict ─────────────────────────────────────────────────►  f_readiness_screen_power_curve (per trial)

CMJ*.txt / PPU*.txt (5-col summary)
    └─► parsed JH_IN, PP_FORCEPLATE, Force_at_PP, Vel_at_PP, PP_W_per_kg
            └─ stored as jump_height, pp_forceplate, force_at_pp, vel_at_pp, pp_w_per_kg

y_data.txt / ir90_data.txt (5-col isometric)
    └─► Max_Force, Avg_Force, Time_to_Max
            └─ stored in f_readiness_screen_y / ir90

Grip (manual entry)
    └─► left_kg, right_kg → avg_kg, max_kg, asymmetry_pct computed in code
            └─ stored in f_readiness_screen_grip (one row per session)

All fact rows for session
    └─► z-score each metric against 28-day baseline
            └─► composite_score, band, cmj_z, ppu_z, iso_z, power_curve_z, grip_z
                intra_session CV, per_metric flags (including insufficient_history)
                    └─ stored in f_readiness_screen_score
```

---

## What the backend intake does NOT need to replicate

- The maintenance UI form and SSE streaming (local-app-specific)
- The `athlete_uuid_override` flow (local-app-specific for correcting mislabeled files)
- The `cancel_event` threading logic (local-app-specific)

The backend should focus on: (1) correct file discovery, (2) passing `movement_type` and
`jump_height_m` into power analysis, (3) writing all PHASE_COLS to the fact tables, (4) updating
the scoring metric lists, and (5) populating `f_readiness_screen_grip`.
