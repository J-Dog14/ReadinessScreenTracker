-- Readiness Screen Tracker — additive schema migrations.
-- These tables are NEW. They sit alongside the existing f_readiness_screen_*
-- fact tables and do not modify them.
--
-- Apply with: python init_db.py
-- Re-running is safe (CREATE TABLE IF NOT EXISTS).

------------------------------------------------------------------------------
-- f_readiness_screen_score
--   One row per (athlete, session_date). Stores the daily composite readiness
--   score plus the sub-score breakdown so the dashboard can render the
--   contribution of each test family.
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.f_readiness_screen_score (
    id                  SERIAL PRIMARY KEY,
    athlete_uuid        VARCHAR(36) NOT NULL,
    session_date        DATE        NOT NULL,
    composite_score     NUMERIC,             -- 0..100, NULL if not enough history
    composite_z         NUMERIC,             -- raw average z-score across metrics
    band                VARCHAR(16),         -- READY | CAUTION | FATIGUED | INSUFFICIENT_HISTORY
    cmj_z               NUMERIC,             -- mean z across CMJ metrics
    ppu_z               NUMERIC,             -- mean z across PPU metrics
    iso_z               NUMERIC,             -- mean z across I/Y/T/IR90 metrics
    power_curve_z       NUMERIC,             -- mean z across power-curve shape metrics
    metrics_used        INTEGER,             -- how many metrics had ≥3 history points
    baseline_window_days INTEGER DEFAULT 28, -- rolling window used
    flags_json          JSONB,               -- per-metric SWC flags + raw z's
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_readiness_score_athlete_date UNIQUE (athlete_uuid, session_date)
);

CREATE INDEX IF NOT EXISTS idx_f_readiness_score_uuid ON public.f_readiness_screen_score(athlete_uuid);
CREATE INDEX IF NOT EXISTS idx_f_readiness_score_date ON public.f_readiness_screen_score(session_date);


------------------------------------------------------------------------------
-- f_readiness_screen_power_curve
--   Curve-shape metrics derived from raw *_Power.txt files. One row per
--   (athlete, session_date, movement_type, trial_id). Modeled after backend's
--   athleticScreen.power_analysis output.
--
--   movement_type: 'CMJ' or 'PPU' (the only readiness movements with power-time)
------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.f_readiness_screen_power_curve (
    id                      SERIAL PRIMARY KEY,
    athlete_uuid            VARCHAR(36) NOT NULL,
    session_date            DATE        NOT NULL,
    movement_type           VARCHAR(8)  NOT NULL,  -- 'CMJ' | 'PPU'
    trial_id                INTEGER,
    source_file             TEXT,                  -- *_Power.txt path that produced this row
    fs_hz                   NUMERIC,

    -- Base metrics (analyze_power_curve)
    n_samples               INTEGER,
    peak_power_w            NUMERIC,
    time_to_peak_s          NUMERIC,
    rise_time_10_90_s       NUMERIC,
    rise_slope_w_per_s      NUMERIC,
    fwhm_s                  NUMERIC,
    auc_j                   NUMERIC,
    t_com_s                 NUMERIC,
    t_com_norm_0to1         NUMERIC,
    cv_local_peak           NUMERIC,

    -- Advanced metrics (analyze_power_curve_advanced)
    rpd_max_w_per_s         NUMERIC,
    time_to_rpd_max_s       NUMERIC,
    auc_pre_j               NUMERIC,
    auc_post_j              NUMERIC,
    work_early_pct          NUMERIC,
    decay_90_10_s           NUMERIC,
    skewness                NUMERIC,
    kurtosis                NUMERIC,
    spectral_centroid_hz    NUMERIC,

    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_power_curve_session UNIQUE (athlete_uuid, session_date, movement_type, trial_id)
);

CREATE INDEX IF NOT EXISTS idx_f_readiness_pc_uuid     ON public.f_readiness_screen_power_curve(athlete_uuid);
CREATE INDEX IF NOT EXISTS idx_f_readiness_pc_date     ON public.f_readiness_screen_power_curve(session_date);
CREATE INDEX IF NOT EXISTS idx_f_readiness_pc_movement ON public.f_readiness_screen_power_curve(movement_type);


