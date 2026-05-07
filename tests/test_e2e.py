"""
End-to-end tests for the Readiness Screen Tracker ingestion pipeline.

Tests the full A-to-Z flow:
  file discovery → date filtering → parsing → athlete resolution →
  DB upsert → power curve analysis → flag update → scoring

Uses realistic fixture files (new 5-column Athletic Screen format) and a
fully mocked database layer.  No data is written to any database.

Run:  python tests/test_e2e.py
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from datetime import date
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Use real today so the date-filter tests stay correct regardless of when they run.
TODAY = date.today().isoformat()
STALE = "2020-01-01"   # old enough to never conflict with today


# ─── Fixture builders ─────────────────────────────────────────────────────────

def _header(athlete: str, session_date: str, filename: str) -> str:
    return f"\tD:\\Readiness Screen 3\\Data\\{athlete}\\{session_date}__1\\{filename}\n"


def iso_txt(athlete: str, session_date: str, filename: str,
            max_force=850.2, max_norm=12.5, avg=720.1, avg_norm=10.6, ttm=0.312) -> str:
    return (
        _header(athlete, session_date, filename)
        + "Header\nmeta\nmeta\nmeta\n"
        + f"1\t{max_force}\t{max_norm}\t{avg}\t{avg_norm}\t{ttm}\n"
    )


def cmj_txt(athlete: str, session_date: str, trial: str,
            jh=15.7, pp_fp=374.8, f_pp=1648.0, v_pp=227.40, wkg=4.47) -> str:
    return (
        _header(athlete, session_date, f"{trial}.c3d")
        + "Header\nmeta\nmeta\nmeta\n"
        + f"1\t{jh}\t{pp_fp}\t{f_pp}\t{v_pp}\t{wkg}\n"
    )


def ppu_txt(athlete: str, session_date: str, trial: str,
            jh=8.2, pp_fp=310.5, f_pp=1420.0, v_pp=180.2, wkg=3.65) -> str:
    return (
        _header(athlete, session_date, f"{trial}.c3d")
        + "Header\nmeta\nmeta\nmeta\n"
        + f"1\t{jh}\t{pp_fp}\t{f_pp}\t{v_pp}\t{wkg}\n"
    )


def power_txt(athlete: str, session_date: str, trial: str, peak_w: float = 3000.0) -> str:
    """Gaussian-shaped power curve — 1000 samples at 1000 Hz, peak at 0.5 s."""
    import numpy as np
    t = np.arange(1000) / 1000.0
    power = peak_w * np.exp(-((t - 0.5) ** 2) / (2 * 0.05 ** 2))
    header = (
        _header(athlete, session_date, f"{trial}.c3d")
        + "\tPowZ\nmeta\nmeta\nmeta\n"
    )
    rows = "".join(f"{i + 1}\t{v:.5f}\n" for i, v in enumerate(power))
    return header + rows


def session_xml(name: str = "Smith, John", gender: str = "Male") -> str:
    return f"""<?xml version="1.0"?>
<SessionData>
  <Session>
    <Fields>
      <Name>{name}</Name>
      <Gender>{gender}</Gender>
      <Height>1.8288</Height>
      <Weight>86.18</Weight>
      <Plyo_Day>1</Plyo_Day>
      <Creation_date>{TODAY}</Creation_date>
    </Fields>
  </Session>
