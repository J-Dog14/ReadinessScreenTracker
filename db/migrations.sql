-- Readiness Screen Tracker — additive schema migrations.
-- These tables are NEW. They sit alongside the existing f_readiness_screen_*
-- fact tables and do not modify them.
--
-- Apply with: python -m app.cli init-db
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
