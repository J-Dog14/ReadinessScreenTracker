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

# Readiness Screen v2 (2026-10-28): tests for the retired Visual3D Output Files pipeline (txt discovery,
# date filter, *_Power.txt analysis, txt upserts, hitter mode) were removed with that pipeline:
# TestFileDiscovery, TestFullPipeline, TestHitterMode, TestPeekAndDateFilter, TestPipelineEdgeCases, TestPowerCurveAnalysis, TestSourceAthleteIdWithOverride, TestTrialIdAndUpsertKey, TestUpsertNullSafety.
# The c3d pipeline is covered by tests/test_c3d_pipeline.py.

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

class TestExtractSourceAthleteId(unittest.TestCase):
    """extract_source_athlete_id now handles underscore-separated initials."""

    def _fn(self, name):
        from ingestion.athlete_manager import extract_source_athlete_id
        return extract_source_athlete_id(name)

    # ── underscore-separated (the new fix) ────────────────────────────────────

    def test_underscore_two_char_initials(self):
        """'Trevor Cleveland_TC' → 'TC'  (was returning full string before fix)."""
        self.assertEqual(self._fn("Trevor Cleveland_TC"), "TC")

    def test_underscore_two_char_initials_cw(self):
        """'Connor Wong_CW' → 'CW'."""
        self.assertEqual(self._fn("Connor Wong_CW"), "CW")

    def test_underscore_three_char_initials(self):
        """'John Smith_JSM' → 'JSM'."""
        self.assertEqual(self._fn("John Smith_JSM"), "JSM")

    # ── space-separated (pre-existing behaviour must still work) ──────────────

    def test_space_two_char_initials(self):
        """'Trevor Cleveland TC' → 'TC'."""
        self.assertEqual(self._fn("Trevor Cleveland TC"), "TC")

    def test_space_three_char_initials(self):
        """'John Smith JSM' → 'JSM'."""
        self.assertEqual(self._fn("John Smith JSM"), "JSM")

    # ── no initials → full name returned ─────────────────────────────────────

    def test_no_initials_returns_full_name(self):
        """'Connor Wong' → 'Connor Wong' (nothing to strip)."""
        self.assertEqual(self._fn("Connor Wong"), "Connor Wong")

    def test_no_initials_clean_name(self):
        self.assertEqual(self._fn("John Smith"), "John Smith")

    # ── edge cases ────────────────────────────────────────────────────────────

    def test_empty_string_returns_empty(self):
        self.assertEqual(self._fn(""), "")

    def test_whitespace_only_returns_whitespace(self):
        self.assertEqual(self._fn("  "), "  ")

    def test_lowercase_trailing_letters_not_stripped(self):
        """Trailing lowercase letters are NOT initials — full name returned."""
        self.assertEqual(self._fn("John Smith_tc"), "John Smith_tc")

    def test_single_char_trailing_not_stripped(self):
        """A single trailing letter is not long enough to be initials."""
        self.assertEqual(self._fn("John Smith_J"), "John Smith_J")

    def test_four_char_trailing_not_stripped(self):
        """Four+ uppercase letters are NOT initials (regex requires 2–3)."""
        self.assertEqual(self._fn("John Smith_JOHN"), "John Smith_JOHN")


# ─────────────────────────────────────────────────────────────────────────────

class TestFindExistingAthleteCase(unittest.TestCase):
    """find_existing_athlete now uses UPPER() for a case-insensitive exact match."""

    def _make_cursor(self, first_result, all_results=None):
        """Return a mock cursor whose first fetchone() call returns first_result
        and whose fetchall() returns all_results (for the fuzzy fallback path)."""
        cur = MagicMock()
        cur.fetchone.return_value = first_result
        cur.fetchall.return_value = all_results or []
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        return conn, cur

    def test_lowercase_db_found_via_upper_exact(self):
        """Athlete stored as 'trevor cleveland' (lowercase) must be found on the
        first pass (UPPER comparison) — NOT via fuzzy fallback."""
        from ingestion.athlete_manager import find_existing_athlete

        mock_row = {"athlete_uuid": "uuid-tc", "name": "Trevor Cleveland",
                    "normalized_name": "trevor cleveland"}
        conn, cur = self._make_cursor(first_result=mock_row)

        result = find_existing_athlete(conn, "TREVOR CLEVELAND")

        self.assertIsNotNone(result)
        self.assertEqual(result["athlete_uuid"], "uuid-tc")
        # Verify SQL uses UPPER(normalized_name) — case-insensitive exact path
        first_sql = cur.execute.call_args_list[0][0][0]
        self.assertIn("UPPER(normalized_name)", first_sql)
        # Fuzzy fallback (fetchall) must NOT have been reached
        cur.fetchall.assert_not_called()

    def test_uppercase_db_also_found_via_upper_exact(self):
        """Athletes stored with uppercase normalized_name still match."""
        from ingestion.athlete_manager import find_existing_athlete

        mock_row = {"athlete_uuid": "uuid-cw", "name": "Connor Wong",
                    "normalized_name": "CONNOR WONG"}
        conn, cur = self._make_cursor(first_result=mock_row)

        result = find_existing_athlete(conn, "CONNOR WONG")

        self.assertIsNotNone(result)
        self.assertEqual(result["athlete_uuid"], "uuid-cw")
        cur.fetchall.assert_not_called()

    def test_no_exact_match_falls_to_fuzzy(self):
        """When exact match returns nothing, fuzzy fallback is tried."""
        from ingestion.athlete_manager import find_existing_athlete

        fuzzy_row = {"athlete_uuid": "uuid-fuzzy", "name": "Trevor Cleveland",
                     "normalized_name": "TREVOR CLEVELAND"}
        # fetchone() → None (no exact match); fetchall() → one candidate
        conn, cur = self._make_cursor(first_result=None, all_results=[fuzzy_row])

        result = find_existing_athlete(conn, "TREVOR CLEVELAND TC")

        # Fuzzy path was attempted
        cur.fetchall.assert_called_once()
        # Should still find Trevor Cleveland via high similarity
        self.assertIsNotNone(result)
        self.assertEqual(result["athlete_uuid"], "uuid-fuzzy")

    def test_no_match_anywhere_returns_none(self):
        """If neither exact nor fuzzy finds anyone, None is returned."""
        from ingestion.athlete_manager import find_existing_athlete

        conn, cur = self._make_cursor(first_result=None, all_results=[])

        result = find_existing_athlete(conn, "COMPLETELY UNKNOWN PERSON")

        self.assertIsNone(result)

    def test_fuzzy_match_emits_warning(self):
        """A fuzzy match should emit a log.warning so it appears in the SSE stream."""
        import logging
        from ingestion.athlete_manager import find_existing_athlete

        fuzzy_row = {"athlete_uuid": "uuid-fuzzy", "name": "Trevor Cleveland",
                     "normalized_name": "TREVOR CLEVELAND"}
        conn, cur = self._make_cursor(first_result=None, all_results=[fuzzy_row])

        with self.assertLogs("ingestion.athlete_manager", level=logging.WARNING) as cm:
            find_existing_athlete(conn, "TREVOR CLEVELAND TC")

        self.assertTrue(
            any("Fuzzy-matched" in msg for msg in cm.output),
            f"Expected 'Fuzzy-matched' warning, got: {cm.output}",
        )


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