</SessionData>
"""


# ─── DB mock helpers ──────────────────────────────────────────────────────────

def make_db_mock():
    """Return (mock_conn, mock_cursor, inserted_tables_list).

    mock_cursor.fetchone() returns None so every SELECT 1 check produces
    'row absent' → pipeline takes the INSERT path.
    The spy records each table name hit by INSERT INTO.
    """
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    inserted: list[str] = []

    def spy(sql: str, params=None):
        if "INSERT INTO" in sql:
            table = sql.split("INSERT INTO")[1].split()[0].strip()
            inserted.append(table)

    cur.execute = spy
    return conn, cur, inserted


def apply_pipeline_patches(
    stack: ExitStack,
    mock_conn,
    athlete_uuid: str = "aaaa-bbbb-cccc-dddd",
    dob=None,
    athlete_side_effect=None,
):
    """Register all DB-touching patches onto an ExitStack.

    Pass athlete_side_effect to override get_or_create_athlete behaviour
    (e.g. raise ValueError to simulate an unknown athlete).
    """
    stack.enter_context(patch("ingestion.pipeline.get_connection",           return_value=mock_conn))
    if athlete_side_effect is not None:
        stack.enter_context(patch("ingestion.pipeline.get_or_create_athlete",    side_effect=athlete_side_effect))
    else:
        stack.enter_context(patch("ingestion.pipeline.get_or_create_athlete",    return_value=(athlete_uuid, False)))
    stack.enter_context(patch("ingestion.pipeline.get_athlete_dob",          return_value=dob))
    stack.enter_context(patch("ingestion.pipeline.update_athlete_data_flag"))
    stack.enter_context(patch("ingestion.pipeline.update_athlete_age_group"))
    stack.enter_context(patch("ingestion.pipeline.call_update_athlete_data_flags"))
    stack.enter_context(patch("ingestion.pipeline.score_session",
                               return_value={
                                   "composite_score": 72.0, "band": "READY",
                                   "composite_z": 1.47, "cmj_z": 1.2,
                                   "ppu_z": 0.9, "iso_z": 1.1, "power_curve_z": 1.5,
                                   "metrics_used": 8,
                               }))


# Import the pipeline once so all tests share the same module object.
from ingestion import pipeline as _pl


# ═══════════════════════════════════════════════════════════════════════════════

class TestFileDiscovery(unittest.TestCase):
    """discover_txt_files and discover_cmj_ppu_trials."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, name):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write("x")

    def test_all_four_iso_movements_discovered(self):
        from ingestion.file_parsers import discover_txt_files
        for fn in ("i_data.txt", "y_data.txt", "t_data.txt", "ir90_data.txt"):
            self._touch(fn)
        found = discover_txt_files(self.tmp)
        self.assertEqual(set(found.keys()), {"I", "Y", "T", "IR90"})

    def test_cmj_ppu_trials_discovered(self):
        from ingestion.file_parsers import discover_cmj_ppu_trials
        for fn in ("CMJ1.txt", "CMJ2.txt", "PPU1.txt",
                   "CMJ1_Power.txt", "PPU1_Power.txt"):
            self._touch(fn)
        trials = discover_cmj_ppu_trials(self.tmp)
        names = {t["trial_name"] for t in trials}
        self.assertIn("CMJ1", names)
        self.assertIn("CMJ2", names)
        self.assertIn("PPU1", names)
        # _Power files must be excluded
        self.assertNotIn("CMJ1_Power", names)
        self.assertNotIn("PPU1_Power", names)

    def test_movement_type_labelled_correctly(self):
        from ingestion.file_parsers import discover_cmj_ppu_trials
        self._touch("CMJ3.txt")
        self._touch("PPU2.txt")
        by_name = {t["trial_name"]: t["movement_type"] for t in discover_cmj_ppu_trials(self.tmp)}
        self.assertEqual(by_name["CMJ3"], "CMJ")
        self.assertEqual(by_name["PPU2"], "PPU")

    def test_file_path_is_absolute_and_exists(self):
        from ingestion.file_parsers import discover_cmj_ppu_trials, discover_txt_files
        for fn in ("i_data.txt", "CMJ1.txt"):
            self._touch(fn)
        iso = discover_txt_files(self.tmp)
        trials = discover_cmj_ppu_trials(self.tmp)
        self.assertTrue(os.path.isabs(iso["I"]))
        self.assertTrue(os.path.isabs(trials[0]["file_path"]))

    def test_empty_directory_returns_empty_dicts(self):
        from ingestion.file_parsers import discover_txt_files, discover_cmj_ppu_trials
        self.assertEqual(discover_txt_files(self.tmp), {})
        self.assertEqual(discover_cmj_ppu_trials(self.tmp), [])


# ─────────────────────────────────────────────────────────────────────────────

