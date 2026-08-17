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


def test_recent_files_finds_nested_evidence(tmp_path) -> None:
    # Evidence files live under data/evidence/<repair-id>/*.json; the non
    # recursive glob used to return nothing, starving the digest prompt.
    nested = tmp_path / "evidence" / "repair-abc"
    nested.mkdir(parents=True)
    (nested / "001-Candidate.json").write_text("{}", encoding="utf-8")
    (tmp_path / "evidence" / "stale.txt").write_text("x", encoding="utf-8")

    flat = RepairService._recent_files(tmp_path / "evidence", "*.json", 10)
    assert flat == []

    recursive = RepairService._recent_files(
        tmp_path / "evidence", "*.json", 10, recursive=True
    )
    assert len(recursive) == 1
    assert recursive[0].endswith("001-Candidate.json")


def test_recent_files_falls_back_to_jsonl_sessions(tmp_path) -> None:
    # -last.md summaries only exist for failed tasks; a digest run must still
    # receive the recent session records so the agent can read them directly.
    (tmp_path / "digest-2026-08-17.jsonl").write_text("{}", encoding="utf-8")
    sessions = RepairService._recent_files(
        tmp_path, "*-last.md", 5, recursive=True
    ) or RepairService._recent_files(tmp_path, "*.jsonl", 5, recursive=True)
    assert len(sessions) == 1
    assert sessions[0].endswith("digest-2026-08-17.jsonl")
