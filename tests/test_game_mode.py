import json
from datetime import UTC, datetime
from pathlib import Path

from control_plane.game_mode import read_game_mode_state


def test_active_game_mode_requires_live_cs2(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    now = datetime(2026, 9, 4, tzinfo=UTC)
    state.write_text(json.dumps({"Status": "Active", "StartedAt": now.isoformat()}), encoding="utf-8")
    active = read_game_mode_state(state, now=now, process_probe=lambda: True)
    assert active.suppress_alerts is True
    inactive = read_game_mode_state(state, now=now, process_probe=lambda: False)
    assert inactive.suppress_alerts is False
