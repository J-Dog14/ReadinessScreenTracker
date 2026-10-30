"""
Centralized configuration. Reads from .env and exposes typed accessors.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env")


def get_database_url() -> str:
    """Postgres connection string (Neon-compatible)."""
    url = os.getenv("WAREHOUSE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "WAREHOUSE_DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return url


def get_output_dir() -> str:
    """QTM Data folder the readiness sessions live in (c3d + session.xml).

    Readiness Screen v2 reads c3d directly; the Visual3D Output Files step is retired.
    READINESS_SCREEN_DATA_DIR wins; an old READINESS_SCREEN_OUTPUT_DIR is only used if it
    doesn't point at an Output Files folder."""
    d = os.getenv("READINESS_SCREEN_DATA_DIR", "").strip()
    if d:
        return d
    legacy = os.getenv("READINESS_SCREEN_OUTPUT_DIR", "").strip()
    if legacy and "output files" not in legacy.lower():
        return legacy
    return "D:/Athletic Screen 2.0/Data"


def get_power_dir() -> str:
    """Folder containing raw *_Power.txt files (usually same as output dir)."""
    return os.getenv(
        "READINESS_SCREEN_POWER_DIR",
        get_output_dir(),
    )


def get_power_sample_rate_hz() -> float:
    return float(os.getenv("POWER_SAMPLE_RATE_HZ", "1000"))


def get_flask_port() -> int:
    return int(os.getenv("FLASK_PORT", "5057"))


def get_flask_debug() -> bool:
    return os.getenv("FLASK_DEBUG", "0") not in ("0", "false", "False", "")