class TestFileParsing(unittest.TestCase):
    """parse_txt_file for both ISO and CMJ/PPU formats."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    # ── CMJ ────────────────────────────────────────────────────────────────

    def test_cmj_5column_all_fields(self):
        from ingestion.file_parsers import parse_txt_file
        path = self._write("CMJ1.txt", cmj_txt("Smith_John", TODAY, "CMJ1"))
        r = parse_txt_file(path, "CMJ")
        self.assertIsNotNone(r)
        self.assertEqual(r["name"], "Smith_John")
        self.assertEqual(r["date"], TODAY)
        self.assertEqual(r["movement_type"], "CMJ")
        self.assertEqual(r["trial_name"], "CMJ1")
        self.assertAlmostEqual(r["JH_IN"],         15.7)
        self.assertAlmostEqual(r["PP_FORCEPLATE"],  374.8)
        self.assertAlmostEqual(r["Force_at_PP"],    1648.0)
        self.assertAlmostEqual(r["Vel_at_PP"],      227.40)
        self.assertAlmostEqual(r["PP_W_per_kg"],    4.47)

    def test_cmj_trial_name_from_filename(self):
        from ingestion.file_parsers import parse_txt_file
        path = self._write("CMJ3.txt", cmj_txt("Smith_John", TODAY, "CMJ3"))
        r = parse_txt_file(path, "CMJ")
        self.assertEqual(r["trial_name"], "CMJ3")

    def test_cmj_returns_none_for_too_few_columns(self):
        from ingestion.file_parsers import parse_txt_file
        short = (
            _header("A", TODAY, "CMJ1.c3d")
            + "h\nh\nh\nh\n"
            "1\t10.0\t200.0\n"      # only 2 values — need 5
        )
        path = self._write("CMJ1.txt", short)
        self.assertIsNone(parse_txt_file(path, "CMJ"))

    def test_cmj_peak_power_populated_from_power_file(self):
        from ingestion.file_parsers import parse_txt_file
        self._write("CMJ1.txt",       cmj_txt("Smith_John", TODAY, "CMJ1"))
        self._write("CMJ1_Power.txt", power_txt("Smith_John", TODAY, "CMJ1", peak_w=2800.0))
        path = os.path.join(self.tmp, "CMJ1.txt")
        r = parse_txt_file(path, "CMJ", folder_path=self.tmp)
        self.assertIsNotNone(r["Peak_Power"])
        self.assertAlmostEqual(r["Peak_Power"], 2800.0, delta=2.0)

    def test_cmj_peak_power_none_when_no_power_file(self):
        from ingestion.file_parsers import parse_txt_file
        path = self._write("CMJ1.txt", cmj_txt("Smith_John", TODAY, "CMJ1"))
        r = parse_txt_file(path, "CMJ", folder_path=self.tmp)
        self.assertIsNone(r["Peak_Power"])

    # ── PPU ────────────────────────────────────────────────────────────────

    def test_ppu_5column_all_fields(self):
        from ingestion.file_parsers import parse_txt_file
        path = self._write("PPU1.txt", ppu_txt("Smith_John", TODAY, "PPU1"))
        r = parse_txt_file(path, "PPU")
        self.assertIsNotNone(r)
        self.assertEqual(r["movement_type"], "PPU")
        self.assertEqual(r["trial_name"], "PPU1")
        self.assertAlmostEqual(r["JH_IN"], 8.2)

    # ── ISO ────────────────────────────────────────────────────────────────

    def test_iso_i_all_columns(self):
        from ingestion.file_parsers import parse_txt_file
        path = self._write("i_data.txt", iso_txt("Smith_John", TODAY, "i_data.txt"))
        r = parse_txt_file(path, "I")
        self.assertIsNotNone(r)
        self.assertEqual(r["movement_type"], "I")
        self.assertAlmostEqual(r["Max_Force"],      850.2)
        self.assertAlmostEqual(r["Max_Force_Norm"],  12.5)
        self.assertAlmostEqual(r["Avg_Force"],       720.1)
        self.assertAlmostEqual(r["Avg_Force_Norm"],  10.6)
        self.assertAlmostEqual(r["Time_to_Max"],     0.312)

    def test_iso_returns_none_for_too_few_columns(self):
        from ingestion.file_parsers import parse_txt_file
        short = (
            _header("A", TODAY, "i_data.txt")
            + "h\nh\nh\nh\n"
            "1\t850.0\t12.0\n"   # only 2 values — need 5
        )
        path = self._write("i_data.txt", short)
        self.assertIsNone(parse_txt_file(path, "I"))


# ─────────────────────────────────────────────────────────────────────────────

class TestPeekAndDateFilter(unittest.TestCase):
    """peek_file_date and the pipeline's date-filtering logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(content)

    def test_peek_file_date_extracts_today(self):
        from ingestion.file_parsers import peek_file_date
        self._write("CMJ1.txt", cmj_txt("A", TODAY, "CMJ1"))
        self.assertEqual(peek_file_date(os.path.join(self.tmp, "CMJ1.txt")), TODAY)

    def test_peek_file_date_returns_none_for_missing_file(self):
        from ingestion.file_parsers import peek_file_date
        self.assertIsNone(peek_file_date("/no/such/file.txt"))

    def test_today_preferred_over_stale(self):
        """Pipeline processes only today's file when both today and stale are present."""
        self._write("i_data.txt",  iso_txt("A", TODAY,  "i_data.txt"))
        self._write("y_data.txt",  iso_txt("A", STALE,  "y_data.txt"))

        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: None)

        self.assertIn("public.f_readiness_screen_i", inserted)
        self.assertNotIn("public.f_readiness_screen_y", inserted)

    def test_most_recent_date_used_when_no_today_files(self):
        """When nothing matches today, the newest date wins."""
        OLDER  = "2026-03-01"
        NEWER  = "2026-04-15"
        self._write("i_data.txt", iso_txt("A", NEWER, "i_data.txt"))
        self._write("y_data.txt", iso_txt("A", OLDER, "y_data.txt"))

        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: None)

        self.assertIn("public.f_readiness_screen_i", inserted)
        self.assertNotIn("public.f_readiness_screen_y", inserted)

    def test_stale_cmj_skipped_when_today_iso_present(self):
        self._write("i_data.txt", iso_txt("A", TODAY,  "i_data.txt"))
        self._write("CMJ1.txt",   cmj_txt("A", STALE, "CMJ1"))

        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: None)

        self.assertIn("public.f_readiness_screen_i", inserted)
        self.assertNotIn("public.f_readiness_screen_cmj", inserted)

    def test_all_same_old_date_all_processed(self):
        """If every file shares the same old date, all of them should be processed."""
        OLD = "2026-02-14"
        self._write("i_data.txt", iso_txt("A", OLD, "i_data.txt"))
        self._write("y_data.txt", iso_txt("A", OLD, "y_data.txt"))

        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: None)

        self.assertIn("public.f_readiness_screen_i", inserted)
        self.assertIn("public.f_readiness_screen_y", inserted)

    def test_skip_message_emitted_for_stale_file(self):
        self._write("i_data.txt", iso_txt("A", TODAY, "i_data.txt"))
        self._write("y_data.txt", iso_txt("A", STALE, "y_data.txt"))

        logs = []
        conn, _, _ = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m))

        self.assertTrue(any("[skip]" in l for l in logs),
                        "Expected a [skip] log for the stale file")


