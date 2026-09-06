from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

GameModePhase = Literal["inactive", "active", "restore_grace"]
STEAM_LIBRARY_CACHE_SECONDS = 60.0
_steam_game_roots_cache: tuple[Path, ...] = ()
_steam_game_roots_cached_at = 0.0


@dataclass(frozen=True, slots=True)
class GameModeStatus:
    """Bounded, fail-safe view of an explicit game-session state owner."""

    phase: GameModePhase
    status: str
    reason: str
    state_path: Path
    started_at: datetime | None = None
    completed_at: datetime | None = None
    process_names: tuple[str, ...] = ()
    process_ids: tuple[int, ...] = ()
    docker_expected_down: bool = False

    @property
    def suppress_alerts(self) -> bool:
        return self.phase != "inactive" and self.docker_expected_down


@dataclass(frozen=True, slots=True)
class GameSessionSignal:
    """A strict, read-only signal for one foreground Steam game session."""

    process_name: str
    process_path: Path
    game_root: Path
    foreground_pid: int
    docker_expected_down: bool


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


def _normalise_process_names(value: Any) -> tuple[str, ...]:
    raw = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        name = Path(item.strip()).name.lower()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _normalise_process_ids(value: Any) -> tuple[int, ...]:
    raw = value if isinstance(value, list) else [value]
    ids: list[int] = []
    for item in raw:
        try:
            process_id = int(item)
        except (TypeError, ValueError):
            continue
        if process_id > 0 and process_id not in ids:
            ids.append(process_id)
    return tuple(ids)


def _steam_roots_from_registry() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    try:
        import winreg

        roots: list[Path] = []
        for hive, key_path in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, "SteamPath")
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                roots.append(Path(value.strip()))
        roots.append(Path(r"C:\Program Files (x86)\Steam"))
        return tuple(
            path
            for path in dict.fromkeys(roots)
            if path.is_dir()
        )
    except (ImportError, OSError):
        return ()


def _steam_library_roots() -> tuple[Path, ...]:
    roots = list(_steam_roots_from_registry())
    libraries: list[Path] = []
    for root in roots:
        libraries.append(root)
        vdf_path = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            raw_path = match.group(1).replace("\\\\", "\\")
            candidate = Path(raw_path)
            if candidate.is_dir():
                libraries.append(candidate)
    return tuple(dict.fromkeys(libraries))


def _steam_game_roots() -> tuple[Path, ...]:
    global _steam_game_roots_cache, _steam_game_roots_cached_at
    now = time.monotonic()
    if (
        _steam_game_roots_cache
        and now - _steam_game_roots_cached_at < STEAM_LIBRARY_CACHE_SECONDS
    ):
        return _steam_game_roots_cache
    game_roots: list[Path] = []
    for library in _steam_library_roots():
        steamapps = library / "steamapps"
        common = steamapps / "common"
        if not common.is_dir():
            continue
        for manifest in steamapps.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            app_id = re.search(r'"appid"\s+"([^"]+)"', text)
            install_dir = re.search(r'"installdir"\s+"([^"]+)"', text)
            game_name = re.search(r'"name"\s+"([^"]+)"', text)
            if not install_dir or (app_id and app_id.group(1) == "228980"):
                continue
            if game_name and game_name.group(1).strip().lower() == "steamworks shared":
                continue
            candidate = (common / install_dir.group(1)).resolve()
            try:
                if candidate.is_dir() and candidate.is_relative_to(common.resolve()):
                    game_roots.append(candidate)
            except (OSError, ValueError):
                continue
    _steam_game_roots_cache = tuple(dict.fromkeys(game_roots))
    _steam_game_roots_cached_at = now
    return _steam_game_roots_cache


