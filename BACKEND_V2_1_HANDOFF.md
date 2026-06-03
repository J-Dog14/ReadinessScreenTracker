# Backend Readiness Handoff — V2.1 Incremental Schema Changes

This document covers changes made **after** the original V2 handoff (`BACKEND_READINESS_HANDOFF.md`).
Apply these on top of the V2 migration. All statements are idempotent or safe to re-run.

---

## Changes

### 1. Expand `band` column — `f_readiness_screen_score`

The band value `"INSUFFICIENT_HISTORY"` is 20 characters and was silently truncated by the original `VARCHAR(16)` definition.

```sql
ALTER TABLE public.f_readiness_screen_score
    ALTER COLUMN band TYPE VARCHAR(32);
```

Prisma — update the existing field:
```prisma
  band  String?  @db.VarChar(32)
```

---

### 2. Add `trial_id` — `f_readiness_screen_cmj` and `f_readiness_screen_ppu`

Tracks which numbered trial (1, 2, …) a row represents within a session. Derived from the trial filename (`CMJ1` → 1, `PPU2` → 2).

```sql
ALTER TABLE public.f_readiness_screen_cmj
    ADD COLUMN IF NOT EXISTS trial_id INTEGER;

ALTER TABLE public.f_readiness_screen_ppu
    ADD COLUMN IF NOT EXISTS trial_id INTEGER;
```

Prisma — add to both models:
```prisma
  trial_id  Int?
```

---

### 3. Force-derived metrics — `f_readiness_screen_cmj` and `f_readiness_screen_ppu`

Four new columns computed from the full-trial vertical GRF (`*_Force.txt` files). All idempotent.

| Column | Description | Units |
|--------|-------------|-------|
| `peak_grf_n` | Peak ground reaction force | N |
| `peak_grf_bw_ratio` | Peak GRF normalised to body weight | dimensionless (e.g. 2.8) |
| `rfd_0_100ms` | Rate of force development — first 100 ms of concentric phase | N/s |
| `concentric_impulse_ns` | Net upward impulse above BW during concentric push | N·s |

```sql
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
```

Prisma — add to both `f_readiness_screen_cmj` and `f_readiness_screen_ppu`:
```prisma
  peak_grf_n             Decimal?
  peak_grf_bw_ratio      Decimal?
  rfd_0_100ms            Decimal?
  concentric_impulse_ns  Decimal?
```

---

## Apply the migration

```bash
npx prisma db push
# or
npx prisma migrate dev --name readiness_v2_1_force_metrics_and_fixes
```

---

## Verification

```sql
-- band column is now VARCHAR(32)
SELECT character_maximum_length
FROM information_schema.columns
WHERE table_name = 'f_readiness_screen_score' AND column_name = 'band';
-- should return 32

-- trial_id exists on CMJ
SELECT column_name FROM information_schema.columns
WHERE table_name = 'f_readiness_screen_cmj' AND column_name = 'trial_id';
-- should return 1 row

-- force columns exist on CMJ
SELECT column_name FROM information_schema.columns
WHERE table_name = 'f_readiness_screen_cmj'
  AND column_name IN ('peak_grf_n', 'peak_grf_bw_ratio', 'rfd_0_100ms', 'concentric_impulse_ns');
-- should return 4 rows
```

---

## Notes

- Body weight is estimated internally by the tracker app from the CMJ quiet-standing GRF and is not stored as a separate column. It is used only at compute time to normalise `peak_grf_bw_ratio` and `concentric_impulse_ns`.
- PPU `eccentric_*` columns remain NULL (still-start protocol — no counter-movement). The four new force columns do populate for PPU using the CMJ-derived body weight.
- `peak_grf_n` is stored as an absolute reference but is not included in the composite readiness score (body-weight-dependent, doesn't z-score cleanly across a season). `peak_grf_bw_ratio`, `rfd_0_100ms`, and `concentric_impulse_ns` are scored.
