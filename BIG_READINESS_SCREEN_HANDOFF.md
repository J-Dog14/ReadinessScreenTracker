# Readiness Screen — Full Intake & Analysis Handoff

This document describes the complete data flow from output files on disk to database rows, covering every file involved, every transformation applied, and every metric computed. It is written so a backend developer can replicate the same logic precisely.

---

## 1. Overview

The Athletic Screen 2.0 software writes a folder of `.txt` files after each testing session. The readiness screen pipeline:

1. Discovers which files are present
2. Filters to today's session (or most recent)
3. Parses each file → extracts athlete name, date, and metric values
4. Resolves athlete UUID from the warehouse (`analytics.d_athletes`)
5. Upserts one row per trial into the appropriate fact table
6. Runs power-curve analysis (`*_Power.txt`) and force analysis (`*_Force.txt`) for CMJ/PPU
7. Writes per-trial power-curve shape metrics to `f_readiness_screen_power_curve`
8. Upserts grip strength (from manual UI entry, not a file)
9. Computes composite readiness score → upserts into `f_readiness_screen_score`

---

## 2. Input File Types

All files live in a single **output folder** (configurable; default `D:/Athletic Screen 2.0/Output Files/Readiness Output Files`).

### 2a. ISO files (static filenames)

| File | Movement | Table |
|------|----------|-------|
| `y_data.txt` | Y | `f_readiness_screen_y` |
| `ir90_data.txt` | IR90 | `f_readiness_screen_ir90` |

Legacy files (`i_data.txt`, `t_data.txt`) are no longer ingested. Historical rows in `f_readiness_screen_i` and `f_readiness_screen_t` remain queryable.

**Format:** Tab-separated. First line = path containing athlete name and date. Rows after the header contain numeric data. Column order (extracted by position):

```
Max_Force | Max_Force_Norm | Avg_Force | Avg_Force_Norm | Time_to_Max
```

### 2b. CMJ/PPU summary files (dynamic filenames)

Pattern: `CMJ1.txt`, `CMJ2.txt`, `PPU1.txt`, `PPU2.txt`, etc.

Discovery rule: any `.txt` file whose name starts with `CMJ` or `PPU` (case-insensitive), excluding:
- Files ending in `_Power.txt`
- Files ending in `_Force.txt`
- Files containing `_data` in the name (legacy consolidated format)

**Format:** Tab-separated, Athletic Screen style. 5-column data row:

```
JH_IN | PP_FORCEPLATE | Force_at_PP | Vel_at_PP | PP_W_per_kg
```

- `JH_IN` — jump height in inches
- `PP_FORCEPLATE` — peak power from force plate (W)
- `Force_at_PP` — ground reaction force at moment of peak power (N)
- `Vel_at_PP` — COM velocity at moment of peak power (m/s)
- `PP_W_per_kg` — peak power normalized to body mass (W/kg)

### 2c. Power time-series files

Pattern: `CMJ1_Power.txt`, `CMJ2_Power.txt`, `PPU1_Power.txt`, `PPU2_Power.txt`, etc.

**Format:** Same header structure as summary files. Data rows: `ITEM<tab>X` where X is instantaneous power in Watts at 1000 Hz. Metric: `PowZ` (vertical power). **Only the concentric push-off phase is captured** — the signal is all-positive.

Used for: power-curve shape analysis only. Phase metrics (eccentric, contraction time, mRSI) require the Force file.

### 2d. Force time-series files

Pattern: `CMJ1_Force.txt`, `CMJ2_Force.txt`, `PPU1_Force.txt`, `PPU2_Force.txt`, etc.

**Format:** Same tab-separated format. Metric: `FP3_Mag` (force plate 3 magnitude, Newtons, 1000 Hz).

**Signal shape (both CMJ and PPU):**
1. Resting phase (~first 50–100 samples): steady baseline (athlete standing / hands-on plate)
2. Eccentric loading phase: force dips below resting baseline
3. Concentric push-off phase: force rises to peak (typically 2–3× body weight for CMJ, 1.5× upper-body weight for PPU)
4. Flight phase: force drops to near-zero (hands/feet leave plate)

The full trial including the eccentric dip is captured. This is the primary source for all phase metrics.

### 2e. Session.xml (optional)

`Session.xml` in the output folder provides session-level metadata (athlete name, gender). Used to default the `gender` field when not determinable from the movement files. If absent, gender defaults to `"Male"`.

---

## 3. File Discovery & Date Filtering

**Source file:** `ingestion/file_parsers.py`