# ─────────────────────────────────────────────────────────────────────────────

class TestFullPipeline(unittest.TestCase):
    """Complete fixture set — all 4 ISO + CMJ1 + CMJ2 + PPU1 + Power files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        athlete = "Smith_John"
        for fn, mv in (
            ("i_data.txt",   "i_data.txt"),
            ("y_data.txt",   "y_data.txt"),
            ("t_data.txt",   "t_data.txt"),
            ("ir90_data.txt","ir90_data.txt"),
        ):
            self._write(fn, iso_txt(athlete, TODAY, mv))
        self._write("CMJ1.txt", cmj_txt(athlete, TODAY, "CMJ1", jh=15.7))
        self._write("CMJ2.txt", cmj_txt(athlete, TODAY, "CMJ2", jh=14.9))
        self._write("PPU1.txt", ppu_txt(athlete, TODAY, "PPU1"))
        for trial in ("CMJ1", "CMJ2", "PPU1"):
            self._write(f"{trial}_Power.txt", power_txt(athlete, TODAY, trial))
        self._write("Session.xml", session_xml())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(content)

    def _run(self, **kwargs):
        conn, cur, inserted = make_db_mock()
        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m), **kwargs)
        return summary, inserted, logs

    def test_all_six_fact_tables_receive_inserts(self):
        _, inserted, _ = self._run()
        for table in (
            "public.f_readiness_screen_i",
            "public.f_readiness_screen_y",
            "public.f_readiness_screen_t",
            "public.f_readiness_screen_ir90",
            "public.f_readiness_screen_cmj",
            "public.f_readiness_screen_ppu",
        ):
            self.assertIn(table, inserted, f"Missing INSERT for {table}")

    def test_row_count_7_inserts(self):
        """4 ISO + 2 CMJ trials + 1 PPU trial = 7 inserts total."""
        summary, _, _ = self._run()
        self.assertEqual(summary["rows_inserted"], 7)
        self.assertEqual(summary["rows_updated"], 0)

    def test_two_cmj_trials_produce_two_inserts(self):
        """CMJ1 and CMJ2 are separate rows, not an upsert collision."""
        _, inserted, _ = self._run()
        cmj_hits = [t for t in inserted if "f_readiness_screen_cmj" in t]
        self.assertEqual(len(cmj_hits), 2)

    def test_power_curve_rows_written_for_every_trial(self):
        """CMJ1, CMJ2, PPU1 each have Power.txt → 3 power_curve rows."""
        summary, _, _ = self._run()
        self.assertEqual(summary["power_curve_rows"], 3)

    def test_no_errors_in_clean_run(self):
        summary, _, _ = self._run()
        self.assertEqual(summary["errors"], [])

    def test_session_xml_parsed_without_error(self):
        """Session.xml is found and parsed — no 'Could not parse' warning."""
        _, _, logs = self._run()
        self.assertFalse(
            any("Could not parse Session.xml" in l for l in logs),
            "Session.xml parse error logged unexpectedly"
        )

    def test_power_analysis_log_emitted_per_trial(self):
        _, _, logs = self._run()
        power_logs = [l for l in logs if "[power]" in l and "analysed" in l]
        self.assertEqual(len(power_logs), 3, "Expected one [power] log per trial")

    def test_athlete_flag_call_emitted(self):
        _, _, logs = self._run()
        self.assertTrue(
            any("[flags]" in l for l in logs),
            "Expected [flags] log after successful run"
        )

    def test_summary_athletes_populated(self):
        summary, _, _ = self._run()
        self.assertEqual(len(summary["athletes"]), 1)


# ─────────────────────────────────────────────────────────────────────────────

class TestPipelineEdgeCases(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        with open(os.path.join(self.tmp, name), "w") as f:
            f.write(content)

    def _run(self, mock_conn=None, **kwargs):
        if mock_conn is None:
            mock_conn, _, _ = make_db_mock()
        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, mock_conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m), **kwargs)
        return summary, logs

    def test_cancel_event_prevents_all_writes(self):
        self._write("i_data.txt", iso_txt("A", TODAY, "i_data.txt"))
        conn, _, inserted = make_db_mock()
        cancel = threading.Event()
        cancel.set()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: None, cancel_event=cancel)
        self.assertEqual(summary["rows_inserted"], 0)
        self.assertEqual(summary["rows_updated"], 0)
        self.assertNotIn("public.f_readiness_screen_i", inserted)

    def test_cancel_emits_cancelled_log(self):
        self._write("i_data.txt", iso_txt("A", TODAY, "i_data.txt"))
        conn, _, _ = make_db_mock()
        cancel = threading.Event()
        cancel.set()
        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m), cancel_event=cancel)
        self.assertTrue(any("[cancelled]" in l for l in logs))

    def test_missing_athlete_records_error_and_continues(self):
        """If one athlete isn't found, that file is skipped but others continue."""
        self._write("i_data.txt", iso_txt("Unknown", TODAY, "i_data.txt"))
        self._write("y_data.txt", iso_txt("Unknown", TODAY, "y_data.txt"))

        conn, _, inserted = make_db_mock()
        call_count = {"n": 0}

        def side_effect(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("Athlete 'Unknown' not found in analytics.d_athletes.")
            return ("fallback-uuid", False)

        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, conn, athlete_side_effect=side_effect)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m))

        self.assertEqual(len(summary["errors"]), 1)
        self.assertTrue(any("[ERROR]" in l for l in logs))
        # Second file still processed
        self.assertGreaterEqual(summary["rows_inserted"], 1)

    def test_missing_power_file_cmj_still_inserted(self):
        """CMJ row is inserted even when the matching _Power.txt is absent."""
        self._write("CMJ1.txt", cmj_txt("A", TODAY, "CMJ1"))
        # No CMJ1_Power.txt

        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: None)

        self.assertIn("public.f_readiness_screen_cmj", inserted)
        self.assertEqual(summary["rows_inserted"], 1)
        self.assertEqual(summary["power_curve_rows"], 0)

    def test_missing_power_file_no_power_log(self):
        self._write("CMJ1.txt", cmj_txt("A", TODAY, "CMJ1"))
        conn, _, _ = make_db_mock()
        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            _pl.run_ingestion(self.tmp, log=lambda m: logs.append(m))
        self.assertTrue(any("no Power.txt" in l for l in logs))

    def test_empty_directory_no_crash(self):
        summary, logs = self._run()
        self.assertEqual(summary["rows_inserted"], 0)
        self.assertFalse(any("[ERROR]" in l for l in logs))

    def test_nonexistent_directory_returns_error(self):
        conn, _, _ = make_db_mock()
        logs = []
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion("/nonexistent/path/xyz",
                                         log=lambda m: logs.append(m))
        self.assertTrue(any("[ERROR]" in l for l in logs))
        self.assertEqual(summary["rows_inserted"], 0)

    def test_iso_only_no_cmj_ppu(self):
        self._write("i_data.txt", iso_txt("A", TODAY, "i_data.txt"))
        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: None)
        self.assertEqual(summary["rows_inserted"], 1)
        self.assertIn("public.f_readiness_screen_i", inserted)

    def test_cmj_only_no_iso(self):
        self._write("CMJ1.txt",       cmj_txt("A", TODAY, "CMJ1"))
        self._write("CMJ1_Power.txt", power_txt("A", TODAY, "CMJ1"))
        conn, _, inserted = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn)
            summary = _pl.run_ingestion(self.tmp, log=lambda m: None)
        self.assertEqual(summary["rows_inserted"], 1)
        self.assertIn("public.f_readiness_screen_cmj", inserted)
        self.assertEqual(summary["power_curve_rows"], 1)

    def test_athlete_uuid_override_used(self):
        """When athlete_uuid_override is set, get_or_create_athlete is not called."""
        self._write("i_data.txt", iso_txt("A", TODAY, "i_data.txt"))
        conn, _, _ = make_db_mock()
        with ExitStack() as s:
            apply_pipeline_patches(s, conn, athlete_uuid="override-uuid-9999")
            summary = _pl.run_ingestion(
                self.tmp, log=lambda m: None,
                athlete_uuid_override="override-uuid-9999",
            )
        self.assertEqual(summary["rows_inserted"], 1)


