# Migration Handoff — Readiness Screen CMJ/PPU Schema Update

Run these statements in the **backend app's** database (Neon warehouse).
They are all `ADD COLUMN IF NOT EXISTS` so they are safe to run more than once.

## Why

The readiness screen export pipeline now outputs CMJ and PPU data in the same
format as the Athletic Screen:

- Numbered trial files: `CMJ1.txt`, `CMJ2.txt`, `PPU1.txt`, etc.
- 5-column layout: `JH_IN`, `PP_FORCEPLATE`, `Force@PP`, `Vel@PP`, `PP_W_per_kg`
- Matching power time-series files: `CMJ1_Power.txt`, `PPU1_Power.txt`

To support multiple trials per session and inline power curve metrics (matching
`f_athletic_screen_cmj`), two schema changes are needed:

1. `trial_name TEXT` — stores `"CMJ1"`, `"PPU2"`, etc. The UPSERT key changes
   from `(athlete_uuid, session_date)` to `(athlete_uuid, session_date, trial_name)`.
2. Thirteen inline power curve metric columns — same column names as
   `f_athletic_screen_cmj`.

---

## SQL

```sql
-- f_readiness_screen_cmj
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

-- f_readiness_screen_ppu
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
```

---

## Notes

- Existing rows will have `trial_name = NULL` and all power curve columns `NULL`.
  This is intentional — old data is untouched.
- `peak_force` and the old Lewis-formula `peak_power` columns are preserved as-is
  for historical rows. New rows will write `peak_force = NULL` (no longer in the
  export format) and `peak_power` = max value from `*_Power.txt`.
- `f_readiness_screen_power_curve` is **unchanged** — the scoring pipeline
  continues to read from it and the tracker app still writes to it.
- No changes are needed to `f_readiness_screen_i`, `f_readiness_screen_y`,
  `f_readiness_screen_t`, or `f_readiness_screen_ir90`.
