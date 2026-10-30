"""
Single source of truth for the Readiness Screen v2 warehouse tables.

    public.f_readiness_screen_v2_cmj / _ppu     one row per jump trial (same metric set as Athletic Screen v2)
    public.f_readiness_screen_v2_iso            one row per isometric trial, movement in I / Y / T / IR90
    public.f_readiness_screen_v2_force_curve    offset-corrected FP3 Fz per trial + event indices

The old f_readiness_screen_* tables are never touched.
    python uais/python/readinessScreen/v2/schema.py > uais/sql/create_readiness_screen_v2_tables.sql
"""
from . import as_schema

JUMPS = ("cmj", "ppu")
TABLE = "public.f_readiness_screen_v2_{}"
ISO_TABLE = "public.f_readiness_screen_v2_iso"
CURVE_TABLE = "public.f_readiness_screen_v2_force_curve"
UNIQUE_KEY = ["athlete_uuid", "session_date", "session_folder", "trial_name"]

KEY_COLS = [
    ("athlete_uuid", "VARCHAR(36) NOT NULL REFERENCES analytics.d_athletes(athlete_uuid) ON DELETE CASCADE"),
    ("session_date", "DATE NOT NULL"),
    ("trial_name", "TEXT NOT NULL"),                   # CMJ1, PPU2, Y1, IR901 ...
    ("session_folder", "TEXT NOT NULL DEFAULT ''"),
]
META_COLS = [
    ("source_system", "VARCHAR(50) NOT NULL DEFAULT 'readiness_screen_v2'"),
    ("source_root", "TEXT"),                           # AS2 = D:/Athletic Screen 2.0/Data, RS3 = D:/Readiness Screen 3/Data
    ("source_athlete_id", "TEXT"),                     # athlete folder name
    ("trial_number", "INTEGER"),
    ("age_at_collection", "NUMERIC"),
    ("age_group", "TEXT"),
    ("source_c3d", "TEXT"),
    ("processor_version", "VARCHAR(20)"),
    ("c3d_first_frame", "INTEGER"),
    ("fs_hz", "NUMERIC"),
    ("plate_used", "TEXT"),
    ("session_bw_source", "TEXT"),
    ("qc_flags", "TEXT[]"),
]
JUMP_META = [("bw_source", "TEXT"), ("starts_unloaded", "BOOLEAN"), ("has_quiet_start", "BOOLEAN")]
# columns the old readiness tables had that kinetics.py does not produce
JUMP_EXTRA = ["eccentric_mean_power_w", "eccentric_work_j", "rfd_con_0_100ms_n_per_s"]
JUMP_METRICS = [m for m in as_schema.METRICS
                if m not in ("ct_s", "rsi", "rsi_legacy", "touchdown_velocity_m_s", "implied_drop_cm")]

ISO_METRICS = [
    "max_force_n", "avg_force_n", "time_to_max_s",                 # V3D definitions, offset-corrected
    "max_force_raw_n", "avg_force_raw_n", "time_to_max_raw_s",     # V3D definitions on the raw signal (validation)
    "plate_offset_n", "push_start_s", "push_duration_s",
    "avg_force_3s_n", "impulse_3s_ns",
    "avg_force_s1_n", "avg_force_s2_n", "avg_force_s3_n", "pct_max_s1", "pct_max_s2", "pct_max_s3", "hold_drop_pct",
    "rfd_0_100ms_n_per_s", "rfd_0_200ms_n_per_s", "peak_rfd_n_per_s", "time_to_peak_rfd_s",
    "mass_kg", "max_force_n_per_kg", "avg_force_n_per_kg", "avg_force_3s_n_per_kg",
]

CURVE_COLS = [
    ("athlete_uuid", "VARCHAR(36) NOT NULL REFERENCES analytics.d_athletes(athlete_uuid) ON DELETE CASCADE"),
    ("session_date", "DATE NOT NULL"),
    ("movement", "VARCHAR(8) NOT NULL"),
    ("trial_name", "TEXT NOT NULL"),
    ("session_folder", "TEXT NOT NULL DEFAULT ''"),
    ("fs_hz", "REAL NOT NULL"),
    ("fz_n", "REAL[] NOT NULL"),
    ("bw_n", "REAL"),
    ("plate_offset_n", "REAL"),
    ("first_frame", "INTEGER"),
    ("takeoff_idx", "INTEGER"), ("landing_idx", "INTEGER"), ("contact_start_idx", "INTEGER"),
    ("concentric_start_idx", "INTEGER"), ("movement_onset_idx", "INTEGER"), ("max_idx", "INTEGER"),
    ("processor_version", "VARCHAR(20)"),
]


def jump_columns(_mv=None):
    cols = list(KEY_COLS) + list(META_COLS) + JUMP_META
    cols += [(m, "DOUBLE PRECISION") for m in JUMP_METRICS + JUMP_EXTRA]
    cols += [(s, "DOUBLE PRECISION") for s in as_schema.SHAPE_COLS]
    return cols


def iso_columns():
    cols = list(KEY_COLS) + [("movement", "VARCHAR(8) NOT NULL")] + list(META_COLS) + [("mass_source", "TEXT")]
    cols += [(m, "DOUBLE PRECISION") for m in ISO_METRICS]
    return cols


def _table(name, cols, idx):
    body = ",\n  ".join(f"{n} {t}" for n, t in cols)
    return [f"CREATE TABLE IF NOT EXISTS {name} (\n  id SERIAL PRIMARY KEY,\n  {body},\n"
            f"  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
            f"  UNIQUE (athlete_uuid, session_date, session_folder, trial_name)\n);",
            f"CREATE INDEX IF NOT EXISTS idx_rs_v2_{idx}_athlete ON {name} (athlete_uuid);",
            f"CREATE INDEX IF NOT EXISTS idx_rs_v2_{idx}_date ON {name} (session_date);"]


def create_sql():
    out = []
    for mv in JUMPS:
        out += _table(TABLE.format(mv), jump_columns(mv), mv)
    out += _table(ISO_TABLE, iso_columns(), "iso")
    out.append(f"CREATE INDEX IF NOT EXISTS idx_rs_v2_iso_movement ON {ISO_TABLE} (movement);")
    body = ",\n  ".join(f"{n} {t}" for n, t in CURVE_COLS)
    out.append(f"CREATE TABLE IF NOT EXISTS {CURVE_TABLE} (\n  {body},\n"
               f"  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
               f"  PRIMARY KEY (athlete_uuid, session_date, session_folder, trial_name)\n);")
    return "-- Generated by uais/python/readinessScreen/v2/schema.py - edit that file, not this one.\n\n" + \
        "\n\n".join(out) + "\n"


if __name__ == "__main__":
    print(create_sql())
