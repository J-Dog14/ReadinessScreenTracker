# Readiness Screen v2 — Implementation Handoff

> **Prerequisites — run this first:**
> All database schema changes (new `f_readiness_screen_grip` table, 9 phase-metric columns on CMJ/PPU/power_curve, `grip_z` column on score table) are owned by the **backend repo**. Before executing any step in this document, pass `BACKEND_READINESS_HANDOFF.md` to a Claude Code agent in the backend repo and confirm the migration has run successfully. The SQL in `db/migrations.sql` of this repo mirrors those changes for local reference and emergency re-apply via `init_db.py`, but the backend Prisma schema is the source of truth.

This document is a handoff for **Claude Code** to implement the next version of the readiness screen tracker. It does **not** include code changes; it describes exactly what to change, where, and why, so the work can be executed in a fresh session with full context.

The goal of v2 is to (a) refine the test battery based on what the athletes actually do during a screen, (b) add new derived metrics that are more sensitive to fatigue than what we currently track, (c) improve how we analyze and compare data day-to-day, and (d) surface a clearer picture on the dashboard for both the coach and the athlete.

---

## 1. Background — what is changing clinically and why

The readiness screen battery is being trimmed and one new test is being added:

**Tests being removed from active collection:**
- ASH **I** position
- ASH **T** position

These are being dropped because they have lower throwing-specific signal than the remaining two positions and the goal is to keep the screen short enough that athletes will actually complete it.

**Tests being kept:**
- ASH **Y** (lower trap / scap function — captures the weakest position, where compensation shows up first)
- ASH **IR90** (rotator cuff function in a throwing-specific arm position — most fatigue-sensitive shoulder test)
- **CMJ** — collected as **2 trials** (was previously variable; the protocol is now fixed at 2)
- **PPU** — collected as **2 trials** (same)

**New test being added:**
- **Grip strength** via handheld dynamometer. Standardized position: standing, elbow fully extended at the side, neutral wrist, dynamometer at the standard second handle position. **Three reps per hand**, max value recorded for each hand. Data is **entered manually** in the maintenance UI at ingestion time because the dynamometer is not exported through QTM or V3D.

**Schema-level decision:** Existing `f_readiness_screen_i` and `f_readiness_screen_t` tables and any rows in them are **preserved** for historical purposes. We just stop ingesting into them, stop scoring against them, and stop displaying them by default on the dashboard. This is safer than dropping data and matches the project's pattern of leaving legacy columns in place (see `MIGRATION_HANDOFF.md`).

---

## 2. Implementation summary (TL;DR for Claude Code)

At a high level, v2 requires:

1. One new table — `f_readiness_screen_grip` — plus a small set of column additions to the existing CMJ and PPU tables and the existing power-curve table for the new derived metrics (eccentric-phase, mRSI, contraction time).
2. Ingestion changes — stop discovering `i_data.txt` and `t_data.txt`, accept a grip-strength payload from the maintenance form, compute new derived metrics from the existing power-time data we already parse.
3. Scoring changes — drop I and T from the ISO metric list, add Y and IR90 only, add grip as a new metric group, add the new CMJ/PPU eccentric-phase and mRSI metrics, add intra-session CV tracking across the 2 trials.
4. Dashboard changes — add a grip-strength panel with L/R asymmetry, add a "movement strategy" panel for the new CMJ/PPU metrics, add a "today vs baseline" comparison view, add a metric-flag heatmap, add a same-session trial-consistency view.
5. Update tests in `tests/smoke_test.py` to cover the new derivations and the grip flow.

Concrete edits start in Section 3.

---

## 3. Data changes — what we collect, what we add

### 3.1 Files dropped from active ingestion

`ingestion/file_parsers.py::ASCII_FILES` currently includes `"I": "i_data.txt"` and `"T": "t_data.txt"`. These two keys should be **removed from active discovery**. Specifically:

- Update `ASCII_FILES` to contain only `{"Y": "y_data.txt", "IR90": "ir90_data.txt"}`.
- Update `discover_txt_files(output_dir)` to return only `Y` and `IR90` (this should follow automatically from the dict change).
- In `routes/maintenance.py` the `scan()` endpoint returns a `files` map — verify it now only contains `Y` and `IR90` entries for ASH.
- In `templates/maintenance.html` the file-discovery tile grid currently shows 6 tiles (I, Y, T, IR90, CMJ, PPU). Reduce to 4 tiles (Y, IR90, CMJ, PPU).
- In `ingestion/pipeline.py::run_ingestion()`, the loop that upserts ISO rows should now only target `f_readiness_screen_y` and `f_readiness_screen_ir90`. No writes should happen to `f_readiness_screen_i` or `f_readiness_screen_t` from this app going forward. Leave those upsert functions in place but unreachable, in case we ever want to backfill — but they should not be in the hot path.

