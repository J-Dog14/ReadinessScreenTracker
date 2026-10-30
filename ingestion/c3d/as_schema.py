"""
Single source of truth for the Athletic Screen v2 warehouse tables.

    public.f_athletic_screen_v2_{cmj,dj,ppu,slv}   one row per trial
    public.f_athletic_screen_v2_force_curve        offset-corrected FP3 Fz per trial + event indices

The old f_athletic_screen_* tables are never touched.
"""
MOVEMENTS = ("cmj", "dj", "ppu", "slv")
TABLE = "public.f_athletic_screen_v2_{}"
CURVE_TABLE = "public.f_athletic_screen_v2_force_curve"

UNIQUE_KEY = ["athlete_uuid", "session_date", "session_folder", "trial_name"]
KEY_COLS = [
    ("athlete_uuid", "VARCHAR(36) NOT NULL REFERENCES analytics.d_athletes(athlete_uuid) ON DELETE CASCADE"),
    ("session_date", "DATE NOT NULL"),
    ("trial_name", "TEXT NOT NULL"),
    ("session_folder", "TEXT NOT NULL DEFAULT ''"),   # e.g. '2026-06-02__2' - two screens on one date stay apart
]
META_COLS = [
    ("source_system", "VARCHAR(50) NOT NULL DEFAULT 'athletic_screen_v2'"),
    ("source_athlete_id", "TEXT"),          # athlete folder name, e.g. 'Josh Sborz_JS'
    ("trial_number", "INTEGER"),
    ("age_at_collection", "NUMERIC"),
    ("age_group", "TEXT"),
    ("source_c3d", "TEXT"),
    ("processor_version", "VARCHAR(20)"),
    ("c3d_first_frame", "INTEGER"),
    ("fs_hz", "NUMERIC"),
    ("bw_source", "TEXT"),
    ("session_bw_source", "TEXT"),
    ("plate_used", "TEXT"),                   # 'FP3' (current) or 'FP1+FP2' (older drop jumps)
    ("starts_unloaded", "BOOLEAN"),
    ("has_quiet_start", "BOOLEAN"),
    ("qc_flags", "TEXT[]"),
]
# numeric metric columns (units in the name)
METRICS = [
    "jh_in", "jh_m", "flight_time_s", "takeoff_velocity_m_s",
    "bw_n", "mass_kg", "plate_offset_n", "v_quiet_resid_m_s", "quiet_cv",
    "v_at_contact_start_m_s", "vmin_m_s", "concentric_duration_s", "time_to_takeoff_from_contact_s",
    "peak_force_n", "peak_force_bw", "peak_force_before_takeoff_s", "peak_rfd_n_per_s", "peak_rfd_bw_per_s",
    "concentric_impulse_ns", "concentric_mean_force_n",
    "peak_power_w", "pp_w_per_kg", "vel_at_pp_m_s", "force_at_pp_n", "pp_before_takeoff_s",
    "concentric_mean_power_w", "concentric_work_j",
    "rpd_max_w_per_s", "rpd_max_before_takeoff_s", "rise_time_10_90_s", "rpd_10_90_w_per_s", "decay_90_10_s",
    # eccentric phase - only when the trial starts with quiet stance (CMJ/SLV/PPU)
    "eccentric_duration_s", "ecc_con_duration_ratio", "contraction_time_s", "countermovement_depth_m",
    "unweighting_min_force_bw", "braking_duration_s", "braking_impulse_ns", "braking_mean_force_bw",
    "eccentric_peak_power_w", "mrsi",
    # drop jump
    "ct_s", "rsi", "rsi_legacy", "touchdown_velocity_m_s", "implied_drop_cm",
]
SHAPE_KEYS = ["n", "dur_s", "peak", "mean", "crest", "t_peak_norm", "impulse", "rise_10_90_s", "rise_slope",
              "fwhm_s", "fwhm_norm", "value_skew", "value_kurtosis", "time_com_norm", "time_sd_norm",
              "time_skew", "time_kurtosis", "n_peaks", "max_rate", "t_max_rate_norm"]
SHAPE_PREFIXES = ["pwr_con_", "fz_con_", "fz_contact_"]
SHAPE_COLS = [p + k for p in SHAPE_PREFIXES for k in SHAPE_KEYS]

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
    ("concentric_start_idx", "INTEGER"), ("movement_onset_idx", "INTEGER"),
    ("processor_version", "VARCHAR(20)"),
]


def trial_columns(movement):
    cols = list(KEY_COLS) + list(META_COLS)
    if movement == "slv":
        cols.append(("side", "TEXT"))
    cols += [(m, "DOUBLE PRECISION") for m in METRICS]
    cols += [(s, "DOUBLE PRECISION") for s in SHAPE_COLS]
    return cols


def create_sql():
    out = []
    for mv in MOVEMENTS:
        cols = ",\n  ".join(f"{n} {t}" for n, t in trial_columns(mv))
        t = TABLE.format(mv)
        out.append(f"CREATE TABLE IF NOT EXISTS {t} (\n  id SERIAL PRIMARY KEY,\n  {cols},\n"
                   f"  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
                   f"  UNIQUE (athlete_uuid, session_date, session_folder, trial_name)\n);")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_as_v2_{mv}_athlete ON {t} (athlete_uuid);")
        out.append(f"CREATE INDEX IF NOT EXISTS idx_as_v2_{mv}_date ON {t} (session_date);")
    cols = ",\n  ".join(f"{n} {t}" for n, t in CURVE_COLS)
    out.append(f"CREATE TABLE IF NOT EXISTS {CURVE_TABLE} (\n  {cols},\n"
               f"  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n"
               f"  PRIMARY KEY (athlete_uuid, session_date, session_folder, trial_name)\n);")
    return "\n\n".join(out) + "\n"


if __name__ == "__main__":
    print(create_sql())
