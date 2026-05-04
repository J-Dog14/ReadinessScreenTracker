# Readiness Screen Tracker — Handoff

A standalone Flask app that ingests athlete readiness data, computes a research-anchored daily readiness score, and renders a dashboard. It is **separate** from the main `OctaneBiomechBackend` but shares the same Neon Postgres warehouse and the same shared output folder on disk (`D:\Athletic Screen 2.0\Output Files`).

The intent: the lab computer captures CMJ / PPU / I / Y / T / IR90 trials → drops `*_data.txt` and `*_Power.txt` files into the Output Files folder → this app ingests, scores, persists, and visualizes — **without** touching the backend.

---

## 1. What this app is and isn't

**Is:**
- A Python (Flask) app, single-process, intended to run on the lab computer.
- A faithful port of the backend's readiness-screen ingestion logic + power-curve analysis, plus a new scoring engine.
- The owner of two new tables: `f_readiness_screen_score` and `f_readiness_screen_power_curve`.
- A reader/writer of the existing `f_readiness_screen_*` fact tables and `analytics.d_athletes` (same as the backend writes them).

**Is not:**
- A replacement for the backend. The backend's UAIS Maintenance still runs other ingestion runners (athletic screen, pitching, hitting, mobility, etc.). This app only handles readiness screen.
- A multi-user web service. There's no auth. Single-user lab tool.
- A migration owner for the backend's tables. The backend's `prisma migrate` is the source of truth for schema. The two new tables are now in the backend's `schema.prisma` too — Prisma owns them.

---

## 2. Architecture

### 2.1 Folder map

```
Readiness Screen Tracker/
├── app.py                       # Flask entry: create_app(), registers blueprints, starts server
├── config.py                    # .env-backed config: DB URL, paths, sample rate, port
├── init_db.py                   # One-shot CREATE TABLE IF NOT EXISTS runner (no-op if Prisma has applied)
├── requirements.txt
├── .env.example                 # Template — copy to .env and fill in
├── .gitignore
├── README.md                    # User-facing setup + quick reference
├── HANDOFF.md                   # This file
│
├── db/
│   ├── connection.py            # psycopg2 connection helper + init_db()
│   └── migrations.sql           # New table DDL (idempotent). Mirrors what Prisma now manages.
│
├── ingestion/
│   ├── units.py                 # m → in, kg → lb (mirrors backend common.units)
│   ├── age_utils.py             # YOUTH/HS/COLLEGE/PRO bands, age_at_collection (mirrors common.age_utils)
│   ├── athlete_manager.py       # get_or_create_athlete vs analytics.d_athletes (slimmed port)
│   ├── file_parsers.py          # txt + Session.xml parsers (faithful port of backend file_parsers)
│   ├── power_analysis.py        # Curve-shape metrics: peak, RPD, FWHM, AUC, decay, skew, spectral
│   ├── scoring.py               # Composite Z-score readiness model + persistence
│   └── pipeline.py              # End-to-end orchestration: parse → upsert → power → score
│
├── routes/
│   ├── maintenance.py           # /maintenance page + /api/run, /api/stream, /api/scan, /api/athletes/search, /api/kill
│   └── dashboard.py             # /dashboard page + /api/dashboard/* (athlete data, score history)
│
├── static/
│   ├── css/theme.css            # Mantine-style dark variables, cards, buttons, terminal, score gauge
│   ├── css/dashboard.css        # Plotly overrides + score-card gradient + band colors
│   ├── js/maintenance.js        # Folder scan, SSE run, summary rendering
│   └── js/dashboard.js          # Plotly charts + score gauge + sub-stat grid
│
├── templates/
│   ├── base.html                # Header / nav / DM Sans + JetBrains Mono webfonts
│   ├── maintenance.html         # 3 cards: athlete picker → folder/scan → run/output
│   └── dashboard.html           # Score card → iso → CMJ → PPU → power-curve trends
│
└── tests/
    └── smoke_test.py            # Pure-Python checks: parsers, normalization, age, power, scoring math
```

### 2.2 Data flow