**Do not drop or alter the `f_readiness_screen_i` and `f_readiness_screen_t` tables.** Historical rows must remain queryable.

### 3.2 New table — `f_readiness_screen_grip`

Grip strength is entered manually in the maintenance UI. One row per athlete-session.

```sql
CREATE TABLE IF NOT EXISTS public.f_readiness_screen_grip (
    id                  SERIAL PRIMARY KEY,
    athlete_uuid        VARCHAR(36) NOT NULL,
    session_date        DATE        NOT NULL,
    source_system       VARCHAR(64),
    source_athlete_id   VARCHAR(64),
    age_at_collection   NUMERIC,
    age_group           VARCHAR(32),
    left_kg             NUMERIC,
    right_kg            NUMERIC,
    avg_kg              NUMERIC,       -- (left_kg + right_kg) / 2, populated at insert
    max_kg              NUMERIC,       -- MAX(left_kg, right_kg), populated at insert
    asymmetry_pct       NUMERIC,       -- 100 * |L-R| / MAX(L,R), populated at insert
    dominant_hand       VARCHAR(8),    -- 'R' or 'L'; nullable. Optional, can be added later.
    entry_source        VARCHAR(16),   -- 'manual' for now; future: 'api', 'csv', etc.
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_grip_athlete_session UNIQUE (athlete_uuid, session_date)
);

CREATE INDEX IF NOT EXISTS idx_grip_athlete ON public.f_readiness_screen_grip(athlete_uuid);
CREATE INDEX IF NOT EXISTS idx_grip_session ON public.f_readiness_screen_grip(session_date);
```

Add this to `db/migrations.sql` and to the backend's Prisma schema. The migration is idempotent, matching the existing project pattern.

### 3.3 New columns on `f_readiness_screen_cmj` and `f_readiness_screen_ppu`

These store the new derived per-trial metrics. They sit on the CMJ/PPU tables rather than the power-curve table because they are scalar per-trial values, identical to the existing `peak_power_w`-style columns already on these tables.

```sql
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS contraction_time_s        DECIMAL,  -- onset of movement to takeoff
    ADD COLUMN IF NOT EXISTS eccentric_duration_s      DECIMAL,  -- start of unweighting to bottom of CM
    ADD COLUMN IF NOT EXISTS concentric_duration_s     DECIMAL,  -- bottom of CM to takeoff
    ADD COLUMN IF NOT EXISTS ecc_con_duration_ratio    DECIMAL,  -- eccentric_duration / concentric_duration
    ADD COLUMN IF NOT EXISTS eccentric_mean_power_w    DECIMAL,  -- mean power during eccentric (will be negative)
    ADD COLUMN IF NOT EXISTS eccentric_peak_power_w    DECIMAL,  -- most negative power value
    ADD COLUMN IF NOT EXISTS eccentric_auc_j           DECIMAL,  -- absolute work absorbed during eccentric
    ADD COLUMN IF NOT EXISTS concentric_auc_j          DECIMAL,  -- positive work during concentric
    ADD COLUMN IF NOT EXISTS mrsi                      DECIMAL;  -- jump_height_m / contraction_time_s

-- Same for PPU
ALTER TABLE public.f_readiness_screen_ppu
    ADD COLUMN IF NOT EXISTS contraction_time_s        DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_duration_s      DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_duration_s     DECIMAL,
    ADD COLUMN IF NOT EXISTS ecc_con_duration_ratio    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_mean_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_peak_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_auc_j           DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_auc_j          DECIMAL,
    ADD COLUMN IF NOT EXISTS mrsi                      DECIMAL;
```

Notes on these columns:
- **`mrsi` uses jump height in meters** for unit consistency (height / time = velocity-like). Make sure `parse_txt_file` already exposes the JH value in a known unit (currently `JH_IN` = inches); convert in the analysis layer.
- **Eccentric power values will be negative numbers** because the V3D-computed power signal is negative during energy absorption. Store them as-is (do not take absolute value at the DB layer); the analysis and dashboard layers handle sign awareness explicitly.

### 3.4 New columns on `f_readiness_screen_power_curve`

The power-curve table is the place to store the same phase metrics derived from the raw power-time signal (it already holds the curve-shape metrics). Mirror the same columns here so multi-trial drilldown can show phase data per trial:

```sql
ALTER TABLE public.f_readiness_screen_power_curve
    ADD COLUMN IF NOT EXISTS contraction_time_s        DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_duration_s      DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_duration_s     DECIMAL,
    ADD COLUMN IF NOT EXISTS ecc_con_duration_ratio    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_mean_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_peak_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_auc_j           DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_auc_j          DECIMAL,
    ADD COLUMN IF NOT EXISTS mrsi                      DECIMAL;
```

