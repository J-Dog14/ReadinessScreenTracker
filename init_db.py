"""
One-shot DB migration runner. Creates the two new tables if they don't exist:
    - public.f_readiness_screen_score
    - public.f_readiness_screen_power_curve

Usage:
    python init_db.py
"""
from db.connection import init_db


if __name__ == "__main__":
    init_db()
