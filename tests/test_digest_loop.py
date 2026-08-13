from __future__ import annotations

import datetime as dt

from control_plane.service import RepairService


def _aware(y: int, m: int, d: int, hh: int, mm: int) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def test_before_digest_time_waits_until_today() -> None:
    now = _aware(2026, 8, 13, 21, 0)
    assert RepairService._seconds_until_digest(now, "21:30") == 30 * 60


def test_at_digest_time_rolls_to_tomorrow() -> None:
    now = _aware(2026, 8, 13, 21, 30)
    assert RepairService._seconds_until_digest(now, "21:30") == 24 * 60 * 60


def test_after_digest_time_rolls_to_tomorrow() -> None:
    # A digest that finished after 21:30 must still fire the next day at 21:30,
    # never skip a day (regression: fixed 86400s sleep after run_digest).
    now = _aware(2026, 8, 13, 21, 40)
    assert RepairService._seconds_until_digest(now, "21:30") == 23 * 60 * 60 + 50 * 60


def test_invalid_digest_time_falls_back_to_2130() -> None:
    now = _aware(2026, 8, 13, 20, 0)
    assert RepairService._seconds_until_digest(now, "not-a-time") == 90 * 60