This duplication is intentional and matches how the existing v1 code stores per-trial scalars on both the CMJ/PPU table and the power_curve table (see `MIGRATION_HANDOFF.md` precedent).

### 3.5 Optional new column on `f_readiness_screen_score`

Add a `grip_z` column to capture the grip group's contribution to the composite, alongside the existing `cmj_z`, `ppu_z`, `iso_z`, `power_curve_z`:

```sql
ALTER TABLE public.f_readiness_screen_score
    ADD COLUMN IF NOT EXISTS grip_z NUMERIC;
```

---

## 4. Ingestion pipeline changes

### 4.1 `ingestion/file_parsers.py`

**4.1.1** Reduce `ASCII_FILES` to:
```python
ASCII_FILES = {
    "Y":    "y_data.txt",
    "IR90": "ir90_data.txt",
}
```

**4.1.2** No other changes needed in this file. The CMJ/PPU parser already handles per-trial files and we are not changing the format of those.

### 4.2 `ingestion/power_analysis.py`

This is where most of the new analytical work happens.

**4.2.1 Phase detection.** Add a new function `detect_phases(power_array, fs_hz)` that, given the power-time signal, identifies:

- `onset_idx` — index where movement begins. Use the first sample where `|power| > onset_threshold`, where `onset_threshold = max(5 W, 0.02 × peak_absolute_power)`. (CMJ standing-still has ~0 power; movement onset is when the signal departs baseline.)
- `bottom_idx` — index of the deepest countermovement, defined as the sample where cumulative impulse (integrated power, since impulse ∝ ∫F·v dt, but for our purposes the zero-crossing of the power signal from negative to positive marks the transition from eccentric energy absorption to concentric energy production). Simpler operationally: **last zero-crossing of the power signal before the global peak** marks the eccentric→concentric transition.
- `peak_idx` — index of max positive power (already computed elsewhere).
- `takeoff_idx` — for CMJ, the first sample after `peak_idx` where power returns to near-zero (athlete in flight; force plate sees only body weight or zero depending on V3D pipeline). For PPU the same logic applies at hand lift-off.

Return a dict: `{onset_idx, bottom_idx, peak_idx, takeoff_idx}`.

**4.2.2 Phase metrics.** Add `analyze_phase_metrics(power_array, fs_hz, jump_height_m=None)` that returns:

```python
{
    "contraction_time_s":      (takeoff_idx - onset_idx) / fs_hz,
    "eccentric_duration_s":    (bottom_idx - onset_idx) / fs_hz,
    "concentric_duration_s":   (takeoff_idx - bottom_idx) / fs_hz,
    "ecc_con_duration_ratio":  eccentric_duration_s / concentric_duration_s,
    "eccentric_mean_power_w":  mean(power[onset_idx:bottom_idx]),       # negative
    "eccentric_peak_power_w":  min(power[onset_idx:bottom_idx]),        # most negative
    "eccentric_auc_j":         abs(trapz(power[onset_idx:bottom_idx])) / fs_hz,
    "concentric_auc_j":        trapz(power[bottom_idx:takeoff_idx]) / fs_hz,
    "mrsi":                    jump_height_m / contraction_time_s if jump_height_m else None,
}
```

Edge cases:
- If `bottom_idx <= onset_idx + 5` (no real eccentric phase detected, e.g. PPU done from a still position): return all eccentric fields as `None`.
- If `concentric_duration_s == 0` (degenerate signal): set the ratio to `None`.
- All metrics should gracefully return `None` rather than raising on malformed signals — match the existing behavior of `analyze_power_curve_advanced` which returns `None` for invalid inputs.

**4.2.3 Update `analyze_power_curve_advanced`** to optionally also return the phase metrics dict so callers do not need a second pass over the signal. Signature change:

```python
def analyze_power_curve_advanced(power_array, fs_hz=1000.0, jump_height_m=None) -> Dict:
    # existing logic returns base + advanced metrics
    # NEW: if power signal has clear phases, also merges in analyze_phase_metrics(...)
```

**4.2.4 `load_power_txt`** does not need changes.

### 4.3 `ingestion/pipeline.py`

**4.3.1** When parsing CMJ/PPU rows, convert the parsed `JH_IN` (inches) to meters using `ingestion/units.py` (add an `inches_to_meters` helper if not present — there is currently a `meters_to_inches`; add the inverse for clarity). Pass that meters value into `analyze_power_curve_advanced(..., jump_height_m=jh_m)`.

**4.3.2** When persisting CMJ/PPU rows, include the new 9 columns from Section 3.3 in the upsert.

**4.3.3** When persisting power_curve rows, include the new 9 columns from Section 3.4 in the upsert. `_persist_power_curve` needs the matching column list extension.

**4.3.4 Grip ingestion path.** The grip data does not come from a file. Modify `run_ingestion()` to accept an optional `grip_payload` parameter:

```python
def run_ingestion(
    output_dir: str,
    power_dir: Optional[str] = None,
    fs_hz: float = 1000.0,
    log: Optional[Callable] = None,
    athlete_uuid_override: Optional[str] = None,
    cancel_event: Optional[threading.Event] = None,
    grip_payload: Optional[Dict] = None,   # NEW
) -> Dict:
```

`grip_payload` shape:
```python
{
    "left_kg": float,
    "right_kg": float,
    "dominant_hand": "R" | "L" | None,
    "notes": str | None,
    # session_date and athlete_uuid are resolved the same way as CMJ/PPU rows
}
```

When provided, after the athlete UUID and session_date are resolved, upsert a row into `f_readiness_screen_grip` using the existing `_upsert` helper. Compute `avg_kg`, `max_kg`, and `asymmetry_pct` in Python before writing (do not rely on a database trigger — none exists in the project).

**4.3.5** After all upserts, the scoring trigger (`score_session(...)`) already covers every athlete-session that was touched. With the new `grip_z` group added in Section 5, this will pick up grip data automatically. No changes needed to the trigger loop itself.

### 4.4 `routes/maintenance.py`

**4.4.1** The `/api/run` endpoint receives `{output_dir, power_dir, fs_hz, athlete_uuid}` today. Extend it to also accept `{grip: {left_kg, right_kg, dominant_hand, notes}}`. Pass through to `run_ingestion(..., grip_payload=...)`.

**4.4.2** The `/api/scan` endpoint now returns only Y, IR90, CMJ, PPU file discovery. Grip is not file-based so it is not included here.

### 4.5 `templates/maintenance.html` and `static/js/maintenance.js`

**4.5.1** In the existing 3-card flow (athlete picker → folder scan → run), add a fourth card or extend the run card with a **Grip Strength** sub-form with four inputs:
- Left hand (kg)
- Right hand (kg)
- Dominant hand (dropdown: R / L / Not specified)
- Notes (optional text)

UX note: the dynamometer Joey uses likely reports in kg or lbs; for simplicity standardize on kg. If a future device reports lbs, conversion happens client-side before submit. Add a small unit toggle on the input group (kg/lbs) that defaults to kg and converts on the fly.

**4.5.2** In `maintenance.js`, when "Run" is pressed, include the grip values in the POST body. If both fields are blank, omit the `grip` key (treat as "not collected today"). If only one is filled, surface a warning before submit.

**4.5.3** Remove the I and T tiles from the file-discovery grid. Keep them as a collapsed "legacy" disclosure section at the bottom of the maintenance page if you want to preserve the visual hint that those tests existed, but they should not be part of the active workflow.

---

## 5. Scoring updates — `ingestion/scoring.py`

The current scoring is solid and most of the structure stays. What changes:

### 5.1 Metric list edits

**ISO_METRICS** — remove the I and T tuples. Final list:

```python
ISO_METRICS = [
    ("f_readiness_screen_y",    "max_force",   +1),
    ("f_readiness_screen_y",    "time_to_max", -1),
    ("f_readiness_screen_ir90", "max_force",   +1),
    ("f_readiness_screen_ir90", "time_to_max", -1),
]
```

**CMJ_METRICS** — append the new derived metrics. mRSI gets the heaviest implicit weight by being included; eccentric mean power and ecc:con duration ratio capture strategy:

```python
CMJ_METRICS = [
    # existing
    ("f_readiness_screen_cmj", "jump_height",   +1),
    ("f_readiness_screen_cmj", "pp_w_per_kg",   +1),
    ("f_readiness_screen_cmj", "force_at_pp",   +1),
    ("f_readiness_screen_cmj", "vel_at_pp",     +1),
    # new
    ("f_readiness_screen_cmj", "mrsi",                   +1),
    ("f_readiness_screen_cmj", "contraction_time_s",     -1),  # longer = fatigued
    ("f_readiness_screen_cmj", "ecc_con_duration_ratio", -1),  # rises with fatigue (braking gets longer relative to push)
    ("f_readiness_screen_cmj", "eccentric_mean_power_w", -1),  # this is negative; more negative (faster braking) = better, so sign = -1
]
```

Sign note on `eccentric_mean_power_w`: the values stored are negative. A larger-magnitude negative (e.g., -1500 W vs -1000 W) means the athlete absorbed energy faster, which is better. Because the raw number is more negative on a good day, sign = -1 inverts that so the signed z-score is positive on a good day.

**PPU_METRICS** — same additions, replacing the CMJ table name with the PPU table name. Note that `eccentric_mean_power_w` may be `None` for PPU if no real eccentric phase is detected (push-up done from a still position rather than dropping into a catch); the scoring code already skips `None` values, so this is fine.

**GRIP_METRICS** — new group:

