# Backend Readiness Handoff — V2 Schema Migration

Run these statements in the **backend app's** database (Neon warehouse) and update the **Prisma schema** to match.
All statements are idempotent (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) — safe to re-run.

**Run this before any code changes in the readiness-screen-tracker repo.**

---

## Why

The Readiness Screen Tracker is being upgraded to v2. Changes that require schema updates:

1. **New `f_readiness_screen_grip` table** — grip strength is now collected manually (handheld dynamometer). One row per athlete-session.
2. **9 new phase-analysis columns on CMJ, PPU, and power_curve tables** — eccentric-phase duration, concentric-phase duration, mRSI, and related metrics derived from the power-time signal.
3. **`grip_z` column on the score table** — stores the grip group's contribution to the composite readiness score.

Existing rows in all tables are untouched (new columns are NULL for historical rows).

---

## SQL

### 1. New table — `f_readiness_screen_grip`

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
    avg_kg              NUMERIC,        -- (left_kg + right_kg) / 2, computed at insert
    max_kg              NUMERIC,        -- MAX(left_kg, right_kg), computed at insert
    asymmetry_pct       NUMERIC,        -- 100 * |L-R| / MAX(L,R), computed at insert
    dominant_hand       VARCHAR(8),     -- 'R' or 'L'; nullable
    entry_source        VARCHAR(16),    -- 'manual' for now
    notes               TEXT,
    created_at          TIMESTAMP DEFAULT NOW(),
    CONSTRAINT uq_grip_athlete_session UNIQUE (athlete_uuid, session_date)
);

CREATE INDEX IF NOT EXISTS idx_grip_athlete ON public.f_readiness_screen_grip(athlete_uuid);
CREATE INDEX IF NOT EXISTS idx_grip_session  ON public.f_readiness_screen_grip(session_date);
```

### 2. New phase-analysis columns — `f_readiness_screen_cmj`

```sql
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
```

### 3. New phase-analysis columns — `f_readiness_screen_ppu`

```sql
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

Note: The PPU protocol is a still-start plank position (no drop-catch), so all eccentric columns will remain NULL for PPU rows. They are added for forward-compatibility.

### 4. New phase-analysis columns — `f_readiness_screen_power_curve`

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

### 5. New score column — `f_readiness_screen_score`

```sql
ALTER TABLE public.f_readiness_screen_score
    ADD COLUMN IF NOT EXISTS grip_z NUMERIC;
```

---

## Prisma Schema Additions

Add the following to the backend's Prisma schema file. Match the naming conventions used for the existing readiness screen models.

### New model

```prisma
model f_readiness_screen_grip {
  id                Int       @id @default(autoincrement())
  athlete_uuid      String    @db.VarChar(36)
  session_date      DateTime  @db.Date
  source_system     String?   @db.VarChar(64)
  source_athlete_id String?   @db.VarChar(64)
  age_at_collection Decimal?
  age_group         String?   @db.VarChar(32)
  left_kg           Decimal?
  right_kg          Decimal?
  avg_kg            Decimal?
  max_kg            Decimal?
  asymmetry_pct     Decimal?
  dominant_hand     String?   @db.VarChar(8)
  entry_source      String?   @db.VarChar(16)
  notes             String?
  created_at        DateTime  @default(now())

  @@unique([athlete_uuid, session_date], name: "uq_grip_athlete_session")
  @@index([athlete_uuid], name: "idx_grip_athlete")
  @@index([session_date], name: "idx_grip_session")
  @@schema("public")
}
```

### Field additions to existing models

Add these fields to `f_readiness_screen_cmj` and `f_readiness_screen_ppu`:

```prisma
  contraction_time_s      Decimal?
  eccentric_duration_s    Decimal?
  concentric_duration_s   Decimal?
  ecc_con_duration_ratio  Decimal?
  eccentric_mean_power_w  Decimal?
  eccentric_peak_power_w  Decimal?
  eccentric_auc_j         Decimal?
  concentric_auc_j        Decimal?
  mrsi                    Decimal?
```

Add the same 9 fields to `f_readiness_screen_power_curve`.

Add to `f_readiness_screen_score`:

```prisma
  grip_z  Decimal?
```

---

## Apply the migration

After updating the Prisma schema:

```bash
# For Neon (direct push, no migration history needed):
npx prisma db push

# Or if using versioned migrations:
npx prisma migrate dev --name readiness_v2_phase_metrics_and_grip
```

---

## Verification

After running, confirm the changes landed:

```sql
-- Confirm grip table exists and has expected columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'f_readiness_screen_grip'
ORDER BY ordinal_position;

-- Confirm phase columns on CMJ
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'f_readiness_screen_cmj'
  AND column_name IN (
    'contraction_time_s', 'eccentric_duration_s', 'concentric_duration_s',
    'ecc_con_duration_ratio', 'eccentric_mean_power_w', 'eccentric_peak_power_w',
    'eccentric_auc_j', 'concentric_auc_j', 'mrsi'
  );

-- Confirm grip_z on score table
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'f_readiness_screen_score'
  AND column_name = 'grip_z';
```

All three queries should return the expected rows. Once confirmed, the readiness-screen-tracker app changes can be applied.

---

## Notes

- All changes are additive. No existing columns are modified or removed.
- `f_readiness_screen_i` and `f_readiness_screen_t` are **not touched** — historical rows remain queryable.
- `eccentric_*` columns on the PPU table will always be NULL in practice (still-start protocol) but are kept for schema consistency.
- Computed columns (`avg_kg`, `max_kg`, `asymmetry_pct`) are populated by the tracker app at insert time, not by a DB trigger.