------------------------------------------------------------------------------
-- f_readiness_screen_cmj / f_readiness_screen_ppu — power-curve columns
--   Inline metrics matching f_athletic_screen_cmj layout. Safe to re-run
--   (ADD COLUMN IF NOT EXISTS). Existing rows keep trial_name = NULL and
--   all power-curve columns = NULL — historical data is untouched.
------------------------------------------------------------------------------
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS trial_name           TEXT,
    ADD COLUMN IF NOT EXISTS peak_power_w         DECIMAL,
    ADD COLUMN IF NOT EXISTS time_to_peak_s       DECIMAL,
    ADD COLUMN IF NOT EXISTS rpd_max_w_per_s      DECIMAL,
    ADD COLUMN IF NOT EXISTS time_to_rpd_max_s    DECIMAL,
    ADD COLUMN IF NOT EXISTS rise_time_10_90_s    DECIMAL,
    ADD COLUMN IF NOT EXISTS fwhm_s               DECIMAL,
    ADD COLUMN IF NOT EXISTS auc_j                DECIMAL,
    ADD COLUMN IF NOT EXISTS work_early_pct       DECIMAL,
    ADD COLUMN IF NOT EXISTS decay_90_10_s        DECIMAL,
    ADD COLUMN IF NOT EXISTS t_com_norm_0to1      DECIMAL,
    ADD COLUMN IF NOT EXISTS skewness             DECIMAL,
    ADD COLUMN IF NOT EXISTS kurtosis             DECIMAL,
    ADD COLUMN IF NOT EXISTS spectral_centroid_hz DECIMAL;

ALTER TABLE public.f_readiness_screen_ppu
    ADD COLUMN IF NOT EXISTS trial_name           TEXT,
    ADD COLUMN IF NOT EXISTS peak_power_w         DECIMAL,
    ADD COLUMN IF NOT EXISTS time_to_peak_s       DECIMAL,
    ADD COLUMN IF NOT EXISTS rpd_max_w_per_s      DECIMAL,
    ADD COLUMN IF NOT EXISTS time_to_rpd_max_s    DECIMAL,
    ADD COLUMN IF NOT EXISTS rise_time_10_90_s    DECIMAL,
    ADD COLUMN IF NOT EXISTS fwhm_s               DECIMAL,
    ADD COLUMN IF NOT EXISTS auc_j                DECIMAL,
    ADD COLUMN IF NOT EXISTS work_early_pct       DECIMAL,
    ADD COLUMN IF NOT EXISTS decay_90_10_s        DECIMAL,
    ADD COLUMN IF NOT EXISTS t_com_norm_0to1      DECIMAL,
    ADD COLUMN IF NOT EXISTS skewness             DECIMAL,
    ADD COLUMN IF NOT EXISTS kurtosis             DECIMAL,
    ADD COLUMN IF NOT EXISTS spectral_centroid_hz DECIMAL;


------------------------------------------------------------------------------
-- V2 additions — grip strength, phase metrics, score column
--   See BACKEND_READINESS_HANDOFF.md for Prisma schema counterparts.
--   All idempotent (ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS).
------------------------------------------------------------------------------

-- New table: f_readiness_screen_grip
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
    avg_kg              NUMERIC,
    max_kg              NUMERIC,
    asymmetry_pct       NUMERIC,
    dominant_hand       VARCHAR(8),
    entry_source        VARCHAR(16),
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_grip_athlete_session UNIQUE (athlete_uuid, session_date)
);

CREATE INDEX IF NOT EXISTS idx_grip_athlete ON public.f_readiness_screen_grip(athlete_uuid);
CREATE INDEX IF NOT EXISTS idx_grip_session  ON public.f_readiness_screen_grip(session_date);

-- Phase-analysis columns on CMJ
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS contraction_time_s        DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_duration_s      DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_duration_s     DECIMAL,
    ADD COLUMN IF NOT EXISTS ecc_con_duration_ratio    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_mean_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_peak_power_w    DECIMAL,
    ADD COLUMN IF NOT EXISTS eccentric_auc_j           DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_auc_j          DECIMAL,
    ADD COLUMN IF NOT EXISTS mrsi                      DECIMAL;

-- Phase-analysis columns on PPU (eccentric cols kept for forward-compat; always NULL for still-start protocol)
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

-- Phase-analysis columns on power_curve
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

-- Grip group sub-score on score table
ALTER TABLE public.f_readiness_screen_score
    ADD COLUMN IF NOT EXISTS grip_z NUMERIC;

-- Expand band column: INSUFFICIENT_HISTORY is 20 chars, exceeded VARCHAR(16)
ALTER TABLE public.f_readiness_screen_score
    ALTER COLUMN band TYPE VARCHAR(32);

-- trial_id on CMJ and PPU fact tables
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS trial_id INTEGER;

ALTER TABLE public.f_readiness_screen_ppu
    ADD COLUMN IF NOT EXISTS trial_id INTEGER;

-- v2.1 — force-derived columns on CMJ and PPU fact tables
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS peak_grf_n            DECIMAL,
    ADD COLUMN IF NOT EXISTS peak_grf_bw_ratio     DECIMAL,
    ADD COLUMN IF NOT EXISTS rfd_0_100ms           DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_impulse_ns DECIMAL;

ALTER TABLE public.f_readiness_screen_ppu
    ADD COLUMN IF NOT EXISTS peak_grf_n            DECIMAL,
    ADD COLUMN IF NOT EXISTS peak_grf_bw_ratio     DECIMAL,
    ADD COLUMN IF NOT EXISTS rfd_0_100ms           DECIMAL,
    ADD COLUMN IF NOT EXISTS concentric_impulse_ns DECIMAL;