```python
GRIP_METRICS = [
    ("f_readiness_screen_grip", "left_kg",       +1),
    ("f_readiness_screen_grip", "right_kg",      +1),
    ("f_readiness_screen_grip", "max_kg",        +1),
    ("f_readiness_screen_grip", "asymmetry_pct", -1),  # higher asymmetry = worse
]
```

**POWER_CURVE_METRICS** — unchanged.

### 5.2 Group bucket changes

In `compute_score_for_session`, add a `"grip"` bucket to `group_zs` and include it in the per-group mean computation. Persist as `grip_z` on the score row (Section 3.5). The composite score formula does not change — it remains `mean(all_zs)` over every metric that has a valid z.

### 5.3 Intra-session consistency tracking

This is **new functionality**, not just a refactor. With 2 CMJ trials and 2 PPU trials per session, intra-session variability is a meaningful readiness signal — when athletes are cooked, the two trials look more different from each other than usual.

Add a helper `compute_intra_session_cv(athlete_uuid, session_date)` that, for each of CMJ jump_height, CMJ mrsi, CMJ peak_power_w, PPU jump_height, PPU mrsi, PPU peak_power_w:

1. Loads the 2 trials for that day.
2. Computes `cv = sd([trial1, trial2]) / mean([trial1, trial2])`.
3. Compares that CV to the athlete's rolling baseline CV (same 28-day window) — flag if today's CV is >2× baseline CV.
4. Returns a dict of per-metric CVs and flags.

Persist this in `flags_json` on the score row under a new `intra_session` key:

```json
{
  "per_metric": { ... existing ... },
  "intra_session": {
    "cmj.jump_height":  {"today_cv": 0.012, "baseline_mean_cv": 0.008, "flag": "stable"},
    "cmj.mrsi":         {"today_cv": 0.083, "baseline_mean_cv": 0.020, "flag": "elevated"},
    ...
  }
}
```

This data feeds the new dashboard "Trial Consistency" panel (Section 6.5).

### 5.4 Baseline window strategy

The current 28-day rolling baseline is reasonable but we should expose **two views** to the user:

- **Short-term baseline** (default 28 days) — what's already there. Sensitive to current training block.
- **Long-term baseline** (e.g., 90 days) — for trend stability and detecting drift independent of block-to-block fluctuation.

Add `baseline_window_days` as a query param to `/api/dashboard/athlete/<uuid>` (default 28) so the dashboard can flip between them. The scoring at ingest time keeps the 28-day default for the persisted score. The dashboard recomputes a non-persistent secondary view when the user changes the window.

This is a meaningful add for the dashboard but not strictly required for the first pass. Mark it **Phase 2** in the implementation order (Section 7).

### 5.5 Minimum history and "insufficient history" handling

`MIN_HISTORY = 3` is fine. **Add explicit messaging** in the score payload: when a metric is skipped due to insufficient history, the existing `flags_json.per_metric` should include an entry with `flag = "insufficient_history"` and `n_history = <actual count>`. This lets the dashboard show which metrics are still ramping up — important early on for grip strength specifically, which will have zero history at rollout.

### 5.6 Per-movement composite scores (already present, just make sure they remain accurate)

The existing `cmj_z`, `ppu_z`, `iso_z`, `power_curve_z` per-movement scores stay. The new `grip_z` joins them. The dashboard should display each as a small sub-gauge or a stat tile next to the overall composite, which is the current pattern — just add a 5th tile for grip.

---

## 6. Dashboard / display changes

The current dashboard has good bones: composite score gauge, ISO trends, CMJ jump height time series + force-velocity scatter, PPU same, power-curve trends. What we're adding moves the needle from "metrics over time" to "what does this athlete's day look like and is it concerning."

### 6.1 Drop I and T traces from the ISO panel

In `static/js/dashboard.js`, the ISO line chart currently renders 4 traces (I, Y, T, IR90). Reduce to 2 traces (Y, IR90). The data payload from `/api/dashboard/athlete/<uuid>` will still include I and T data for historical rows but the chart should not plot them by default. Add a "Show historical (I, T)" toggle below the chart that re-enables them. This preserves access to old data without cluttering the everyday view.

### 6.2 New: Grip Strength panel

Insert as Row 1.5 between ISO and CMJ. Contents:

- Time-series line chart with 2 traces (left_kg, right_kg) plus a third trace for `asymmetry_pct` on a secondary Y axis.
- A 4-tile stat grid showing **today's** left, right, max, and asymmetry %, each with a delta vs the rolling 28-day mean.
- Asymmetry color logic: `<10%` green, `10–15%` yellow, `>15%` red. (These thresholds are roughly in line with the literature on bilateral grip asymmetry in athletic populations; the user can tune later.)

### 6.3 New: Movement Strategy panel

