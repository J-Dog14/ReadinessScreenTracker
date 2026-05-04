"""
File parsers for readiness screen output files.

Faithful port of backend's uais/python/readinessScreen/file_parsers.py. The
parsing rules (line/column layout, name/date regex, header order) match the
backend exactly so files produced by the existing capture pipeline parse the
same way here.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from .units import meters_to_inches, kg_to_lbs


# Mapping movement_type → expected file name in the Output Files folder.
# Used by the maintenance page to discover what's available before running.
ASCII_FILES = {
    "I":    "i_data.txt",
    "Y":    "y_data.txt",
    "T":    "t_data.txt",
    "IR90": "ir90_data.txt",
    "CMJ":  "cmj_data.txt",
    "PPU":  "ppu_data.txt",
}

# Column order in the cmj/ppu txt rows (after the leading row-number column).
CMJ_PPU_COLUMNS = [
    "JH_IN", "LEWIS_PEAK_POWER", "Max_Force",
    "PP_W_per_kg", "PP_FORCEPLATE", "Force_at_PP", "Vel_at_PP",
]

# Column order in the I/Y/T/IR90 txt rows.
FORCE_COLUMNS = [
    "Max_Force", "Max_Force_Norm", "Avg_Force", "Avg_Force_Norm", "Time_to_Max",
]


# ---------------------------------------------------------------------------
# Helpers — extract athlete name and session date from the file's first line.
# Backend logic matched bit-for-bit so a file the backend ingests fine ingests
# fine here too.
# ---------------------------------------------------------------------------

def extract_name(line: str) -> Optional[str]:
    """First-line path looks like:
       \\D:\\Athletic Screen 2.0\\Data\\NAME\\2024-11-24__2\\...
    The athlete name is the path segment immediately after the segment 'Data'."""
    try:
        parts = line.split(chr(92))  # backslash
        for i, part in enumerate(parts):
            if part == "Data" and i + 1 < len(parts):
                cleaned = parts[i + 1].strip().strip("_")
                if cleaned:
                    return cleaned
    except Exception:
        pass

    m = re.search(r"Data\\([^\\]+?)(?=\\|\t|$)", line)
    if m:
        cleaned = m.group(1).strip().strip("_")
        if cleaned:
            return cleaned

    return None


def extract_date(line: str) -> Optional[str]:
    """Pull the YYYY-MM-DD path segment from the first line."""
    try:
        for part in line.split(chr(92)):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", part)
            if m:
                return m.group(1)
    except Exception:
        pass

    m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Session XML parsing.
# ---------------------------------------------------------------------------

def find_session_xml(folder_path: str) -> Optional[str]:
    if not folder_path or not os.path.isdir(folder_path):
        return None
    for root_dir, _, files in os.walk(folder_path):
        for fn in files:
            if fn.lower().startswith("session") and fn.lower().endswith(".xml"):
                return os.path.join(root_dir, fn)
    return None


def _xml_find_text(element: ET.Element, tag: str) -> Optional[str]:
    found = element.find(tag)
    return found.text if found is not None else None


def parse_session_xml(xml_path: str) -> Dict:
    """Returns {name, gender, height (inches), weight (lbs), plyo_day, creation_date}."""
    tree = ET.parse(xml_path)
    fields = tree.getroot().find(".//Session/Fields")
    if fields is None:
        raise ValueError(f"Session/Fields not found in {xml_path}")

    name = _xml_find_text(fields, "Name")
    gender = _xml_find_text(fields, "Gender")
    h_raw = _xml_find_text(fields, "Height")
    w_raw = _xml_find_text(fields, "Weight")
    plyo_day = _xml_find_text(fields, "Plyo_Day")
    creation_date = _xml_find_text(fields, "Creation_date")

    h_m = float(h_raw) if h_raw else None
    w_kg = float(w_raw) if w_raw else None

    return {
        "name": name,
        "gender": gender,
        "height": meters_to_inches(h_m),
        "weight": kg_to_lbs(w_kg),
        "plyo_day": plyo_day,
        "creation_date": creation_date,
    }


def normalize_gender(g: Optional[str]) -> str:
    if not g:
        return "Male"
    s = str(g).strip().lower()
    if s in ("m", "male"):
        return "Male"
    if s in ("f", "female"):
        return "Female"
    return "Male"


# ---------------------------------------------------------------------------
# Per-test txt parsing. Returns a dict with name, date, movement_type, and
# whatever metric columns are appropriate for that movement.
#
# File layout (matches backend reader):
#   line 0: tab-separated paths
#   lines 1-4: header / metadata (skip)
#   line 5+:  numeric rows, "rownum\tcol1\tcol2\t..."
# ---------------------------------------------------------------------------

def parse_txt_file(file_path: str, movement_type: str) -> Optional[Dict]:
    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return None

    if not lines:
        return None

    first_line = lines[0]
    name = extract_name(first_line)
    date = extract_date(first_line)
    if not name or not date:
        return None

    # First numeric row.
    values = None
    for line in lines[5:]:
        line = line.strip()
        if not line or not re.match(r"^\d", line):
            continue
        parts = line.split("\t")
        try:
            if len(parts) > 1:
                values = [float(t) for t in parts[1:]]
            else:
                values = [float(t) for t in line.split()[1:]]
            break
        except (ValueError, IndexError):
            continue

    if not values:
        return None

    if movement_type in ("CMJ", "PPU"):
        if len(values) < 7:
            return None
        keys = CMJ_PPU_COLUMNS
    else:
        if len(values) < 5:
            return None
        keys = FORCE_COLUMNS

    out = {"name": name, "date": date, "movement_type": movement_type}
    for i, k in enumerate(keys):
        out[k] = values[i] if i < len(values) else None
    return out


def discover_txt_files(output_dir: str) -> Dict[str, str]:
    """Scan an Output Files directory and return {movement_type: file_path} for present files."""
    found = {}
    if not output_dir or not os.path.isdir(output_dir):
        return found
    for movement, fname in ASCII_FILES.items():
        p = os.path.join(output_dir, fname)
        if os.path.isfile(p):
            found[movement] = p
    return found
