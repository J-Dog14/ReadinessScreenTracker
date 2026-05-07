"""
File parsers for readiness screen output files.

I/Y/T/IR90 files use the original static names (i_data.txt, etc.).
CMJ/PPU files now use the Athletic Screen format: CMJ1.txt, CMJ2.txt, PPU1.txt, etc.
with matching CMJ1_Power.txt / PPU1_Power.txt power-time-series files.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from math import inf
from typing import Dict, List, Optional

from .units import meters_to_inches, kg_to_lbs


# Static file mapping for isometric force movements only.
# CMJ and PPU are now discovered dynamically as CMJ1.txt, PPU1.txt, etc.
ASCII_FILES = {
    "I":    "i_data.txt",
    "Y":    "y_data.txt",
    "T":    "t_data.txt",
    "IR90": "ir90_data.txt",
}

# 5-column format (Athletic Screen style): JH, PP_FP, Force@PP, Vel@PP, W/kg
CMJ_PPU_COLUMNS = [
    "JH_IN", "PP_FORCEPLATE", "Force_at_PP", "Vel_at_PP", "PP_W_per_kg",
]

# Column order in the I/Y/T/IR90 txt rows.
FORCE_COLUMNS = [
    "Max_Force", "Max_Force_Norm", "Avg_Force", "Avg_Force_Norm", "Time_to_Max",
]


# ---------------------------------------------------------------------------
# Helpers — extract athlete name and session date from the file's first line.
# ---------------------------------------------------------------------------

def extract_name(line: str) -> Optional[str]:
    """First-line path looks like:
       \\D:\\...\\Data\\NAME\\2024-11-24__2\\...
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


def peek_file_date(file_path: str) -> Optional[str]:
    """Read only line 0 of a txt file and return its YYYY-MM-DD date, or None."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            first_line = f.readline()
        return extract_date(first_line)
    except OSError:
        return None


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
# Power file helpers (Athletic Screen style).
# ---------------------------------------------------------------------------

def peak_power_from_pow_file(trial_name_base: str, folder_path: str) -> Optional[float]:
    """Return the maximum PowZ value from {trial_name_base}_Power.txt, or None."""
    power_file = os.path.join(folder_path, f"{trial_name_base}_Power.txt")
    if not os.path.exists(power_file):
        return None

    peak = -inf
    try:
        with open(power_file, "r", encoding="utf-8", errors="ignore") as pf:
            in_numeric = False
            for line in pf:
                line = line.strip()
                if not line:
                    continue
                if re.match(r"^\d+\s+", line):
                    in_numeric = True
                if in_numeric:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            val = float(parts[1])
                            if val > peak:
                                peak = val
                        except ValueError:
                            pass
    except Exception:
        return None

    return None if peak == -inf else peak


# ---------------------------------------------------------------------------
# Per-test txt parsing.
#
# File layout:
#   line 0: tab-separated paths (contains athlete name and session date)
#   lines 1-4: header / metadata (skip)
#   line 5+:  numeric rows, "rownum\tcol1\tcol2\t..."
# ---------------------------------------------------------------------------

def parse_txt_file(
    file_path: str,
    movement_type: str,
    folder_path: Optional[str] = None,
) -> Optional[Dict]:
    """Parse a readiness screen txt file.

    For CMJ/PPU, pass folder_path so the matching *_Power.txt can be loaded
    to populate the Peak_Power field.  Returns a dict with name, date,
    movement_type, trial_name (CMJ/PPU only), and metric columns.
    """
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
        if len(values) < 5:
            return None
        trial_name = os.path.splitext(os.path.basename(file_path))[0]
        peak_power = peak_power_from_pow_file(trial_name, folder_path) if folder_path else None

        out = {
            "name":          name,
            "date":          date,
            "movement_type": movement_type,
            "trial_name":    trial_name,
            "Peak_Power":    peak_power,
        }
        for i, k in enumerate(CMJ_PPU_COLUMNS):
            out[k] = values[i] if i < len(values) else None
        return out
    else:
        if len(values) < 5:
            return None
        out = {"name": name, "date": date, "movement_type": movement_type}
        for i, k in enumerate(FORCE_COLUMNS):
            out[k] = values[i] if i < len(values) else None
        return out


# ---------------------------------------------------------------------------
# File discovery.
# ---------------------------------------------------------------------------

def discover_txt_files(output_dir: str) -> Dict[str, str]:
    """Return {movement_type: file_path} for the static I/Y/T/IR90 files."""
    found = {}
    if not output_dir or not os.path.isdir(output_dir):
        return found
    for movement, fname in ASCII_FILES.items():
        p = os.path.join(output_dir, fname)
        if os.path.isfile(p):
            found[movement] = p
    return found


def discover_cmj_ppu_trials(output_dir: str) -> List[Dict]:
    """Return a list of CMJ/PPU trial descriptors.

    Each entry: {movement_type, trial_name, file_path}.
    Discovers all CMJ*.txt and PPU*.txt files (case-insensitive),
    excluding *_Power.txt files.
    """
    results = []
    if not output_dir or not os.path.isdir(output_dir):
        return results
    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith(".txt") or fname.endswith("_Power.txt"):
            continue
        upper = fname.upper()
        if upper.startswith("CMJ"):
            mvt = "CMJ"
        elif upper.startswith("PPU"):
            mvt = "PPU"
        else:
            continue
        results.append({
            "movement_type": mvt,
            "trial_name":    os.path.splitext(fname)[0],
            "file_path":     os.path.join(output_dir, fname),
        })
    return results
