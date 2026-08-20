from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from control_plane.metrics import NOTIFY_FAILURES
from control_plane.notify import Notifier


def _config(script_path):
    return SimpleNamespace(
        notification_enabled=True,
        feishu_notify_script=script_path,
    )


def _count(reason: str) -> float:
    return NOTIFY_FAILURES.labels(reason=reason)._value.get()


def test_notify_script_missing_counts(tmp_path) -> None:
    notifier = Notifier(_config(tmp_path / "missing-notify.ps1"))
    before = _count("script_missing")
    asyncio.run(notifier.notify("info", "title", "text"))
    assert _count("script_missing") == before + 1


def test_notify_spawn_or_timeout_counts(tmp_path) -> None:
    script = tmp_path / "notify.ps1"
    script.write_text("# placeholder")
    notifier = Notifier(_config(script))
    before = _count("spawn_or_timeout")
    with patch(
        "control_plane.notify.asyncio.create_subprocess_exec",
        side_effect=OSError("spawn failed"),
    ):
        asyncio.run(notifier.notify("info", "title", "text"))
    assert _count("spawn_or_timeout") == before + 1
