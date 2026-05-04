# Readiness Screen Tracker

A standalone Flask app for ingesting, scoring, and visualizing daily athlete readiness data from the Octane biomech readiness screen battery (CMJ, PPU, and isometric I/Y/T/IR90 tests).

This app is **separate** from the main Octane biomech backend — it does not modify it — but reads from the same shared output folder (`D:\Athletic Screen 2.0\Output Files`) and writes to the same Neon Postgres warehouse.

## What it does

1. **Ingest** — point it at the Output Files folder; it parses the same `cmj_data.txt`, `ppu_data.txt`, `i_data.txt`, `y_data.txt`, `t_data.txt`, `ir90_data.txt`, and `Session.xml` the backend reads.
2. **Resolve athlete UUIDs** against `analytics.d_athletes` exactly the way the backend does (90% name similarity, period-as-comma normalization, source_athlete_id mapping).
3. **Insert/upsert** rows into `f_readiness_screen_*` fact tables (existing schema, untouched).
4. **Analyze power-velocity curves** when raw `*_Power.txt` files are present (RPD, FWHM, AUC, decay, skewness, spectral centroid — the same `power_analysis.py` toolkit used in athletic screen).
5. **Score** — a research-anchored composite Z-score against the athlete's own rolling 28-day baseline (Buchheit 2014, Halson 2014). Each metric is z-standardized, averaged, and mapped to a 0–100 readiness score with traffic-light bands.
6. **Persist** the daily score and curve metrics to two **new** tables: `f_readiness_screen_score` and `f_readiness_screen_power_curve`.
7. **Display** — a Mantine-styled maintenance page for ingestion (mirrors the UAIS Maintenance UX), and a dark dashboard (mirrors the original Dash readiness dashboard) showing trends, sub-scores, and the F-vs-V scatter against peers.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # Then edit .env with your DB URL
python -m app.cli init-db       # Creates the two new tables
python app.py                   # Starts Flask on http://127.0.0.1:5057
```

## Configuration

Set in `.env`:

```
WAREHOUSE_DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
READINESS_SCREEN_OUTPUT_DIR=D:/Athletic Screen 2.0/Output Files
READINESS_SCREEN_POWER_DIR=D:/Athletic Screen 2.0/Output Files
FLASK_PORT=5057
```

## Architecture

```
Readiness Screen Tracker/
├── app.py                       # Flask entry point
├── config.py                    # Env + path config
├── requirements.txt
├── .env.example
├── ingestion/
│   ├── file_parsers.py          # CMJ/PPU/I/Y/T/IR90 txt + Session.xml
│   ├── athlete_manager.py       # get_or_create_athlete vs d_athletes
│   ├── units.py                 # m→in, kg→lb
│   ├── age_utils.py             # age_group canonical (YOUTH/HS/COLLEGE/PRO)
│   ├── power_analysis.py        # Power curve metrics (RPD, FWHM, AUC, etc.)
│   ├── scoring.py               # Composite Z-score readiness model
│   └── pipeline.py              # Orchestrates parse → insert → score
├── db/
│   ├── connection.py            # psycopg2 + .env, with keepalives
│   └── migrations.sql           # New tables (idempotent)
├── routes/
│   ├── maintenance.py           # /maintenance, /api/run, /api/stream
│   └── dashboard.py             # /dashboard, /api/athlete/<uuid>/data
├── templates/
│   ├── base.html
│   ├── maintenance.html
│   └── dashboard.html
└── static/
    ├── css/theme.css            # Mantine-style dark CSS variables
    ├── css/dashboard.css
    ├── js/maintenance.js
    └── js/dashboard.js
```

## Scoring methodology

The composite readiness score follows Buchheit's individualized monitoring framework:

1. For each metric *m* (CMJ jump_height, peak_power, peak_force, PPU jump_height, max_force across I/Y/T/IR90, plus power-curve shape z's), compute a Z-score against the athlete's own rolling 28-day historical baseline (mean μ_m, SD σ_m).
2. Aggregate: `z_composite = mean(z_m for m in metrics_with_data)` — only metrics with ≥3 historical data points contribute.
3. Map `z_composite` to a 0–100 readiness score: `score = clip(50 + 15 * z_composite, 0, 100)`. (15 SD-units → ±100 points; one SD ≈ ±15 points.)
4. Traffic-light bands:
   - **Green (READY):** score ≥ 60 (≥ +0.67 SD)
   - **Yellow (CAUTION):** 40 ≤ score < 60 (within ±0.67 SD)
   - **Red (FATIGUED):** score < 40 (≤ -0.67 SD)

Per-test SWC flags (Hopkins 2004) are also exposed as JSON for drill-down — meaningful drop / stable / meaningful rise per metric, threshold = 0.2 × between-subject SD.

References: Buchheit M. (2014) *Front Physiol*; Halson SL. (2014) *Sports Med*; Hopkins WG. (2004) *Sportscience*.

## Notes on relationship to the backend

This app **does not import from** `OctaneBiomechBackend`. The ingestion modules here are a faithful port of the backend's `uais/python/readinessScreen/` and `uais/python/athleticScreen/power_analysis.py` so this app can run independently on the lab computer — but they live in this repo, not as a sibling install.

The two new tables (`f_readiness_screen_score`, `f_readiness_screen_power_curve`) are **additive** — they don't touch the backend's existing `f_readiness_screen_*` schema.
