import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from control_plane import game_mode as game_mode_module
from control_plane.game_mode import GameSessionSignal, read_game_mode_state


def test_active_game_mode_requires_live_declared_game_process_and_docker_signal(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    now = datetime(2026, 9, 4, tzinfo=UTC)
    state.write_text(
        json.dumps(
            {
                "Status": "Active",
                "StartedAt": now.isoformat(),
                "ProcessNames": ["example-game.exe"],
                "DockerExpectedDown": True,
            }
        ),
        encoding="utf-8",
    )
    active = read_game_mode_state(state, now=now, process_probe=lambda *_: True)
    assert active.suppress_alerts is True
    inactive = read_game_mode_state(state, now=now, process_probe=lambda *_: False)
    assert inactive.suppress_alerts is False


def test_active_state_without_reliable_owner_signal_never_suppresses(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    now = datetime(2026, 9, 4, tzinfo=UTC)
    state.write_text(
        json.dumps({"Status": "Active", "StartedAt": now.isoformat()}),
        encoding="utf-8",
    )
    inactive = read_game_mode_state(state, now=now, process_probe=lambda *_: True)
    assert inactive.suppress_alerts is False
    assert "process identity" in inactive.reason


def test_active_game_without_expected_docker_shutdown_does_not_suppress(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state.json"
    now = datetime(2026, 9, 4, tzinfo=UTC)
    state.write_text(
        json.dumps(
            {"Status": "Active", "StartedAt": now.isoformat(), "ProcessNames": ["game.exe"]}
        ),
        encoding="utf-8",
    )
    inactive = read_game_mode_state(state, now=now, process_probe=lambda *_: True)
    assert inactive.suppress_alerts is False
    assert "Docker" in inactive.reason


def test_auto_steam_bridge_requires_foreground_installed_game_and_docker_down() -> None:
    signal = GameSessionSignal(
        process_name="example-game.exe",
        process_path=Path(r"D:\SteamLibrary\steamapps\common\Example\example-game.exe"),
        game_root=Path(r"D:\SteamLibrary\steamapps\common\Example"),
        foreground_pid=1234,
        docker_expected_down=True,
    )
    active = read_game_mode_state(None, steam_session_probe=lambda: signal)
    assert active.phase == "active"
    assert active.status == "AutoSteam"
    assert active.suppress_alerts is True

    not_expected = read_game_mode_state(
        None,
        steam_session_probe=lambda: GameSessionSignal(
            process_name=signal.process_name,
            process_path=signal.process_path,
            game_root=signal.game_root,
            foreground_pid=signal.foreground_pid,
            docker_expected_down=False,
        ),
    )
    assert not_expected.suppress_alerts is False
    assert "not proven" in not_expected.reason


def test_steam_library_is_not_scanned_while_docker_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned = False

    def fail_if_scanned() -> tuple[Path, ...]:
        nonlocal scanned
        scanned = True
        return ()

    monkeypatch.setattr(game_mode_module, "_docker_desktop_is_stopped", lambda: False)
    monkeypatch.setattr(game_mode_module, "_steam_game_roots", fail_if_scanned)
    assert game_mode_module.detect_steam_game_session() is None
    assert scanned is False
