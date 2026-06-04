# Backend Handoff — Scoring Tier Column

Apply this **on top of** all prior migrations (V2, V2.1). One new column on one existing table. Idempotent and safe to re-run.

---

## What and Why

The readiness score now uses a 3-tier system based on how many prior sessions an athlete has:

| Tier | Sessions | Method |
|------|----------|--------|
| `FIRST_RUN` | 0 prior | Z-score vs. team/cohort population mean |
| `A_TO_B` | 1 prior | Z-score of delta (today − prior session) scaled by cohort SD |
| `READINESS` | 2+ prior | Personal rolling z-score (existing methodology, now MIN_HISTORY=2) |

The `scoring_tier` column stores which method was used so the dashboard can display context ("Peer Comparison" badge, "vs. Last Session" badge, or nothing for normal sessions).

---

## SQL

```sql
ALTER TABLE public.f_readiness_screen_score
    ADD COLUMN IF NOT EXISTS scoring_tier VARCHAR(16);
```

Valid values: `FIRST_RUN`, `A_TO_B`, `READINESS`. Existing rows will be `NULL` (treated as `READINESS` by the dashboard — no visual change for historical data).

---

## Prisma

Add one nullable field to the `f_readiness_screen_score` model:

```prisma
model f_readiness_screen_score {
  // ... all existing fields ...
  scoring_tier  String?  @db.VarChar(16)
}
```

Then run:

```bash
npx prisma db push
# or
npx prisma migrate dev --name readiness_scoring_tier
```

---

## Verification

```sql
SELECT column_name, character_maximum_length, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'f_readiness_screen_score'
  AND column_name  = 'scoring_tier';
-- Should return: scoring_tier | 16 | YES
```

---

## Notes

- No existing rows are modified. `scoring_tier` will be `NULL` for all historical score rows until ingestion is re-run for those sessions.
- The tracker app writes this column on every new ingestion. Re-running ingestion for a past date will backfill the correct tier value for that session.
- No other tables are touched.
