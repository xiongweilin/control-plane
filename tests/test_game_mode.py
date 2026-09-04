from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from control_plane.alerts import alert_fingerprint
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.game_mode import read_game_mode_state
from control_plane.models import Alert, AlertmanagerPayload
from control_plane.service import RepairService
from control_plane.storage import Store


class _NoopNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def notify(self, severity: str, title: str, text: str) -> None:
        self.calls.append((severity, title, text))


def _write_state(path, *, status: str, started_at: datetime | None = None, completed_at: datetime | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "Status": status,
                "StartedAt": started_at.isoformat() if started_at else None,
                "CompletedAt": completed_at.isoformat() if completed_at else None,
            }
        ),
        encoding="utf-8",
    )


def test_active_game_mode_requires_fresh_state_and_process(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    _write_state(path, status="Active", started_at=now - timedelta(minutes=5))

    active = read_game_mode_state(path, now=now, process_probe=lambda: True)
    assert active.phase == "active"
    assert active.suppress_alerts is True

    missing_process = read_game_mode_state(path, now=now, process_probe=lambda: False)
    assert missing_process.phase == "inactive"
    assert "cs2.exe" in missing_process.reason


def test_game_mode_state_is_fail_open_for_stale_or_failed_restore(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    _write_state(path, status="Active", started_at=now - timedelta(hours=13))
    assert read_game_mode_state(path, now=now, process_probe=lambda: True).phase == "inactive"

    _write_state(path, status="RestoreFailed", completed_at=now)
    failed = read_game_mode_state(path, now=now)
    assert failed.phase == "inactive"
    assert "restore failed" in failed.reason


def test_restored_state_has_bounded_grace_period(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    path = tmp_path / "state.json"
    _write_state(path, status="Restored", completed_at=now - timedelta(minutes=5))
    assert read_game_mode_state(path, now=now).phase == "restore_grace"

    _write_state(path, status="Restored", completed_at=now - timedelta(minutes=11))
    assert read_game_mode_state(path, now=now).phase == "inactive"


def _alert(*, status: str, labels: dict[str, str]) -> Alert:
    return Alert.model_validate(
        {
            "status": status,
            "labels": labels,
            "annotations": {},
            "startsAt": "2026-09-04T11:00:00Z",
            "endsAt": "2026-09-04T12:00:00Z" if status == "resolved" else None,
            "fingerprint": "game-mode-test",
        }
    )


@pytest.mark.asyncio
async def test_allowlisted_alert_is_suppressed_and_resolved_without_warning(tmp_path) -> None:
    now = datetime.now(UTC)
    state_path = tmp_path / "state.json"
    _write_state(state_path, status="Active", started_at=now - timedelta(minutes=1))
    config = replace(
        ControlPlaneConfig(),
        api_key="test-key",
        state_db=tmp_path / "control-plane.db",
        notification_enabled=False,
        game_mode_state_path=state_path,
    )
    store = Store(config.state_db)
    notifier = _NoopNotifier()
    service = RepairService(
        config,
        store,
        Budget(store, 10, 8),
        agent=object(),
        notifier=notifier,
        game_mode_process_probe=lambda: True,
    )
    labels = {"alertname": "ContainerRestartStorm"}
    firing = _alert(status="firing", labels=labels)
    response = await service.ingest(
        AlertmanagerPayload.model_validate(
            {"status": "firing", "alerts": [firing.model_dump(mode="json", by_alias=True)]}
        )
    )
    assert response.suppressed == 1
    assert response.accepted == 0
    assert notifier.calls == []

    assert service._game_mode_suppression(firing) is not None
    key = "game_mode:suppression:" + alert_fingerprint(firing)
    record = json.loads(store.get_setting(key))
    assert record["status"] == "suppressed"

    resolved = _alert(status="resolved", labels=labels)
    await service.ingest(
        AlertmanagerPayload.model_validate(
            {"status": "resolved", "alerts": [resolved.model_dump(mode="json", by_alias=True)]}
        )
    )
    record = json.loads(store.get_setting(key))
    assert record["status"] == "resolved"
    assert notifier.calls == []
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_game_mode_allowlist_does_not_cover_real_host_or_user_alerts(tmp_path) -> None:
    config = replace(
        ControlPlaneConfig(),
        api_key="test-key",
        game_mode_state_path=tmp_path / "state.json",
    )
    store = Store(tmp_path / "control-plane.db")
    service = RepairService(config, store, Budget(store, 10, 8), agent=object())
    assert service._game_mode_target_allowed(
        _alert(
            status="firing",
            labels={"alertname": "PrometheusScrapeFailed", "job": "control-plane"},
        )
    ) is False
    assert service._game_mode_target_allowed(
        _alert(
            status="firing",
            labels={"alertname": "PrometheusScrapeFailed", "job": "prometheus"},
        )
    ) is True
    assert service._game_mode_suppression(
        _alert(status="firing", labels={"alertname": "HighCPU"})
    ) is None
    await service.close()
    store.close()