```
Output Files folder
   ├── cmj_data.txt, ppu_data.txt, i_data.txt, y_data.txt, t_data.txt, ir90_data.txt
   ├── Session.xml
   └── (optionally) <trial>_Power.txt files
        │
        ▼
ingestion/file_parsers.py          ── parses each txt; pulls athlete name + date from line 1
        │
        ▼
ingestion/athlete_manager.py       ── get_or_create_athlete vs analytics.d_athletes
        │                              (90% similarity match, period-as-comma rule)
        ▼
ingestion/pipeline.py              ── UPSERT into f_readiness_screen_<movement>
        │                              (existing tables, schema unchanged)
        ▼
ingestion/power_analysis.py        ── for each *_Power.txt, compute curve metrics
        │
        ▼
f_readiness_screen_power_curve     ── new table, one row per (athlete, date, movement, trial)
        │
        ▼
ingestion/scoring.py               ── pull rolling 28-day baselines; z-score every metric;
        │                              average → composite_score; band → READY/CAUTION/FATIGUED
        ▼
f_readiness_screen_score           ── new table, one row per (athlete, date)
        │
        ▼
routes/dashboard.py + dashboard.js ── renders score gauge, time series, F-vs-V scatter, power trends
```

---

## 3. Database

### 3.1 New tables (owned by this app, mirrored in backend's `schema.prisma`)

**`public.f_readiness_screen_score`** — one row per (athlete_uuid, session_date).

| Column | Type | Notes |
|---|---|---|
| id | SERIAL PK | |
| athlete_uuid | VARCHAR(36) | FK to `d_athletes.athlete_uuid` |
| session_date | DATE | |
| composite_score | NUMERIC | 0..100; NULL when no metric had ≥3 history points |
| composite_z | NUMERIC | mean of signed z-scores across all contributing metrics |
| band | VARCHAR(16) | `READY` (≥60), `CAUTION` (40..60), `FATIGUED` (<40), `INSUFFICIENT_HISTORY` |
| cmj_z, ppu_z, iso_z, power_curve_z | NUMERIC | per-group mean z, for dashboard sub-stats |
| metrics_used | INTEGER | how many metrics had enough history to contribute |
| baseline_window_days | INTEGER | currently always 28 — exposed to make this changeable per-run later |
| flags_json | JSONB | per-metric `{today, mean, sd, z, flag, n_history, sign}` for drilldown |
| created_at | TIMESTAMP | |

Unique on `(athlete_uuid, session_date)`. Indexed on `athlete_uuid` and `session_date`.

**`public.f_readiness_screen_power_curve`** — one row per (athlete_uuid, session_date, movement_type, trial_id).

Stores the output of `analyze_power_curve_advanced()` for every `*_Power.txt` file ingested. Movement is `'CMJ'` or `'PPU'`. Columns mirror the dict that function returns: `peak_power_w`, `time_to_peak_s`, `rise_time_10_90_s`, `rise_slope_w_per_s`, `fwhm_s`, `auc_j`, `t_com_s`, `t_com_norm_0to1`, `cv_local_peak`, `rpd_max_w_per_s`, `time_to_rpd_max_s`, `auc_pre_j`, `auc_post_j`, `work_early_pct`, `decay_90_10_s`, `skewness`, `kurtosis`, `spectral_centroid_hz`. Plus `source_file` (the absolute path of the txt that produced the row) and `fs_hz` (sample rate used).

Unique on `(athlete_uuid, session_date, movement_type, trial_id)`.

### 3.2 Existing tables this app reads/writes

These are **the backend's** tables — schema controlled by the backend's `schema.prisma`. This app reads from them (for the dashboard) and UPSERTs into them (for ingestion), but never alters their schema:

| Table | Role |
|---|---|
| `analytics.d_athletes` | Athlete dimension. Lookup by normalized_name (90% fuzzy). Insert if no match. |
| `public.f_readiness_screen` | Session-level rollup. (Not currently written by this app — it writes the per-test tables only.) |
| `public.f_readiness_screen_cmj` | CMJ trial: jump_height, peak_power, peak_force, pp_w_per_kg, pp_forceplate, force_at_pp, vel_at_pp |
| `public.f_readiness_screen_ppu` | PPU trial: same columns as CMJ |
| `public.f_readiness_screen_i / _y / _t / _ir90` | Isometric: avg_force, avg_force_norm, max_force, max_force_norm, time_to_max |