def _foreground_process() -> tuple[int, Path] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        window = user32.GetForegroundWindow()
        if not window or not user32.IsWindowVisible(window):
            return None
        process_id = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(window, ctypes.byref(process_id)):
            return None
        handle = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            return int(process_id.value), Path(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None


def _docker_desktop_is_stopped() -> bool:
    """Return true only when the daemon and Desktop process are both absent."""

    if os.name != "nt":
        return False
    docker = shutil.which("docker.exe") or shutil.which("docker")
    tasklist = shutil.which("tasklist")
    if not docker or not tasklist:
        return False
    try:
        daemon = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=2,
            check=False,
        )
        if daemon.returncode == 0:
            return False
        processes = subprocess.run(
            [tasklist, "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if processes.returncode != 0:
        return False
    desktop_processes = {
        "docker desktop.exe",
        "com.docker.backend.exe",
        "com.docker.proxy.exe",
        "com.docker.service.exe",
    }
    for row in csv.reader(processes.stdout.splitlines()):
        if row and Path(row[0].strip()).name.lower() in desktop_processes:
            return False
    return True


def detect_steam_game_session() -> GameSessionSignal | None:
    """Detect only an installed Steam game owning the visible foreground window."""

    if not _docker_desktop_is_stopped():
        return None
    foreground = _foreground_process()
    if foreground is None:
        return None
    foreground_pid, process_path = foreground
    if process_path.name.lower() in {
        "steam.exe",
        "steamwebhelper.exe",
        "steamservice.exe",
        "steamcmd.exe",
        "steamerrorreporter.exe",
    }:
        return None
    try:
        resolved_process = process_path.resolve()
    except OSError:
        return None
    for game_root in _steam_game_roots():
        try:
            if resolved_process.is_relative_to(game_root):
                return GameSessionSignal(
                    process_name=process_path.name,
                    process_path=resolved_process,
                    game_root=game_root,
                    foreground_pid=foreground_pid,
                    docker_expected_down=True,
                )
        except (OSError, ValueError):
            continue
    return None


def is_game_process_running(
    process_names: tuple[str, ...], process_ids: tuple[int, ...]
) -> bool:
    """Return whether a state-owner-declared game process is live.

    The control plane has no psutil dependency. ``tasklist`` is a fixed,
    read-only Windows query; a missing executable, timeout, malformed output, or
    missing process identity fails closed (``False``), so a stale or ambiguous
    state cannot silence alerts. The process identity is supplied by the existing
    state owner; this function does not guess whether an arbitrary process is a
    game.
    """

    if not process_names and not process_ids:
        return False
    tasklist = shutil.which("tasklist")
    if not tasklist:
        return False
    try:
        result = subprocess.run(
            [tasklist, "/FO", "CSV", "/NH"],
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
    wanted_names = {Path(name).name.lower() for name in process_names}
    wanted_ids = set(process_ids)
    try:
        rows = csv.reader(result.stdout.splitlines())
        for row in rows:
            if len(row) < 2:
                continue
            image_name = Path(row[0].strip()).name.lower()
            try:
                process_id = int(row[1].strip().replace(",", ""))
            except ValueError:
                process_id = -1
            if image_name in wanted_names or process_id in wanted_ids:
                return True
    except (csv.Error, UnicodeError):
        return False
    return False


def read_game_mode_state(
    state_path: Path | None,
    *,
    now: datetime | None = None,
    active_max_age_seconds: int = 12 * 60 * 60,
    restore_grace_seconds: int = 10 * 60,
    process_probe: Callable[[tuple[str, ...], tuple[int, ...]], bool] = is_game_process_running,
    steam_session_probe: Callable[[], GameSessionSignal | None] = detect_steam_game_session,
) -> GameModeStatus:
    """Read the launcher state and return an alert-suppression phase.

    The state file is an advisory signal only. ``Active`` requires a fresh
    timestamp, a state-owner-declared process identity, a live matching process,
    and an explicit ``DockerExpectedDown=true`` declaration. ``Restored`` is
    suppressible only during a short, bounded post-restore grace period and only
    when that same Docker expectation was declared. Any malformed, missing,
    stale, future-dated, ambiguous, or failed-restore state returns ``inactive``.
    """

    path = Path(state_path) if state_path is not None else Path()

    def inactive(reason: str, status: str = "") -> GameModeStatus:
        return GameModeStatus(
            phase="inactive",
            status=status,
            reason=reason,
            state_path=path,
        )

    if state_path is None:
        signal = steam_session_probe()
        if signal is None:
            return inactive("no reliable Steam game session signal")
        if not signal.docker_expected_down:
            return inactive("Steam game session detected but Docker shutdown is not proven")
        return GameModeStatus(
            phase="active",
            status="AutoSteam",
            reason=(
                "installed Steam game owns the foreground window and Docker Desktop "
                "is stopped"
            ),
            state_path=path,
            started_at=(now or datetime.now(UTC)).astimezone(UTC),
            process_names=(signal.process_name,),
            process_ids=(signal.foreground_pid,),
            docker_expected_down=True,
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
    process_names = _normalise_process_names(
        payload.get("ProcessNames", payload.get("ProcessName"))
    )
    process_ids = _normalise_process_ids(
        payload.get("ProcessIds", payload.get("ProcessId"))
    )
    docker_expected_down = payload.get("DockerExpectedDown") is True

    if status == "Active":
        if started_at is None:
            return inactive("active state has no valid StartedAt", status)
        age = (current - started_at).total_seconds()
        if age < 0 or age > max(0, active_max_age_seconds):
            return inactive("active state is stale or future-dated", status)
        if not process_names and not process_ids:
            return inactive("active state has no declared game process identity", status)
        if not docker_expected_down:
            return inactive("active state does not declare expected Docker shutdown", status)
        try:
            running = bool(process_probe(process_names, process_ids))
        except Exception:  # pragma: no cover - defensive boundary for host probes
            running = False
        if not running:
            return inactive("declared game process not found", status)
        return GameModeStatus(
            phase="active",
            status=status,
            reason="fresh active state, declared game process present, Docker shutdown expected",
            state_path=path,
            started_at=started_at,
            process_names=process_names,
            process_ids=process_ids,
            docker_expected_down=True,
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
                process_names=process_names,
                process_ids=process_ids,
                docker_expected_down=docker_expected_down,
            )
        return inactive("restore grace period expired", status)

    if status == "RestoreFailed":
        return inactive("game-mode restore failed; alerts remain enabled", status)
    return inactive("unknown game-mode state", status)
