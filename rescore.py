from ingestion.scoring import score_session
from db.connection import get_connection
from datetime import date

START = date(2026, 6, 3)
END   = date(2026, 6, 16)

conn = get_connection()
try:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT athlete_uuid, session_date"
            " FROM public.f_readiness_screen_score"
            " WHERE session_date BETWEEN %s AND %s"
            " ORDER BY session_date, athlete_uuid",
            (START, END),
        )
        sessions = cur.fetchall()
finally:
    conn.close()

print("Rescoring", len(sessions), "sessions...")
for athlete_uuid, session_date in sessions:
    result = score_session(athlete_uuid, session_date)
    band = result["band"]
    composite = result["composite_score"]
    grip_z = result.get("grip_z")
    print(session_date, athlete_uuid[:8] + "...", band.ljust(22), "composite=" + str(composite), "grip_z=" + str(grip_z))
print("Done.")
