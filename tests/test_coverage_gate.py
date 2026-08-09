from __future__ import annotations

from scripts.check_coverage import TOTAL_RE


def test_total_line_regex_parses_coverage() -> None:
    match = TOTAL_RE.search("control_plane 3104 714 77%\nTOTAL 3104 714 77%")
    assert match is not None
    assert match.group(1) == "77"


def test_total_line_regex_ignores_module_rows() -> None:
    assert TOTAL_RE.search("control_plane/service.py 120 10 92%") is None