```
discover_txt_files(output_dir)       → {movement: file_path} for ISO files
discover_cmj_ppu_trials(output_dir)  → [{movement_type, trial_name, file_path}, ...]
```

**Date filtering:** Each file's first line contains the path to the C3D source, which includes the session date (format `YYYY-MM-DD`). The pipeline peeks the first line of each discovered file, extracts the date, and processes only:
- Files from **today** if any exist
- Otherwise files from the **most recent date** found
- Otherwise all files (fallback)

---

## 4. Athlete Resolution

**Source file:** `ingestion/athlete_manager.py`

The athlete name is extracted from the file path in line 1: the segment immediately after `\Data\` in the path (e.g., `D:\...\Data\KOEN KARSA_KK\...` → `"KOEN KARSA_KK"`).

Resolution uses **fuzzy matching** against `analytics.d_athletes` (SequenceMatcher, case-insensitive, strips punctuation and trailing initials). The pipeline **never creates athletes** — if no match is found with similarity ≥ 80%, the file is skipped and an error is logged.

The `source_athlete_id` stored in every fact row is the athlete's display name **with trailing 2–3 character uppercase initials stripped** (e.g., `"KOEN KARSA KK"` → `"KOEN KARSA"`). This is done by `extract_source_athlete_id(name)`.

---

## 5. ISO File Ingestion (Y, IR90)

**Tables:** `f_readiness_screen_y`, `f_readiness_screen_ir90`

**Upsert key:** `(athlete_uuid, session_date)` — one row per athlete per session.

**Columns written:**

| Column | Source |
|--------|--------|
| `athlete_uuid` | resolved from athlete_manager |
| `session_date` | extracted from file line 1 |
| `source_system` | `"readiness_screen"` (hardcoded) |
| `source_athlete_id` | stripped athlete name |
| `age_at_collection` | computed from DOB at time of session |
| `age_group` | `"youth"` / `"adult"` / etc. |
| `avg_force` | column 3 of data row |
| `avg_force_norm` | column 4 |
| `max_force` | column 1 |
| `max_force_norm` | column 2 |
| `time_to_max` | column 5 |

---

## 6. CMJ/PPU Trial Ingestion

**Tables:** `f_readiness_screen_cmj`, `f_readiness_screen_ppu`

**Upsert key:** `(athlete_uuid, session_date, trial_name)` — one row per trial per session (NULL-safe via `IS NOT DISTINCT FROM`).

### 6a. Summary data (from CMJ1.txt)

| Column | Source |
|--------|--------|
| `athlete_uuid` | resolved |
| `session_date` | from file |
| `source_system` | `"readiness_screen"` |
| `source_athlete_id` | stripped name |
| `trial_name` | filename without extension (`"CMJ1"`, `"PPU2"`, etc.) |
| `trial_id` | integer extracted from trial_name suffix (`CMJ1` → `1`) |
| `age_at_collection` | computed |
| `age_group` | computed |
| `jump_height` | `JH_IN` — jump height in inches |
| `peak_power` | `Peak_Power` — from Power.txt peak if available |
| `peak_force` | always NULL (not in readiness export) |
| `pp_w_per_kg` | `PP_W_per_kg` |
| `pp_forceplate` | `PP_FORCEPLATE` |
| `force_at_pp` | `Force_at_PP` |
| `vel_at_pp` | `Vel_at_PP` |

### 6b. Power curve metrics (from CMJ1_Power.txt)

The power signal is loaded at 1000 Hz and passed to `analyze_power_curve_advanced()`.

**Source file:** `ingestion/power_analysis.py` → `analyze_power_curve_advanced()`

These 13 columns are written inline to the CMJ/PPU row:

| Column | Description |
|--------|-------------|
| `peak_power_w` | global maximum of power signal (W) |
| `time_to_peak_s` | time from signal onset to peak (s) |
| `rpd_max_w_per_s` | peak rate of power development (max dP/dt) |
| `time_to_rpd_max_s` | time to peak RPD |
| `rise_time_10_90_s` | time for power to rise from 10% to 90% of peak |
| `fwhm_s` | full-width-at-half-maximum of the power curve |
| `auc_j` | total area under the power curve (joules) |
| `work_early_pct` | % of total work done before the peak |
| `decay_90_10_s` | time for power to fall from 90% to 10% post-peak |
| `t_com_norm_0to1` | normalized time of centre-of-mass (0=onset, 1=peak) |
| `skewness` | statistical skewness of the power curve |
| `kurtosis` | statistical kurtosis |
| `spectral_centroid_hz` | frequency-weighted roughness of the curve |

These same metrics are also written to `f_readiness_screen_power_curve` (one row per trial — see Section 7).

### 6c. Phase + force metrics (from CMJ1_Force.txt)

**Source file:** `ingestion/power_analysis.py` → `analyze_phase_metrics_from_force()`

If a `{trial_name}_Force.txt` exists, this analysis runs and **overrides** the phase metrics that would have come from the power file (which cannot detect the eccentric phase).

#### Body weight pre-estimation

Before the CMJ/PPU trial loop, the pipeline scans for the first available CMJ Force file and estimates full body weight from the first 50 ms of quiet standing:

```
BW_N = mean(force_data[0:50])   # first 50 samples at 1000 Hz
mass_kg = BW_N / 9.81
```

This value is passed as `body_weight_n` to all subsequent force analyses (both CMJ and PPU) and is used solely for the `peak_grf_bw_ratio` normalization.

#### Phase detection algorithm

Applied identically to both CMJ and PPU.

```
signal_rest_n = mean(F[0:50])           # resting baseline from THIS file's first 50 ms
mass_kg       = signal_rest_n / 9.81    # correct mass per movement (full body for CMJ, upper body for PPU)
v             = cumsum(F - signal_rest_n) / (mass_kg × fs_hz)   # COM velocity (m/s)
P             = F × v                    # signed instantaneous power (W)
```

**Phase boundaries:**
1. **Eccentric start** (`ecc_start_idx`): first sample where `v < -0.02 m/s` (velocity threshold chosen to catch PPU's shallower eccentric dip ~-0.05 m/s while rejecting resting noise ~0.0004 m/s)
2. **Bottom** (`bottom_idx`): first negative→positive velocity zero-crossing between `ecc_start_idx` and peak power — marks eccentric→concentric transition
3. **Takeoff** (`takeoff_idx`): first sample after peak power where `F < 0.05 × signal_rest_n` — marks when hands/feet leave the plate

If no eccentric start is detected, or if `bottom_idx ≤ ecc_start_idx + 5`, all metrics return `None`.

#### 9 PHASE_COLS computed

| Column | Formula | CMJ | PPU |
|--------|---------|-----|-----|
| `contraction_time_s` | `(takeoff - ecc_start) / fs` | ✓ | ✓ |
| `eccentric_duration_s` | `(bottom - ecc_start) / fs` | ✓ | ✓ |
| `concentric_duration_s` | `(takeoff - bottom) / fs` | ✓ | ✓ |
| `ecc_con_duration_ratio` | `eccentric_duration / concentric_duration` | ✓ | ✓ |
| `eccentric_mean_power_w` | `mean(P[ecc_start:bottom])` — will be negative | ✓ | ✓ |
| `eccentric_peak_power_w` | `min(P[ecc_start:bottom])` — most negative | ✓ | ✓ |
| `eccentric_auc_j` | `abs(trapz(P[ecc_start:bottom], dx=1/fs))` | ✓ | ✓ |
| `concentric_auc_j` | `trapz(P[bottom:takeoff], dx=1/fs)` | ✓ | ✓ |
| `mrsi` | `jump_height_m / contraction_time_s` | ✓ | ✓ |

`jump_height_m` is converted from `JH_IN` (inches) via `× 0.0254`.

`eccentric_*` columns are `None` only if no clear eccentric phase is detected (i.e., `bottom_idx ≤ ecc_start_idx + 5`).

#### 4 FORCE_COLS computed

| Column | Formula | Notes |
|--------|---------|-------|
| `peak_grf_n` | `max(F)` | absolute peak GRF in Newtons |
| `peak_grf_bw_ratio` | `peak_grf_n / body_weight_n` | uses full BW (from CMJ pre-estimation) for both CMJ and PPU — comparable across movements |
| `rfd_0_100ms` | `(F[bottom + 100] - F[bottom]) / 0.1` | slope of GRF in first 100 ms of concentric phase (N/s) |
| `concentric_impulse_ns` | `trapz(F[bottom:takeoff] - signal_rest_n, dx=1/fs)` | net upward impulse above movement-specific resting force (N·s) |

---

## 7. Power Curve Table

**Table:** `f_readiness_screen_power_curve`

**Upsert key:** `(athlete_uuid, session_date, movement_type, trial_id)`

One row per trial. Written from the same power analysis call that writes inline to the CMJ/PPU table. Contains the 13 power-curve shape columns plus the 9 PHASE_COLS (though phase columns remain `NULL` here because the power signal lacks the eccentric phase — force file data is not written to this table).

---

## 8. Grip Strength

**Table:** `f_readiness_screen_grip`

**Upsert key:** `(athlete_uuid, session_date)`

Grip data is entered manually via the maintenance UI (not from a file). The pipeline accepts it as a `grip_payload` dict and computes derived values at insert time:

| Column | Source |
|--------|--------|
| `left_kg` | entered (converted from lbs if needed) |
| `right_kg` | entered |
| `avg_kg` | `(left_kg + right_kg) / 2` |
| `max_kg` | `max(left_kg, right_kg)` |
| `asymmetry_pct` | `100 × |left - right| / max(left, right)` |
| `dominant_hand` | `"R"` or `"L"` (optional) |
| `entry_source` | `"manual"` |

---

## 9. Composite Readiness Score

**Table:** `f_readiness_screen_score`

**Source file:** `ingestion/scoring.py` → `score_session()`

**Upsert key:** `(athlete_uuid, session_date)`

### Metrics scored

All metrics use z-score vs a rolling 28-day baseline (minimum 3 prior sessions to produce a valid z-score).

**CMJ metrics** (`f_readiness_screen_cmj`, averaged across all trials for the session):

| Column | Sign | Interpretation |
|--------|------|----------------|
| `jump_height` | +1 | higher = better |
| `pp_w_per_kg` | +1 | higher = better |
| `force_at_pp` | +1 | higher = better |
| `vel_at_pp` | +1 | higher = better |
| `mrsi` | +1 | higher = better |
| `contraction_time_s` | -1 | shorter = better (inverted) |
| `ecc_con_duration_ratio` | -1 | lower = better |
| `eccentric_mean_power_w` | -1 | more negative = better braking power |
| `peak_grf_bw_ratio` | +1 | higher = better |
| `rfd_0_100ms` | +1 | higher = better |
| `concentric_impulse_ns` | +1 | higher = better |

**PPU metrics** (`f_readiness_screen_ppu`):

| Column | Sign |
|--------|------|
| `jump_height` | +1 |
| `pp_w_per_kg` | +1 |
| `force_at_pp` | +1 |
| `vel_at_pp` | +1 |
| `mrsi` | +1 |
| `contraction_time_s` | -1 |
| `peak_grf_bw_ratio` | +1 |
| `rfd_0_100ms` | +1 |
| `concentric_impulse_ns` | +1 |
| `ecc_con_duration_ratio` | -1 |
| `eccentric_mean_power_w` | -1 |

**ISO metrics** (`f_readiness_screen_y`, `f_readiness_screen_ir90`):

| Column | Sign |
|--------|------|
| `max_force` (Y) | +1 |
| `max_force` (IR90) | +1 |
| `time_to_max` (Y) | -1 |
| `time_to_max` (IR90) | -1 |

**Power curve metrics** (`f_readiness_screen_power_curve`, averaged per session):

| Column | Sign |
|--------|------|
| `peak_power_w` | +1 |
| `rpd_max_w_per_s` | +1 |
| `rise_slope_w_per_s` | +1 |
| `auc_j` | +1 |
| `rise_time_10_90_s` | -1 |

**Grip metrics** (`f_readiness_screen_grip`):

| Column | Sign |
|--------|------|
| `left_kg` | +1 |
| `right_kg` | +1 |
| `max_kg` | +1 |
| `asymmetry_pct` | -1 |

### Scoring algorithm

```
For each metric:
  1. Fetch today_value = session average across all trials
  2. Fetch baseline_values = session-level averages from prior sessions within 28 days
  3. If len(baseline_values) < 3: metric flagged "insufficient_history", excluded from composite
  4. z = (today_value - mean(baseline_values)) / std(baseline_values)
  5. z_signed = sign × z