Insert as Row 2.5 between CMJ and PPU (or expand the CMJ row into a 2x2 grid — designer's call). Contents for CMJ and PPU each:

- **mRSI trend** — line chart over time. This is the single most informative new metric; give it prime real estate.
- **Contraction time trend** — line chart over time. Annotate the athlete's baseline mean as a horizontal reference line.
- **Eccentric:Concentric duration ratio trend** — line chart over time, with a horizontal reference line at the athlete's baseline mean.
- **Eccentric mean power trend** — line chart over time (recall: values are negative).

These four small charts together tell the strategy story: a fatigued athlete may hit the same jump height but with a longer contraction time, slower braking, and a worse mRSI.

### 6.4 New: Today vs Baseline comparison view

A single visual in the score-card row showing **today's value for each of the ~15 contributing metrics** plotted against the athlete's baseline distribution. Two design options:

- **Option A (recommended):** Horizontal bar chart with one row per metric. Bar shows today's z-score; color-coded by flag (rise/stable/drop). Tooltip on hover shows raw value, baseline mean, baseline SD, n_history.
- **Option B:** Radar chart with one axis per metric group (CMJ, PPU, ISO, Power Curve, Grip) showing today's z-score for that group. Smaller information density but easier for athletes to scan.

Recommend implementing Option A first; the athlete-facing summary can be a polished version of Option B later.

This view directly answers the question "what's actually flagged today?" — currently the user has to click into individual charts to see that.

### 6.5 New: Trial Consistency panel (uses Section 5.3 data)

Small panel that for each of CMJ and PPU shows:

- Today's 2 trials side by side as bar pairs for jump_height, mrsi, peak_power_w.
- The intra-session CV value with the flag color (stable / elevated).
- A 14-day sparkline of intra-session CV trend.

This is the most underused signal in current readiness practice. It catches problems even when the means look fine.

### 6.6 New: Metric Flag Heatmap

A wide, dense view at the bottom of the dashboard showing the last 14 days × all ~15 metrics, with each cell colored by that day's per-metric flag (`rise` / `stable` / `drop` / `insufficient_history`). Read horizontally to see "which metrics have been dropping" — read vertically to see "what was today's overall pattern." This is the single best at-a-glance fatigue diagnostic.

Implementation note: the flag data is already in `flags_json.per_metric` per session. The dashboard just needs to fan it out across the date axis.

### 6.7 Update score gauge legend

The existing 3-band system (READY / CAUTION / FATIGUED at >60 / 40–60 / <40) is fine but the labels could be more actionable. Suggest:

- READY → "Train" (green)
- CAUTION → "Monitor" (yellow)
- FATIGUED → "Modify" (red)

These map to actual decisions ("what do I do today?") rather than describing the athlete. Optional change — purely cosmetic.

### 6.8 Update `/api/dashboard/athlete/<uuid>` payload

Add to the response:

```json
{
  ...existing...
  "grip": {
    "timeseries": [{"date": "...", "left_kg": ..., "right_kg": ..., "asymmetry_pct": ..., "max_kg": ...}, ...]
  },
  "movement_strategy": {
    "cmj": [{"date": "...", "mrsi": ..., "contraction_time_s": ..., "ecc_con_duration_ratio": ..., "eccentric_mean_power_w": ...}, ...],
    "ppu": [...]
  },
  "intra_session": {
    "cmj": [{"date": "...", "trials": [{"jump_height": ..., "mrsi": ...}, {"jump_height": ..., "mrsi": ...}], "cv": {...}}, ...],
    "ppu": [...]
  },
  "today_vs_baseline": [
    {"metric": "cmj.mrsi", "group": "cmj", "today": 0.42, "mean": 0.45, "sd": 0.02, "z": -1.5, "flag": "drop", "n_history": 12, "label": "CMJ mRSI"},
    ...
  ],
  "flag_heatmap": {
    "dates": ["2026-05-14", ..., "2026-05-28"],
    "metrics": ["cmj.jump_height", "cmj.mrsi", ..., "grip.asymmetry_pct"],
    "cells": [[ "rise", "stable", ... ], ...]   // 2D array, dates × metrics
  }
}
```

`today_vs_baseline` is essentially the contents of `flags_json.per_metric` for the latest session, restructured for ease of plotting.

`flag_heatmap` is the same data fanned out across the 14-day window.

---

## 7. Implementation order — suggested sequence for Claude Code

Each step is independently shippable. Run smoke tests after each.

1. **Schema migration (backend)** — Confirm `BACKEND_READINESS_HANDOFF.md` has been executed in the backend repo and all new columns/tables exist. Verify with: `SELECT column_name FROM information_schema.columns WHERE table_name = 'f_readiness_screen_grip';` — should return the expected columns. The SQL is also mirrored in `db/migrations.sql` (re-runnable via `python init_db.py` if needed for a clean local environment).

2. **Drop I and T from active ingestion** (Section 4.1). Verify the existing I and T tables and rows are untouched. Verify `/api/scan` no longer returns these. Run smoke test.

3. **Phase detection and new derived metrics** (Section 4.2). Add `detect_phases` and `analyze_phase_metrics` to `power_analysis.py`. Wire into `analyze_power_curve_advanced`. Add a unit test in `smoke_test.py` that uses a synthetic CMJ-shaped power signal and verifies the phase indices are detected within tolerance.

4. **Pipeline write-through** (Section 4.3). Wire new metric values into the CMJ/PPU upsert and power_curve upsert. Add `inches_to_meters` to `units.py`. Run end-to-end against a real session folder and verify rows have populated values.

5. **Grip ingestion** (Section 4.3.4 + 4.4 + 4.5). New table is already created in step 1; this step adds the maintenance form, the `/api/run` payload extension, and the upsert path. Test with a manual entry of 50 kg / 48 kg.

6. **Scoring updates** (Section 5). Drop I and T from ISO_METRICS, append CMJ/PPU new metrics, add GRIP_METRICS, add `grip_z` to score payload, add intra-session CV computation, expand `flags_json`. Run smoke test for scoring math.

7. **Dashboard payload expansion** (Section 6.8). Update the `/api/dashboard/athlete/<uuid>` endpoint to return the new structures.

8. **Dashboard frontend — ISO trim** (Section 6.1). Smallest visible UI change first.

9. **Dashboard frontend — Grip panel** (Section 6.2).

10. **Dashboard frontend — Today vs Baseline** (Section 6.4). Biggest impact-per-effort ratio; do this before the strategy panel.

11. **Dashboard frontend — Movement Strategy panel** (Section 6.3).

12. **Dashboard frontend — Trial Consistency panel** (Section 6.5).

13. **Dashboard frontend — Metric Flag Heatmap** (Section 6.6).

14. **Phase 2: Configurable baseline window** (Section 5.4). Implement the dashboard control + non-persistent recomputation.

15. **Documentation refresh** — Update `README.md` and `HANDOFF.md` to reflect v2 state. Move this file to `archive/` or rename to `HANDOFF_V2_CHANGELOG.md` so it remains as history.

---

## 8. Tests to add — `tests/smoke_test.py`

Add the following pure-Python checks (no DB required, in the existing style):

- **Phase detection on synthetic signal.** Generate a power-time array shaped like a typical CMJ (200ms zero baseline, 300ms negative-going dip to -1500 W, 200ms positive-going rise to +3500 W peak, 100ms decay to zero, 200ms zero flight). Assert that `detect_phases` returns onset, bottom, peak, and takeoff indices within ±20 samples (20 ms at 1000 Hz) of the known values.

- **Phase metrics math.** Same synthetic signal. Assert `eccentric_duration_s ≈ 0.3`, `concentric_duration_s ≈ 0.3`, `ecc_con_duration_ratio ≈ 1.0`, `eccentric_mean_power_w ≈ -750` (rough triangle approximation, so loose tolerance), `mrsi ≈ jump_height_m / 0.6`.

- **Grip metric derivations.** Given `left_kg=50, right_kg=45`, assert `avg_kg == 47.5`, `max_kg == 50`, `asymmetry_pct == 10.0`.

- **Sign correctness for new metrics.** Inject a value into the scoring engine for each new metric (mrsi, contraction_time, ecc_con_ratio, eccentric_mean_power, asymmetry_pct) and verify that improving the metric (e.g., mrsi rising, contraction_time shrinking, asymmetry shrinking) produces a positive signed z-score, and degrading it produces a negative one.

- **Intra-session CV.** Two CMJ trials with jump_height 15.0 and 15.6 inches → CV of 0.0276 (within tolerance). Assert the math.

- **Insufficient history flag.** New athlete with 0 historical sessions — assert every metric is flagged `insufficient_history` and composite is `None` with band `INSUFFICIENT_HISTORY`. (Existing test may already cover this; just verify it still passes with the new metric list.)

---

## 9. Open questions / decisions still to make

These are not blockers but should be discussed with the user (Joey) before or during implementation:

1. **Unit for grip strength input.** Default to kg in the UI. Confirm whether the dynamometer Joey uses reports in kg, lbs, or both. If lbs is the default device output, swap the form default. Storage is in kg either way.

2. **Asymmetry thresholds.** The 10% / 15% green/yellow/red thresholds in Section 6.2 are a reasonable starting point but are not from a single canonical source. Confirm with Joey whether he prefers other thresholds or wants to set them per-athlete based on baseline.

3. **Dominant hand entry.** Optional field. If Joey wants this to be required (e.g., to compute "dominant vs non-dominant asymmetry" instead of just absolute asymmetry), make the field required and add a `dominant_kg` / `nondominant_kg` set of computed columns. Defer until Joey confirms.

4. **Baseline window default.** 28 days is reasonable in-season. Off-season blocks may want 14 days; long-term tracking may want 90. Confirm whether the default should stay at 28 or be tunable globally via `.env`.

5. **mRSI definition.** The formula is `jump_height / contraction_time` where contraction time spans onset to takeoff. Some literature defines it as onset to peak velocity. The onset-to-takeoff definition is the most common and what's specified here, but flag this in the test commentary so it's clear.

6. **Eccentric phase definition for PPU.** ~~Confirm whether Joey's athletes do drop-catch PPU or still-start PPU.~~ **RESOLVED:** Athletes perform PPU from a still plank position (no drop-catch). The power signal starts at baseline, goes straight into the concentric push, peaks, and returns to baseline. Implementation decisions:
   - `detect_phases` accepts `movement_type="PPU"` and sets `bottom_idx = onset_idx` (no eccentric phase).
   - All eccentric metrics (`eccentric_duration_s`, `eccentric_mean_power_w`, `eccentric_peak_power_w`, `eccentric_auc_j`, `ecc_con_duration_ratio`) are `None` for all PPU rows.
   - Eccentric columns on the PPU and power_curve tables are **kept** for forward-compatibility in case the protocol ever changes.
   - `PPU_METRICS` includes only `mrsi` and `contraction_time_s` (not eccentric metrics) — they will always be `None` and would be no-ops in scoring.
   - The PPU side of the Movement Strategy dashboard panel shows only mRSI and Contraction Time charts (not ecc:con ratio or eccentric mean power).

7. **Should `f_readiness_screen_score.grip_z` be backfilled** for old sessions where no grip data exists? Recommendation: no. Leave it `NULL` for pre-rollout sessions. The dashboard should render "no data" rather than zero.

---

## 10. Files that will be touched (summary table)

| File | Change |
|------|--------|
| `db/migrations.sql` | + `f_readiness_screen_grip` table; + 9 columns × 2 tables (CMJ, PPU); + 9 columns on `f_readiness_screen_power_curve`; + `grip_z` column on score table |
| `MIGRATION_HANDOFF_V2.md` (new) | SQL for backend's Prisma to mirror the above |
| `ingestion/file_parsers.py` | Remove I and T from `ASCII_FILES` |
| `ingestion/units.py` | Add `inches_to_meters` |
| `ingestion/power_analysis.py` | Add `detect_phases`, `analyze_phase_metrics`; extend `analyze_power_curve_advanced` |
| `ingestion/pipeline.py` | Pass JH in meters into power analyzer; persist new CMJ/PPU/power_curve columns; accept `grip_payload`; upsert grip row |
| `ingestion/scoring.py` | Update ISO_METRICS, CMJ_METRICS, PPU_METRICS, add GRIP_METRICS, add `grip_z` to payload, add intra-session CV, expand `flags_json` |
| `routes/maintenance.py` | Accept grip payload in `/api/run` |
| `templates/maintenance.html` | Remove I/T tiles; add grip strength sub-form |
| `static/js/maintenance.js` | POST grip payload with run; tile grid update |
| `routes/dashboard.py` | Expand `/api/dashboard/athlete/<uuid>` payload (grip, movement_strategy, intra_session, today_vs_baseline, flag_heatmap) |
| `templates/dashboard.html` | Add grip panel, movement strategy panel, today-vs-baseline view, trial consistency panel, flag heatmap |
| `static/js/dashboard.js` | Render the new panels; remove I/T traces from ISO chart by default |
| `static/css/dashboard.css` | Style new panels and heatmap |
| `tests/smoke_test.py` | Add Section 8 tests |
| `README.md`, `HANDOFF.md` | Refresh after implementation |

---

## 11. Reference — clinical rationale (for the README update later)

For the README, the v2 section should briefly explain (so anyone picking this up understands the why):

- ASH I and T were dropped because they have less throwing-specific signal than Y (lower-trap function) and IR90 (cuff function in a throwing arm position). Trimming the battery keeps athlete compliance high.
- Grip strength was added because it's a 30-second test with very high reliability that correlates well with systemic neuromuscular fatigue. Bilateral asymmetry trend is the most useful signal.
- Eccentric phase metrics, mRSI, and contraction time were added because jump height alone is one of the least fatigue-sensitive CMJ metrics. Athletes can hit the same height with worse strategy. The new metrics catch strategy changes before output drops.
- Intra-session CV was added because trial-to-trial variability is one of the earliest signs of neuromuscular fatigue. With only 2 trials per movement we can still compute it; with more trials it would be more sensitive.
- The "Today vs Baseline" and "Metric Flag Heatmap" views were added because the v1 dashboard was strong at "show me trends over time" but weak at "tell me what's flagged right now and why" — which is the actual decision the coach is making in the moment.

---

End of handoff.
