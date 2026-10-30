"""
Readiness Screen v2 (c3d) intake - offline tests, no database and no c3d files needed.

    python -m pytest tests/test_c3d_pipeline.py -q
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ingestion.c3d.isometric import process_isometric           # noqa: E402
from ingestion.c3d.kinetics import process_trial, G              # noqa: E402
from ingestion.c3d import schema                                 # noqa: E402
import ingestion.pipeline as P                                    # noqa: E402

FS = 900.0


def iso_signal():
    t = np.arange(int(4 * FS)) / FS
    f = np.zeros_like(t) + 0.5                                    # 0.5 N plate offset
    on = t >= 0.5
    f[on] += np.minimum(80.0, 160.0 * (t[on] - 0.5))              # ramps to 80 N by 1.0 s, holds
    return f


def cmj_signal(bw=800.0, flight=0.5):
    """quiet stance -> unweight -> push -> flight -> landing (simple but physically consistent enough)."""
    quiet = np.full(int(0.8 * FS), bw)
    unweight = bw - 300 * np.sin(np.linspace(0, np.pi, int(0.25 * FS)))
    push = bw + 900 * np.sin(np.linspace(0, np.pi, int(0.30 * FS)))
    air = np.zeros(int(flight * FS))
    land = np.concatenate([bw + 1500 * np.sin(np.linspace(0, np.pi, int(0.15 * FS))), np.full(int(0.6 * FS), bw)])
    return np.concatenate([quiet, unweight, push, air, land]), flight


class TestProcessors(unittest.TestCase):
    def test_isometric_v3d_definitions(self):
        m, fz = process_isometric(iso_signal(), FS, mass_kg=80.0)
        self.assertAlmostEqual(m["max_force_n"], 80.0, delta=0.5)        # offset removed
        self.assertAlmostEqual(m["max_force_raw_n"], 80.5, delta=0.5)    # V3D raw definition
        self.assertAlmostEqual(m["push_start_s"], 0.5 + 10 / 160, delta=0.02)
        self.assertAlmostEqual(m["max_force_n_per_kg"], 1.0, delta=0.01)
        self.assertEqual(len(fz), len(iso_signal()))

    def test_cmj_jump_height_from_flight_time(self):
        fz, flight = cmj_signal()
        m, arr = process_trial(fz, FS, "CMJ")
        self.assertAlmostEqual(m["jh_m"], G * flight ** 2 / 8, delta=0.01)
        self.assertGreater(m["peak_power_w"], 0)
        self.assertIn("pwr_con_time_kurtosis", m)

    def test_schema_columns_unique(self):
        for cols in (schema.jump_columns("cmj"), schema.iso_columns(), schema.CURVE_COLS):
            names = [c for c, _ in cols]
            self.assertEqual(len(names), len(set(names)))


class FakeCur:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def execute(self, sql, params=None): self.log.append(" ".join(sql.split()))
    def fetchall(self): return []
    def fetchone(self): return None


class FakeConn:
    def __init__(self, log): self.log = log
    def cursor(self, *a, **k): return FakeCur(self.log)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def fake_rows():
    base = dict(source_root="AS2", athlete_folder="Test Athlete_TA", athlete_name="Test Athlete",
                session_date="2026-10-28", session_folder="2026-10-28_", source_c3d="x.c3d",
                processor_version="rs2.0.0", qc_flags="")
    rows = [dict(base, movement="CMJ", trial_name="CMJ1", trial_number=1, jh_in=18.0, peak_power_w=5000.0),
            dict(base, movement="PPU", trial_name="PPU1", trial_number=1, jh_in=4.0, peak_power_w=1500.0),
            dict(base, movement="Y", trial_name="Y_1", trial_number=1, max_force_n=90.0)]
    srow = dict(base, status="ok: 3 trials, 0 errors (readiness_screen)", xml_name="Test Athlete")
    return srow, rows


class TestPipeline(unittest.TestCase):
    def run_pipeline(self, **kw):
        sql, written = [], []
        d = tempfile.mkdtemp()
        sess = Path(d) / "Test Athlete_TA" / "2026-10-28_"
        sess.mkdir(parents=True)
        (sess / "session.xml").write_text("<x/>")
        srow, rows = fake_rows()
        ev = lambda cur, q, data, page_size=200: written.append((q.split("INTO ")[1].split()[0], len(data)))
        with mock.patch.object(P, "get_connection", lambda: FakeConn(sql)), \
             mock.patch.object(P, "process_session", lambda f, root, work: (srow, [dict(r) for r in rows], [])), \
             mock.patch.object(P, "get_or_create_athlete", lambda **k: ("u-1", False)), \
             mock.patch.object(P, "get_athlete_dob", lambda u: None), \
             mock.patch.object(P, "update_athlete_age_group", lambda *a: None), \
             mock.patch.object(P, "update_athlete_data_flag", lambda *a: None), \
             mock.patch.object(P, "call_update_athlete_data_flags", lambda: None), \
             mock.patch.object(P, "score_session", lambda u, d, is_hitter=False: {"band": "READY", "composite_score": 60}), \
             mock.patch("psycopg2.extras.execute_values", ev):
            summary = P.run_ingestion(str(sess), log=lambda m: None, **kw)
        return summary, sql, written

    def test_writes_only_v2_tables(self):
        summary, sql, written = self.run_pipeline()
        self.assertEqual(summary["errors"], [])
        tables = {t for t, _ in written}
        self.assertEqual(tables, {"public.f_readiness_screen_v2_cmj", "public.f_readiness_screen_v2_ppu",
                                  "public.f_readiness_screen_v2_iso"})
        self.assertFalse(any("f_readiness_screen_cmj " in q or "f_readiness_screen_power_curve" in q for q in sql))
        self.assertEqual(len(summary["scores"]), 1)

    def test_rerun_replaces_the_session(self):
        _, sql, _ = self.run_pipeline()
        deletes = [q for q in sql if q.startswith("DELETE")]
        self.assertTrue(any("f_readiness_screen_v2_cmj" in q for q in deletes))
        self.assertTrue(any("f_readiness_screen_v2_force_curve" in q for q in deletes))

    def test_hitter_mode_skips_isometrics(self):
        summary, _, written = self.run_pipeline(is_hitter=True)
        self.assertNotIn("public.f_readiness_screen_v2_iso", {t for t, _ in written})

    def test_output_files_folder_is_rejected(self):
        d = Path(tempfile.mkdtemp()) / "Output Files"
        d.mkdir()
        s = P.run_ingestion(str(d), log=lambda m: None)
        self.assertTrue(s["errors"] and "retired" in s["errors"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