composite_z = mean(all z_signed values with sufficient history)
composite_score = clip(50 + 15 × composite_z, 0, 100)

band:
  score ≥ 60 → "READY"
  40 ≤ score < 60 → "CAUTION"
  score < 40 → "FATIGUED"
  no metrics with history → "INSUFFICIENT_HISTORY"
```

Athletic screen sessions (`f_athletic_screen_cmj`, `f_athletic_screen_ppu`) are included as supplemental baseline sources for the core CMJ/PPU metrics (jump_height, pp_w_per_kg, force_at_pp, vel_at_pp). This means prior athletic screen tests contribute to the baseline even on days with no readiness screen.

### Score table columns written

| Column | Value |
|--------|-------|
| `composite_score` | 0–100 float |
| `composite_z` | raw z before scaling |
| `band` | READY / CAUTION / FATIGUED / INSUFFICIENT_HISTORY |
| `cmj_z` | mean z across all CMJ metrics |
| `ppu_z` | mean z across all PPU metrics |
| `iso_z` | mean z across ISO metrics |
| `power_curve_z` | mean z across power-curve metrics |
| `grip_z` | mean z across grip metrics |
| `metrics_used` | count of metrics with ≥3 baseline sessions |
| `baseline_window_days` | 28 (fixed) |
| `flags_json` | per-metric raw z + SWC flag (JSONB) |

---

## 10. Key Source Files

| File | Responsibility |
|------|---------------|
| `ingestion/pipeline.py` | orchestrates everything; contains `run_ingestion()` |
| `ingestion/file_parsers.py` | `discover_cmj_ppu_trials()`, `parse_txt_file()`, `load_power_txt()` file I/O |
| `ingestion/power_analysis.py` | `analyze_power_curve_advanced()` (power shape), `analyze_phase_metrics_from_force()` (phase + force metrics) |
| `ingestion/scoring.py` | `score_session()`, `CMJ_METRICS`, `PPU_METRICS`, `ISO_METRICS`, `GRIP_METRICS`, `POWER_CURVE_METRICS` |
| `ingestion/athlete_manager.py` | `get_or_create_athlete()` (fuzzy match only), `extract_source_athlete_id()` |
| `ingestion/age_utils.py` | `calculate_age_at_collection()`, `calculate_age_group()` |
| `ingestion/units.py` | `inches_to_meters()` (for jump height conversion) |
| `db/connection.py` | `get_connection()` — psycopg2 to Neon via `WAREHOUSE_DATABASE_URL` |

---

## 11. Constants

```python
# ingestion/pipeline.py

