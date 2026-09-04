from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

GameModePhase = Literal["inactive", "active", "restore_grace"]


@dataclass(frozen=True, slots=True)
class GameModeStatus:
    """Bounded, fail-open view of the CS2 launcher state file."""

    phase: GameModePhase
    status: str
    reason: str
    state_path: Path
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def suppress_alerts(self) -> bool:
        return self.phase != "inactive"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_cs2_running() -> bool:
    """Return whether Windows currently reports a ``cs2.exe`` process.

    The control plane has no psutil dependency. ``tasklist`` is a fixed,
    read-only Windows query; a missing executable, timeout, or malformed output
    fails open (``False``), so a stale game-mode state cannot silence alerts.
    """

    tasklist = shutil.which("tasklist")
    if not tasklist:
        return False
    try:
        result = subprocess.run(
            [tasklist, "/FI", "IMAGENAME eq cs2.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any(
        line.strip() and "cs2.exe" in line.lower()
        for line in result.stdout.splitlines()
    )


def read_game_mode_state(
    state_path: Path,
    *,
    now: datetime | None = None,
    active_max_age_seconds: int = 12 * 60 * 60,
    restore_grace_seconds: int = 10 * 60,
    process_probe: Callable[[], bool] = is_cs2_running,
) -> GameModeStatus:
    """Read the launcher state and return an alert-suppression phase.

    The state file is an advisory signal only. ``Active`` requires a fresh
    timestamp *and* a live ``cs2.exe`` process. ``Restored`` is suppressible
    only during a short, bounded post-restore grace period. Any malformed,
    missing, stale, future-dated, or failed-restore state returns ``inactive``.
    """

    path = Path(state_path)

    def inactive(reason: str, status: str = "") -> GameModeStatus:
        return GameModeStatus(
            phase="inactive",
            status=status,
            reason=reason,
            state_path=path,
        )

    try:
        raw = path.read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
    except (OSError, UnicodeError):
        return inactive("state file unavailable")
    except json.JSONDecodeError:
        return inactive("state file is not valid JSON")
    if not isinstance(payload, dict):
        return inactive("state file is not an object")

    status = str(payload.get("Status", "") or "")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    started_at = _parse_timestamp(payload.get("StartedAt"))
    completed_at = _parse_timestamp(payload.get("CompletedAt"))

    if status == "Active":
        if started_at is None:
            return inactive("active state has no valid StartedAt", status)
        age = (current - started_at).total_seconds()
        if age < 0 or age > max(0, active_max_age_seconds):
            return inactive("active state is stale or future-dated", status)
        try:
            running = bool(process_probe())
        except Exception:  # pragma: no cover - defensive boundary for host probes
            running = False
        if not running:
            return inactive("cs2.exe process not found", status)
        return GameModeStatus(
            phase="active",
            status=status,
            reason="fresh active state and cs2.exe process present",
            state_path=path,
            started_at=started_at,
        )

    if status == "Restored":
        if completed_at is None:
            return inactive("restored state has no valid CompletedAt", status)
        age = (current - completed_at).total_seconds()
        if 0 <= age <= max(0, restore_grace_seconds):
            return GameModeStatus(
                phase="restore_grace",
                status=status,
                reason="Docker restore grace period is active",
                state_path=path,
                started_at=started_at,
                completed_at=completed_at,
            )
        return inactive("restore grace period expired", status)

    if status == "RestoreFailed":
        return inactive("game-mode restore failed; alerts remain enabled", status)
    return inactive("unknown game-mode state", status)