# ─────────────────────────────────────────────────────────────────────────────

class TestPowerCurveAnalysis(unittest.TestCase):
    """Power file loading and curve analysis against fixture Power.txt files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_power(self, name, peak_w) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(power_txt("A", TODAY, name.replace("_Power.txt", ""), peak_w=peak_w))
        return path

    def test_load_power_txt_correct_length(self):
        from ingestion.power_analysis import load_power_txt
        path = self._write_power("CMJ1_Power.txt", peak_w=2000.0)
        arr = load_power_txt(path)
        self.assertEqual(len(arr), 1000)

    def test_load_power_txt_correct_peak(self):
        from ingestion.power_analysis import load_power_txt
        path = self._write_power("CMJ1_Power.txt", peak_w=2500.0)
        arr = load_power_txt(path)
        self.assertAlmostEqual(arr.max(), 2500.0, delta=2.0)

    def test_all_power_curve_cols_present_in_metrics(self):
        from ingestion.power_analysis import load_power_txt, analyze_power_curve_advanced
        path = self._write_power("CMJ1_Power.txt", peak_w=3000.0)
        arr = load_power_txt(path)
        m = analyze_power_curve_advanced(arr, fs_hz=1000.0)
        for col in _pl.POWER_CURVE_COLS:
            self.assertIn(col, m, f"POWER_CURVE_COLS member '{col}' missing from metrics")

    def test_peak_power_w_close_to_fixture_value(self):
        from ingestion.power_analysis import load_power_txt, analyze_power_curve_advanced
        path = self._write_power("CMJ1_Power.txt", peak_w=3000.0)
        arr = load_power_txt(path)
        m = analyze_power_curve_advanced(arr, fs_hz=1000.0)
        self.assertAlmostEqual(m["peak_power_w"], 3000.0, delta=5.0)

    def test_metrics_are_finite(self):
        from ingestion.power_analysis import load_power_txt, analyze_power_curve_advanced
        path = self._write_power("CMJ1_Power.txt", peak_w=3000.0)
        arr = load_power_txt(path)
        m = analyze_power_curve_advanced(arr, fs_hz=1000.0)
        for col in _pl.POWER_CURVE_COLS:
            val = m.get(col)
            if val is not None:
                self.assertTrue(math.isfinite(float(val)),
                                f"Metric '{col}' is not finite: {val}")

    def test_peak_power_from_pow_file_matches_array_max(self):
        from ingestion.file_parsers import peak_power_from_pow_file
        from ingestion.power_analysis import load_power_txt
        path = self._write_power("CMJ1_Power.txt", peak_w=1800.0)
        arr = load_power_txt(path)
        peak = peak_power_from_pow_file("CMJ1", self.tmp)
        self.assertAlmostEqual(peak, arr.max(), delta=1.0)


# ─────────────────────────────────────────────────────────────────────────────

class TestTrialIdAndUpsertKey(unittest.TestCase):
    """trial_name extraction and UPSERT key uniqueness for multi-trial sessions."""

    def test_trial_id_from_name(self):
        self.assertEqual(_pl._trial_id_from_name("CMJ1"), 1)
        self.assertEqual(_pl._trial_id_from_name("CMJ2"), 2)
        self.assertEqual(_pl._trial_id_from_name("PPU3"), 3)
        self.assertEqual(_pl._trial_id_from_name("CMJ10"), 10)
        self.assertEqual(_pl._trial_id_from_name("NoDigit"), 1)  # fallback

    def test_two_trials_use_different_upsert_keys(self):
        """CMJ1 and CMJ2 must target separate rows (different trial_name in WHERE)."""
        tmp = tempfile.mkdtemp()
        try:
            athlete = "Jones_Mike"
            for trial, jh in (("CMJ1", 16.0), ("CMJ2", 15.1)):
                with open(os.path.join(tmp, f"{trial}.txt"), "w") as f:
                    f.write(cmj_txt(athlete, TODAY, trial, jh=jh))
                with open(os.path.join(tmp, f"{trial}_Power.txt"), "w") as f:
                    f.write(power_txt(athlete, TODAY, trial))

            conn, cur, inserted = make_db_mock()
            with ExitStack() as s:
                apply_pipeline_patches(s, conn)
                summary = _pl.run_ingestion(tmp, log=lambda m: None)

            self.assertEqual(summary["rows_inserted"], 2)
            cmj_inserts = [t for t in inserted if "f_readiness_screen_cmj" in t]
            self.assertEqual(len(cmj_inserts), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