POWER_CURVE_COLS = [
    "peak_power_w", "time_to_peak_s", "rpd_max_w_per_s", "time_to_rpd_max_s",
    "rise_time_10_90_s", "fwhm_s", "auc_j", "work_early_pct", "decay_90_10_s",
    "t_com_norm_0to1", "skewness", "kurtosis", "spectral_centroid_hz",
]

PHASE_COLS = [
    "contraction_time_s", "eccentric_duration_s", "concentric_duration_s",
    "ecc_con_duration_ratio", "eccentric_mean_power_w", "eccentric_peak_power_w",
    "eccentric_auc_j", "concentric_auc_j", "mrsi",
]

FORCE_COLS = [
    "peak_grf_n", "peak_grf_bw_ratio", "rfd_0_100ms", "concentric_impulse_ns",
]
```

```python
# ingestion/file_parsers.py

ASCII_FILES = {
    "Y":    "y_data.txt",
    "IR90": "ir90_data.txt",
}

# CMJ/PPU: any file starting with CMJ or PPU (case-insensitive),
# excluding _Power.txt, _Force.txt, and _data files
```

---

## 12. Critical Implementation Notes

1. **Force file must include quiet standing** — The first 50 ms (50 samples at 1000 Hz) must be the athlete at rest on the plate. This is used to estimate signal resting force and mass. If the file starts at movement onset, all phase detection will fail.

2. **Mass is computed per-file from signal resting force, not from stored body weight:**
   - CMJ: `signal_rest_n ≈ full body weight` → correct full-body mass
   - PPU: `signal_rest_n ≈ upper body weight` → correct upper-body mass
   - This ensures velocity integration is physically accurate for both movements

3. **Body weight (from CMJ) is used only for `peak_grf_bw_ratio`** across both CMJ and PPU, so the ratio is comparable across movements.

4. **Velocity threshold is -0.02 m/s** (not -0.05) to catch PPU's shallow eccentric dip (~-0.05 m/s with upper-body mass) while staying safely above resting noise (~0.0004 m/s).

5. **Upsert logic:** Check for existence → INSERT if new, UPDATE if exists. `source_athlete_id` is included in update columns so it corrects if the athlete name was wrong on first insert.

6. **Athletic screen supplemental sources:** The scoring baseline for core CMJ/PPU metrics (jump_height, pp_w_per_kg, force_at_pp, vel_at_pp) pulls from BOTH `f_readiness_screen_cmj/ppu` AND `f_athletic_screen_cmj/ppu`. Session-level averages are used for the baseline, not individual trial values.

7. **`_Force.txt` files do NOT go through the trial discovery** — they are excluded from `discover_cmj_ppu_trials()` and only accessed explicitly by filename after a matched summary trial is found.

8. **Null safety:** Any metric with `today_value = NULL` is skipped silently. Any metric with fewer than 3 baseline sessions is flagged `insufficient_history` and excluded from the composite score but still included in `flags_json`.
