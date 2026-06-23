"""
Full backfill — recomputes and upserts scores for every session in f_readiness_screen_score.
Run after any change to scoring logic (e.g. SCORE_SD_TO_POINTS, Z_CLAMP, metric lists).
Sessions are processed in chronological order so that rolling baseline lookups are consistent.
"""
from ingestion.scoring import score_session
from db.connection import get_connection

conn = get_connection()
try:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT athlete_uuid, session_date"
            " FROM public.f_readiness_screen_score"
            " ORDER BY session_date, athlete_uuid",
        )
        sessions = cur.fetchall()
finally:
    conn.close()

print(f"Rescoring {len(sessions)} sessions (all-time)...")
errors = []
for i, (athlete_uuid, session_date) in enumerate(sessions, 1):
    try:
        result = score_session(athlete_uuid, session_date)
        band      = result["band"]
        composite = ult["composite_score"]
        tier      = result.get("scoring_tier", "?")
        print(f"[{i}/{len(sessions)}] {session_date}  {athlete_uuid[:8]}...  "
              f"{tier.ljust(12)}  {band.ljust(22)}  score={composite}")
    except Exception as exc:
        msg = f"  ERROR {session_date} {athlete_uuid[:8]}...: {exc}"
        print(msg)
        errors.append(msg)

print(f"\nDone. {len(sessions) - len(errors)} rescored, {len(errors)} errors.")
if errors:
    print("Errors:")
    for e in errors:
        print(e)
