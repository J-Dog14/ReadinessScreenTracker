"""
Unit conversions. Mirrors backend's common.units.

Session XML provides height in meters, weight in kg.
d_athletes / fact tables store inches and pounds.
"""
from typing import Optional

METERS_TO_INCHES = 39.3701
KG_TO_LBS = 2.2046226


def meters_to_inches(m: Optional[float]) -> Optional[float]:
    if m is None or m <= 0:
        return None
    return m * METERS_TO_INCHES


def kg_to_lbs(kg: Optional[float]) -> Optional[float]:
    if kg is None or kg <= 0:
        return None
    return kg * KG_TO_LBS