UPSERT pattern: SELECT exists → UPDATE or INSERT. Same approach the backend uses, so this app and the backend can co-write without colliding.

### 3.3 Schema-sync convention

The two new tables are now in the backend's `schema.prisma` (you added them; `prisma migrate` applied them). **The backend's Prisma is the source of truth for schema going forward.**

If you change a table's columns:
1. Edit `prisma/schema.prisma` in the backend.
2. Run `npx prisma migrate dev --name <change>` from the backend folder. (Needs `DATABASE_URL_DIRECT` set — Neon's unpooled host.)
3. Update this app's `db/migrations.sql` to match (defensive — ensures `init_db.py` stays accurate).
4. Update this app's INSERT/UPDATE column lists in `ingestion/pipeline.py` and `ingestion/scoring.py` if columns moved.
5. Run `tests/smoke_test.py`.

---

## 4. The scoring engine — what to know before you touch it

Lives in `ingestion/scoring.py`. Research basis is in the module docstring (Buchheit 2014, Halson 2014, Hopkins 2004).

### 4.1 Pipeline

For each metric `m` in `CMJ_METRICS + PPU_METRICS + ISO_METRICS + POWER_CURVE_METRICS`:
1. Pull today's value: `AVG(col)` over all trials on `session_date`.
2. Pull baseline values: `AVG(col) GROUP BY session_date` for sessions strictly before `session_date` and within the baseline window.
3. If today is non-null AND baseline has ≥ `MIN_HISTORY` (=3) sessions: compute `z = (today - mean) / sample_sd`.
4. Sign-correct: multiply by `sign` (`+1` higher-is-better, `-1` lower-is-better).
5. Append signed z to `all_zs` and to `group_zs[group_name]`.

Then:
- `composite_z = mean(all_zs)`
- `composite_score = clip(50 + 15 * composite_z, 0, 100)`
- band: `READY` ≥ 60, `FATIGUED` < 40, else `CAUTION`
- `flags_json` = per-metric drilldown dict, including SWC-style flag (`rise` if z ≥ 0.6, `drop` if z ≤ -0.6, `stable` otherwise)

### 4.2 Sign convention — the most error-prone bit

Each metric in the four `*_METRICS` tuples carries a sign:

```python
("f_readiness_screen_cmj", "jump_height", +1)   # higher = better
("f_readiness_screen_i",   "time_to_max", -1)   # lower  = better (faster RFD)
```

If you add a metric, get the sign right or the score will be inverted for that metric. **The signed z must be positive on a "good day."**

### 4.3 Common tweaks

- **Change baseline window:** edit `DEFAULT_BASELINE_DAYS` in `scoring.py` (currently 28). Or pass `baseline_days=` to `compute_score_for_session(...)`. Already persisted in `baseline_window_days` column so dashboards know which window was used.
- **Change band cutoffs:** `BAND_READY` / `BAND_FATIGUED` constants in `scoring.py`. Update `dashboard.html`'s legend text and `dashboard.js`'s threshold lines if you do.
- **Change the score-units mapping:** `SCORE_SD_TO_POINTS` (currently 15 — meaning ±1 SD ≈ ±15 points; ±3.3 SD pins to 0/100). If you make this larger the score becomes more reactive to single-day swings.
- **Add a metric:** append a tuple to one of the four `*_METRICS` lists. The query is built from the tuple — no SQL changes needed.
- **Add a metric group:** add a new `<NAME>_METRICS` list, add to the `groups` dict in `compute_score_for_session()`, and add `<name>_z` to `f_readiness_screen_score` (in BOTH places: this app's `db/migrations.sql` AND the backend's `schema.prisma`). Then add a sub-stat tile in `dashboard.js`.

### 4.4 Why this approach

The user picked it from a clarifying-question menu earlier — composite Z-score against the athlete's own rolling baseline is the gold standard for individual day-to-day readiness monitoring (Buchheit). Peer-percentile against the cohort would tell you "how is this athlete profiled" not "is this athlete ready today." SWC-only would have no single composite score. The hybrid (with ACWR added) is roadmap material if the user later wants a fitness/fatigue indicator.

---

## 5. Power curve analysis

Lives in `ingestion/power_analysis.py`. Faithful port of the backend's `athleticScreen/power_analysis.py` — exact same metric definitions, so values produced here match what the backend produces for athletic screen.

`load_power_txt(path)` reads column 2 from a `*_Power.txt` (column 1 is sample index, column 2 is instantaneous power in watts). Tab- or whitespace-separated, header rows skipped automatically.

`analyze_power_curve(power, fs_hz)` — base metrics: `peak_power_w`, `time_to_peak_s`, `rise_time_10_90_s`, `rise_slope_w_per_s` (= `0.8 * peak / rise_time`), `fwhm_s`, `auc_j`, `t_com_s`, `t_com_norm_0to1`, `cv_local_peak`. Plus indices (`onset_idx`, `peak_idx`, etc.) used internally.

`analyze_power_curve_advanced(power, fs_hz)` — adds `rpd_max_w_per_s` (max dP/dt), `time_to_rpd_max_s`, `auc_pre_j`/`auc_post_j`/`work_early_pct`, `decay_90_10_s`, `skewness`, `kurtosis`, `spectral_centroid_hz`.

`find_power_files(power_dir, movement)` walks `power_dir`, returns sorted list of `*_Power.txt` files containing the movement token in the name (case-insensitive). Sort order = trial order, so `enumerate(..., start=1)` in `pipeline.py` gives a stable `trial_id`.

The pipeline calls these for every `(athlete_uuid, session_date)` from the current run, persists each result to `f_readiness_screen_power_curve`, then the scoring engine picks them up via the `POWER_CURVE_METRICS` group.

**Sample rate:** default 1000 Hz. Override via `.env`'s `POWER_SAMPLE_RATE_HZ` or per-run via the maintenance page's "Sample rate" input. If it's wrong, every time-based metric (rise time, FWHM, decay, RPD denominator) will be wrong — so worth verifying once with the user before trusting numbers.

---

## 6. Frontend

### 6.1 Maintenance page (`/maintenance`)

Mirrors the visual style of the backend's UAIS Maintenance page — same Mantine-style dark CSS variables, same card layout, same monospace terminal output box, same "athlete-picker → run → stream → summary" flow.

Three cards top-to-bottom:
1. **Athlete picker.** "Auto-detect" extracts name from each file's first line (matches backend reader). "Existing athlete" lets you search `analytics.d_athletes` by name and pin all data to that UUID, overriding what the file says.
2. **Output files folder.** Two text inputs (output dir, power dir) plus sample-rate input. "Scan folder" hits `/api/scan` and lights up the six file tiles based on what's present.
3. **Run.** "Run" button POSTs to `/api/run`, gets a `job_id`, then opens an EventSource to `/api/stream/<job_id>`. Each `event: log` updates the terminal pane with stage-colored output. The final `event: done` carries the summary JSON, which renders as score cards (one per athlete-date) with sub-z stats and a "View dashboard" link.

Note: there's a "Stop" button wired up that hits `/api/kill/<job_id>`, but the pipeline doesn't yet check the cancel flag mid-run. For now `Stop` just hides the kill button — full cancellation is a follow-up. (See section 8.)

### 6.2 Dashboard page (`/dashboard`)

Single-page dark dashboard. Athlete dropdown at top. Five cards:

1. **Composite readiness card** — circular SVG gauge showing today's composite score (last session in DB), banded color (green/yellow/red/gray). Sub-stat grid showing CMJ z / PPU z / Iso z / Power curve z. Score-history line plot at bottom with READY (60) and FATIGUED (40) threshold lines.
2. **Isometric** — line chart of avg_force over time for I/Y/T/IR90 (one trace each, palette: cyan/green/orange/pink). Stat grid below shows latest + delta vs prev session.
3. **CMJ** — two side-by-side plots: jump height over time, and Force-vs-Velocity-at-peak-power scatter (peer cloud as faint cyan + this athlete colored by date). Stat grid below.
4. **PPU** — same layout as CMJ.
5. **Power-curve trends** — peak_power_w over time on left axis, RPD max on right axis, CMJ vs PPU as separate traces. Empty-state message if no `*_Power.txt` was ingested yet.

All charts are Plotly.js (loaded from CDN). Layout uses `paper_bgcolor: transparent` so the card backgrounds show through.

The single API call that drives everything is `GET /api/dashboard/athlete/<uuid>`. Returns one big JSON with `iso`, `cmj`, `ppu`, `score_history`, `latest_score`, `power_curves`. No incremental updates — re-rendering refetches the whole payload (single-user app, not a perf concern).

---

## 7. Configuration

`.env` keys (template in `.env.example`):

| Key | Default | Notes |
|---|---|---|
| `WAREHOUSE_DATABASE_URL` | (required) | Same Neon URL the backend uses. Pooled host is fine — this app does no migrations at runtime. |
| `READINESS_SCREEN_OUTPUT_DIR` | `D:/Athletic Screen 2.0/Output Files` | Where the txt files live. |
| `READINESS_SCREEN_POWER_DIR` | (falls back to output dir) | Where `*_Power.txt` files live. Usually same. |
| `POWER_SAMPLE_RATE_HZ` | `1000` | Force plate sample rate. Same as backend's athletic screen default. |
| `FLASK_PORT` | `5057` | |
| `FLASK_DEBUG` | `0` | Set to `1` for autoreload during development. |

`config.py` reads these via `python-dotenv` at import time. Errors are deferred until first DB call (so importing config without `.env` doesn't crash — useful for tests).

---

## 8. Running it

First time on a clean machine:

```powershell
cd C:\Users\Joey\PycharmProjects\Readiness Screen Tracker
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env       # then edit: paste WAREHOUSE_DATABASE_URL
python init_db.py            # safe no-op now that Prisma owns the schema
python app.py                # serves http://127.0.0.1:5057
```

Daily run: open `/maintenance`, point at the folder, hit Run, then `/dashboard`.

Smoke test (no DB needed):

```
python tests/smoke_test.py
```

Should print 6 sections of `ok` lines and `ALL SMOKE TESTS PASSED.`

---

## 9. Boundaries with the backend

### 9.1 What was ported (and why we duplicated, not imported)

- `common/units.py` → `ingestion/units.py`
- `common/age_utils.py` → `ingestion/age_utils.py` (subset — only what we need)
- `common/athlete_manager.py` → `ingestion/athlete_manager.py` (slimmed: only `get_or_create_athlete`, `find_existing_athlete`, `update_athlete_age_group`, search/list helpers)
- `common/athlete_utils.py` → folded into `ingestion/athlete_manager.py` (`extract_source_athlete_id`)
- `readinessScreen/file_parsers.py` → `ingestion/file_parsers.py`
- `readinessScreen/main.py`'s `process_txt_files` → `ingestion/pipeline.py`'s `run_ingestion`
- `athleticScreen/power_analysis.py` → `ingestion/power_analysis.py`

We chose to duplicate rather than import from the backend so the lab computer can run this app without needing the backend repo present on disk. The cost: if the backend changes a parsing rule, this app falls behind. The drift is bounded because the file format is fixed by the capture pipeline (V3D), but worth knowing.

### 9.2 What's NEW (no backend equivalent)

- `ingestion/scoring.py` — the composite Z-score model is novel to this app.
- The two new tables and their JSONB drilldown.
- The Mantine-skinned Flask UI.
- The dashboard's score gauge and power-curve trend panel.

### 9.3 Drift policy

If the backend's parsing logic or athlete-resolution rules change in `OctaneBiomechBackend/uais/python/readinessScreen/` or `common/`, mirror the change here and re-run `tests/smoke_test.py`. Specifically watch:

- `extract_name` / `extract_date` regex in `file_parsers.py` (if the file path format changes)
- `normalize_name_for_matching` rules in `athlete_manager.py` (period-as-comma, underscores, fuzzy threshold)
- `calculate_age_group` thresholds in `age_utils.py` (currently YOUTH<14, HS 14-18, COLLEGE 18-22, PRO >22)

Schema changes are now Prisma-owned (section 3.3) — no drift risk there as long as `npx prisma migrate dev` is the only way the backend's schema changes.

---

## 10. Limitations / known issues / follow-ups

Things that are deliberately MVP, in priority order if you want to keep building:

1. **`/api/kill` is a no-op mid-run.** The pipeline doesn't yet check `Job.cancelled`. Add a check inside the per-movement loop and the scoring loop. Easy.
2. **No PDF reports.** The original Readiness-Screen project generated per-athlete PDFs with peer-cohort histograms. Not ported. If desired, add a `reports/` module using ReportLab and surface it from the dashboard.
3. **Single trial per (athlete, date, movement, trial_id).** Power curve trial_id is derived from `enumerate()` over `find_power_files()`. If the same trial number appears in two runs, the second overwrites via the unique constraint. Fine in practice, but if you ever ingest multi-rep power data per session you'll want a richer trial key.
4. **Power-curve metric trend chart only shows peak + RPD.** Easy to add more metric overlays in `dashboard.js`'s `renderPowerCurves()`.
5. **No SWC against between-subject SD.** The current implementation flags z thresholds at ±0.6 against the athlete's own SD, which is roughly Hopkins's WITHIN-subject smallest-worthwhile-change. Adding peer-cohort SDs would require a query that pulls cohort SDs alongside personal stats. Roadmap.
6. **No login.** Anyone with network access to the lab computer can hit the app. Fine for `127.0.0.1`-only; if you ever bind to `0.0.0.0`, add a `secret_key` and a one-password gate.
7. **Athlete-picker DOB / new-athlete creation modal.** The maintenance page can match existing athletes but doesn't yet have the backend's modal for prompting DOB / height / weight when creating a brand new one. New athletes today get created with NULL DOB → `age_at_collection` ends up NULL → the `age_group` column is left at NULL on the new fact rows. Not blocking (composite z-score doesn't need age), but the dashboard's age-group filter will exclude them.
8. **`d_athletes` updates are minimal.** The port doesn't replicate the backend's `update_athlete_in_warehouse` height/weight/email-fill logic. We only set `age_group` from the most recent insert. If you want height/weight tracked, port the rest from `common.athlete_manager.update_athlete_in_warehouse`.
9. **No tests for `pipeline.py` end-to-end.** `smoke_test.py` covers the math and parsers. A DB-backed integration test (using a test schema or rolled-back transaction) would be a good add.

---

## 11. Quick file reference

When making changes, here's where to look first:

| Want to change... | Edit... |
|---|---|
| The score formula or weights | `ingestion/scoring.py` |
| Which metrics contribute to the score | the `*_METRICS` lists in `ingestion/scoring.py` |
| Power-curve metric definitions | `ingestion/power_analysis.py` |
| File parsing / first-line regex | `ingestion/file_parsers.py` |
| Athlete name matching rules | `ingestion/athlete_manager.py` |
| The maintenance page UX | `templates/maintenance.html` + `static/js/maintenance.js` |
| The dashboard layout/charts | `templates/dashboard.html` + `static/js/dashboard.js` |
| Theme colors / CSS variables | `static/css/theme.css` |
| Score gauge band colors | `static/css/dashboard.css` |
| API contracts | `routes/maintenance.py` + `routes/dashboard.py` |
| New table column / index | the backend's `prisma/schema.prisma` (run `prisma migrate dev`); then mirror in this app's `db/migrations.sql` |
| Configuration / env vars | `config.py` + `.env.example` |

---

## 12. The Prisma migration that's now applied

For posterity: the backend's `schema.prisma` should now contain the two new models (`f_readiness_screen_score`, `f_readiness_screen_power_curve`) and the back-references on `d_athletes`. The migration was generated with:

```bash
cd C:\Users\Joey\PycharmProjects\OctaneBiomechBackend
npx prisma migrate dev --name add_readiness_score_tables
```

(This required `DATABASE_URL_DIRECT` set in the backend's `.env` — Neon's unpooled host, no `-pooler` in the URL — because Prisma uses `directUrl` for migrations.)

The migration file lives under `prisma/migrations/<timestamp>_add_readiness_score_tables/migration.sql` in the backend repo. Commit it.
