"""Read-only operational inspection for the personal Windows profile.

The inspector deliberately reports facts and escalation guidance only.  It has
no repair capabilities: process/service enablement, Docker garbage collection,
and unrelated security baselines stay outside this provider. The probe focuses
on local operational reliability, repository synchronization, recovery facts,
and the configured v2rayN/Docker facts.
"""

from __future__ import annotations

# Probe commands and Chinese escalation text are intentionally kept readable;
# their long lines are bounded and contain no secret values.
# ruff: noqa: E501, RUF001
import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig
from .game_mode import read_game_mode_state

CHECK_NAMES = (
    "recoverability",
    "synchronization",
    "known_garbage",
    "automatic_handling",
    "codex_primary",
    "docker_exited_containers",
    "docker_build_cache",
    "v2rayn_path",
    "v2rayn_status",
)

HISTORICAL_SAFETY_HINT_ALERT_NAMES = frozenset(
    {
        "ControlPlaneCodexUnavailable",
        "ControlPlaneCodexLegacyPath",
        "ControlPlaneReadinessDegraded",
        "ControlPlaneReadyProviderMismatch",
        "ControlPlaneRecoverabilityDegraded",
        "ControlPlaneSynchronizationDegraded",
        "ControlPlaneAutomaticHandlingUnavailable",
        "DockerExitedContainers",
        "DockerBuildCacheAccumulating",
        "V2rayNPathDrift",
        "V2rayNStatusDrift",
    }
)


@dataclass(frozen=True, slots=True)
class CheckObservation:
    name: str
    status: str
    severity: str
    automation: str
    detail: str
    manual_action: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "severity": self.severity,
            "automation": self.automation,
            "detail": self.detail,
            "manual_action": self.manual_action,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    checked_at: float
    observations: tuple[CheckObservation, ...]
    probe_error: str = ""

    @property
    def problems(self) -> tuple[CheckObservation, ...]:
        return tuple(item for item in self.observations if item.status == "problem")

    @property
    def unknowns(self) -> tuple[CheckObservation, ...]:
        return tuple(item for item in self.observations if item.status == "unknown")

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "probe_error": self.probe_error,
            "problems": [item.name for item in self.problems],
            "unknowns": [item.name for item in self.unknowns],
            "checks": [item.as_dict() for item in self.observations],
        }


ProbeRunner = Callable[[], Mapping[str, Any]]


def _unknown(
    name: str,
    detail: str,
    *,
    configured: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> CheckObservation:
    return CheckObservation(
        name=name,
        status="unknown",
        severity="warning",
        automation="codex-judgment",
        detail=detail,
        manual_action="保留当前证据；告警路径必须先调用 Codex 判断可逆性，未明确可逆前不要自动修复。",
        metadata={"configured": configured, **dict(metadata or {})},
    )


def _ok(
    name: str,
    detail: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> CheckObservation:
    return CheckObservation(
        name=name,
        status="ok",
        severity="info",
        automation="none",
        detail=detail,
        manual_action="无需处理；保留最近一次只读核验记录。",
        metadata={"configured": True, **dict(metadata or {})},
    )


def _problem(
    name: str,
    detail: str,
    manual_action: str,
    *,
    severity: str = "warning",
    automation: str = "codex-judgment",
    metadata: Mapping[str, Any] | None = None,
) -> CheckObservation:
    return CheckObservation(
        name=name,
        status="problem",
        severity=severity,
        automation=automation,
        detail=detail,
        manual_action=manual_action,
        metadata={"configured": True, **dict(metadata or {})},
    )


def _lifecycle_observations(
    config: ControlPlaneConfig,
    payload: Mapping[str, Any],
    *,
    provider_health: Mapping[str, Any] | None,
) -> list[CheckObservation]:
    """Evaluate the four standing responsibilities of this profile.

    Recovery and synchronization are evidence-producing checks, not automatic
    safety decisions. Problem/unknown observations enter the alert path, which
    must obtain a current Codex judgment before any effect. Only the exact
    configured known-garbage set is eligible for the reversible automatic
    handler; the inspector never performs that handler itself.
    """

    observations: list[CheckObservation] = []

    recovery_value = payload.get("recovery_ok")
    if recovery_value is True:
        observations.append(
            _ok(
                "recoverability",
                "recovery sources are present and readable",
                metadata={"path_count": len(config.recovery_paths)},
            )
        )
    elif recovery_value is False:
        missing_raw = payload.get("recovery_missing_paths")
        missing = (
            [str(item) for item in missing_raw if item]
            if isinstance(missing_raw, list)
            else []
        )
        observations.append(
            _problem(
                "recoverability",
                f"recovery source missing or unreadable: {len(missing)}",
                "先恢复 ratio 文档源与 Docker 备份可读性，再执行任何环境变更；不得把缺少备份当作可恢复。",
                severity="critical",
                metadata={"missing_count": len(missing), "missing_paths": missing},
            )
        )
    else:
        observations.append(
            _unknown("recoverability", "恢复源未能核验", configured=bool(config.recovery_paths))
        )

    sync_value = payload.get("synchronization_ok")
    if sync_value is True:
        observations.append(
            _ok(
                "synchronization",
                "configured repositories and chezmoi source are synchronized",
                metadata={
                    "checked_count": int(payload.get("synchronization_checked", 0) or 0),
                    "subjects": payload.get("synchronization_subjects", []),
                },
            )
        )
    elif sync_value is False:
        failures_raw = payload.get("synchronization_failures")
        failures = (
            [str(item) for item in failures_raw if item]
            if isinstance(failures_raw, list)
            else []
        )
        observations.append(
            _problem(
                "synchronization",
                f"synchronization drift detected: {len(failures)}",
                "每个 subject 先由 diagnosis Codex 判断；若可逆、owner/project 明确且工作树干净，交给精确 capability 做 fast-forward 或 exact push，并在 effect 后重新核验；首轮发现不可逆或仓库不干净立即通知飞书。",
                severity="critical",
                metadata={
                    "failure_count": len(failures),
                    "failures": failures,
                    "failure_reasons": payload.get("synchronization_failure_reasons", []),
                    "checked_count": int(payload.get("synchronization_checked", 0) or 0),
                    "subjects": payload.get("synchronization_subjects", []),
                },
            )
        )
    else:
        observations.append(
            _unknown(
                "synchronization",
                "仓库/chezmoi 同步状态未能核验",
                configured=bool(config.synchronization_paths),
                metadata={"subjects": payload.get("synchronization_subjects", [])},
            )
        )

    garbage_value = payload.get("known_garbage_count")
    if isinstance(garbage_value, (int, float)):
        paths_raw = payload.get("known_garbage_paths")
        paths = (
            [str(item) for item in paths_raw if item]
            if isinstance(paths_raw, list)
            else []
        )
        if int(garbage_value) > 0:
            observations.append(
                _problem(
                    "known_garbage",
                    f"known generated garbage paths present: {int(garbage_value)}",
                    "control-plane 仅可将配置中的精确路径移入可恢复隔离区；其他缓存、容器、卷、镜像和历史经验必须人工确认，不得扩大清理范围。",
                    automation="automatic",
                    metadata={"count": int(garbage_value), "paths": paths},
                )
            )
        else:
            observations.append(
                _ok("known_garbage", "no configured known-garbage path is present")
            )
    else:
        observations.append(
            _unknown(
                "known_garbage",
                "已知垃圾路径未能核验",
                configured=bool(config.known_garbage_paths),
            )
        )

    if not config.automatic_handling_enabled:
        observations.append(
            _problem(
                "automatic_handling",
                "automatic handling is disabled by configuration",
                "恢复 control-plane 自动处理开关并验证 allowlist；告警仍须先经 Codex 当前安全性判断，自动处理不可用时不得产生 effect，等待明确指令。",
                severity="critical",
            )
        )
    else:
        operations = dict(provider_health or {}).get("personal-operations")
        available = operations.get("available") if isinstance(operations, dict) else None
        if available is True:
            observations.append(
                _ok("automatic_handling", "personal operations handler is available")
            )
        elif available is False:
            observations.append(
                _problem(
                    "automatic_handling",
                    "personal operations handler is unavailable",
                    "自动处理能力不可用时只保留告警与人工 Work，不得绕过 Agent Kernel 直接执行 effect。",
                    severity="critical",
                )
            )
        else:
            observations.append(
                _unknown("automatic_handling", "自动处理 provider 健康状态未能核验")
            )

    return observations


def _parse_size(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return 0
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    for unit, multiplier in units.items():
        if text.endswith(unit):
            try:
                return max(0, int(float(text[: -len(unit)]) * multiplier))
            except ValueError:
                return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        return 0


def _path_status(payload: Mapping[str, Any], expected: str | None) -> tuple[bool | None, str]:
    actual = str(payload.get("v2rayn_path", "")).strip()
    running = payload.get("v2rayn_running")
    if not actual:
        if running is True:
            return None, f"v2rayN 进程正在运行，但可执行路径不可读；expected={expected or '<not configured>'}"
        if expected and Path(expected).is_file():
            return True, f"expected={expected} exists; v2rayN process is not running"
        return None, "v2rayN 进程路径/状态未能核验"
    if expected:
        expected_path = os.path.normcase(os.path.abspath(expected))
        actual_path = os.path.normcase(os.path.abspath(actual)) if actual else ""
        return actual_path == expected_path, f"actual={actual or '<missing>'} expected={expected}"
    return None, f"actual={actual or '<missing>'}; expected path 未配置"


def evaluate_environment(
    config: ControlPlaneConfig,
    payload: Mapping[str, Any],
    *,
    provider_health: Mapping[str, Any] | None = None,
    game_mode_suppresses_docker: bool = False,
) -> EnvironmentSnapshot:
    """Convert raw read-only probe data into stable, alertable observations."""

    provider = dict(provider_health or {}).get("codex-primary")
    provider_available = provider.get("available") if isinstance(provider, dict) else None
    cli = Path(config.codex_cli)
    resolved_cli = shutil.which(str(cli)) or (str(cli) if cli.is_file() else "")
    cli_text = str(resolved_cli or cli)
    legacy_path = any(
        token in cli_text.lower()
        for token in ("opencodex", "open-codex", "old-codex", "legacy-codex")
    )
    if provider_available is False:
        observations = [
            _problem(
                "codex_primary",
                f"codex-primary unavailable; cli={cli_text}",
                "确认当前官方 Codex CLI 路径、版本和网关；人工恢复后重新探测。不要切回旧 OpenCodex 路径。",
                severity="critical",
                metadata={"cli": cli_text, "provider_available": False},
            )
        ]
    elif legacy_path or (not resolved_cli and not cli.is_file()):
        observations = [
            _problem(
                "codex_primary",
                f"Codex CLI path is missing or legacy: {cli_text}",
                "人工确认官方 Codex CLI 安装路径与版本，并更新 control-plane 配置；不要自动安装/升级。",
                severity="critical",
                metadata={"cli": cli_text, "provider_available": provider_available},
            )
        ]
    else:
        observations = [
            _ok(
                "codex_primary",
                f"provider_available={provider_available}; cli={cli_text}",
                metadata={"cli": cli_text, "provider_available": provider_available},
            )
        ]

    docker_available = payload.get("docker_available")
    exited = payload.get("docker_exited_count")
    cache_raw = payload.get("docker_build_cache_bytes", payload.get("docker_build_cache_size"))
    cache_bytes = _parse_size(cache_raw)
    if game_mode_suppresses_docker:
        observations.extend(
            [
                _ok(
                    "docker_exited_containers",
                    "Docker containers are intentionally stopped by an active game session",
                    metadata={"expected_down": True, "suppressed_by_game_mode": True},
                ),
                _ok(
                    "docker_build_cache",
                    "Docker Desktop is intentionally stopped by an active game session",
                    metadata={"expected_down": True, "suppressed_by_game_mode": True},
                ),
            ]
        )
    elif docker_available is False:
        observations.extend(
            [
                _unknown("docker_exited_containers", "Docker CLI/daemon 未能核验"),
                _unknown("docker_build_cache", "Docker CLI/daemon 未能核验"),
            ]
        )
    elif isinstance(exited, (int, float)):
        exited_names_raw = payload.get("docker_exited_container_names")
        exited_names = (
            [str(item) for item in exited_names_raw if item]
            if isinstance(exited_names_raw, list)
            else []
        )
        expected_names = set(config.docker_expected_exited_containers)
        unexpected_names = [name for name in exited_names if name not in expected_names]
        expected_only = int(exited) > 0 and bool(exited_names) and not unexpected_names
        if int(exited) <= 0:
            observations.append(
                _ok("docker_exited_containers", "no exited containers", metadata={"exited_count": 0})
            )
        elif expected_only:
            observations.append(
                _ok(
                    "docker_exited_containers",
                    "only configured one-shot containers are exited",
                    metadata={
                        "exited_count": 0,
                        "total_exited_count": int(exited),
                        "expected_down": True,
                        "expected_containers": exited_names,
                    },
                )
            )
        else:
            observations.append(
                _problem(
                    "docker_exited_containers",
                    f"unexpected exited containers={len(unexpected_names) if exited_names else int(exited)}",
                    "人工检查退出原因和 compose 期望状态；可在 allowlisted 项目内人工决定是否重启，但不要删除容器、卷或镜像。",
                    metadata={
                        "exited_count": len(unexpected_names) if exited_names else int(exited),
                        "total_exited_count": int(exited),
                        "expected_containers": sorted(expected_names),
                        "unexpected_containers": unexpected_names,
                    },
                )
            )
        if cache_raw in (None, ""):
            observations.append(_unknown("docker_build_cache", "build cache 未能核验"))
        elif cache_bytes > config.docker_build_cache_max_bytes:
            observations.append(
                _problem(
                    "docker_build_cache",
                    f"build cache bytes={cache_bytes} exceeds threshold={config.docker_build_cache_max_bytes}",
                    "人工评估缓存用途与磁盘压力；不要自动执行 docker builder prune 或删除 Docker 数据。",
                    metadata={"bytes": cache_bytes, "threshold_bytes": config.docker_build_cache_max_bytes},
                )
            )
        else:
            observations.append(
                _ok(
                    "docker_build_cache",
                    f"build cache bytes={cache_bytes}",
                    metadata={"bytes": cache_bytes, "threshold_bytes": config.docker_build_cache_max_bytes},
                )
            )
    else:
        observations.extend(
            [
                _unknown("docker_exited_containers", "退出容器数量未能核验"),
                _unknown("docker_build_cache", "build cache 未能核验"),
            ]
        )

    path_match, path_detail = _path_status(payload, config.v2rayn_expected_path)
    if path_match is True:
        observations.append(_ok("v2rayn_path", path_detail))
    elif path_match is False:
        observations.append(
            _problem(
                "v2rayn_path",
                f"v2rayN path drift: {path_detail}",
                "人工确认实际 v2rayN.exe 路径、启动方式和配置归属；不要自动移动、替换或升级客户端。",
                metadata={"actual_path": payload.get("v2rayn_path", ""), "expected_path": config.v2rayn_expected_path or ""},
            )
        )
    else:
        observations.append(_unknown("v2rayn_path", path_detail, configured=bool(config.v2rayn_expected_path)))
    if payload.get("v2rayn_running") is True:
        observations.append(_ok("v2rayn_status", "v2rayN process is running"))
    elif payload.get("v2rayn_running") is False and config.v2rayn_expected_path:
        observations.append(
            _problem(
                "v2rayn_status",
                "v2rayN expected process is not running",
                "人工确认 v2rayN 启动任务、实际路径和代理出口；不要自动启动未知程序。",
            )
        )
    else:
        observations.append(_unknown("v2rayn_status", "v2rayN 运行状态未配置或未能核验", configured=bool(config.v2rayn_expected_path)))

    observations = _lifecycle_observations(
        config,
        payload,
        provider_health=provider_health,
    ) + observations
    return EnvironmentSnapshot(checked_at=time.time(), observations=tuple(observations))


class EnvironmentInspectionProvider:
    """Read-only provider that turns local operational facts into metrics."""

    def __init__(
        self,
        config: ControlPlaneConfig,
        *,
        probe_runner: ProbeRunner | None = None,
    ) -> None:
        self.config = config
        self._probe_runner = probe_runner or self._run_local_probe
        self._provider_health: dict[str, Any] = {}
        self._snapshot: EnvironmentSnapshot | None = None
        self._lock = asyncio.Lock()
        self._descriptor = ProviderDescriptor(
            id="environment-inspection",
            name="Personal Environment Inspection",
            version="1.0.0",
            capabilities=["environment.inspect"],
            priority=30,
            tags={"personal-profile", "read-only", "verification"},
            effect_semantics="pure",
            side_effect_class="pure",
            reversibility="reversible",
            provider_family="environment-inspection",
            execution_domain="windows-local",
            trust_boundary="read-only-probe",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def set_provider_health(self, health: Mapping[str, Any]) -> None:
        self._provider_health = {
            str(item.get("provider_id")): dict(item)
            for item in health.values()
            if isinstance(item, dict) and item.get("provider_id")
        }

    @property
    def snapshot(self) -> EnvironmentSnapshot | None:
        return self._snapshot

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.descriptor.id,
            available=True,
            detail="read-only environment probe ready",
            metadata={
                "last_checked_at": self._snapshot.checked_at if self._snapshot else 0,
                "problem_count": len(self._snapshot.problems) if self._snapshot else 0,
                "unknown_count": len(self._snapshot.unknowns) if self._snapshot else 0,
            },
        )

    async def refresh(self, *, force: bool = False) -> EnvironmentSnapshot:
        now = time.time()
        if (
            not force
            and self._snapshot is not None
            and now - self._snapshot.checked_at < self.config.environment_cache_seconds
        ):
            return self._snapshot
        async with self._lock:
            now = time.time()
            if (
                not force
                and self._snapshot is not None
                and now - self._snapshot.checked_at < self.config.environment_cache_seconds
            ):
                return self._snapshot
            probe_error = ""
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(self._probe_runner),
                    timeout=self.config.environment_probe_timeout_seconds,
                )
                snapshot = evaluate_environment(
                    self.config,
                    payload,
                    provider_health=self._provider_health,
                    game_mode_suppresses_docker=(
                        self.config.game_mode_enabled
                        and read_game_mode_state(
                            self.config.game_mode_state_path,
                            active_max_age_seconds=self.config.game_mode_active_max_age_seconds,
                            restore_grace_seconds=self.config.game_mode_restore_grace_seconds,
                        ).suppress_alerts
                    ),
                )
            except Exception as exc:  # pragma: no cover - defensive boundary
                probe_error = str(exc)[:500]
                snapshot = EnvironmentSnapshot(
                    checked_at=time.time(),
                    observations=tuple(
                        _unknown(name, f"environment probe failed: {probe_error}") for name in CHECK_NAMES
                    ),
                    probe_error=probe_error,
                )
            self._snapshot = snapshot
            return snapshot

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        del context
        if request.capability != "environment.inspect":
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="unavailable",
                message=f"unsupported capability {request.capability}",
            )
        snapshot = await self.refresh(force=True)
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=json.dumps(snapshot.as_dict(), ensure_ascii=False),
            metadata={"problem_count": len(snapshot.problems), "unknown_count": len(snapshot.unknowns)},
        )

    async def cancel(self, request_id: str) -> None:
        del request_id

    def _run_bounded_command(
        self,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a probe command and terminate its process tree on timeout."""

        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired as exc:
            # Stop only the probe process itself.  A recursive tree kill could
            # terminate an unrelated or still-useful child process.
            with suppress(OSError):
                process.kill()
            with suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=5)
            raise TimeoutError(f"probe command timed out after {timeout:.1f}s") from exc
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

    def _run_local_probe(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {}
        if os.name == "nt":
            payload.update(self._run_windows_probe())
        payload.update(self._run_lifecycle_probe())
        return payload

    def _run_lifecycle_probe(self) -> Mapping[str, Any]:
        """Probe only the configured recovery, sync, and garbage boundaries.

        Git and chezmoi output is reduced to bounded subject evidence: exact
        repository path/project, clean state, branch, and commit SHAs.  No
        command output, diff, or credential is persisted.
        """

        missing_recovery: list[str] = []
        for raw_path in self.config.recovery_paths:
            path = Path(raw_path)
            if not path.exists() or (path.is_dir() and not any(path.iterdir())):
                missing_recovery.append(raw_path)

        sync_failures: list[str] = []
        sync_failure_reasons: list[dict[str, str]] = []
        sync_subjects: list[dict[str, str]] = []
        checked = 0
        git = shutil.which("git.exe") or shutil.which("git")

        def project_for_path(raw_path: str) -> str:
            candidate = Path(raw_path).resolve(strict=False)
            for project, configured in self.config.project_dirs.items():
                if candidate == Path(configured).resolve(strict=False):
                    return project
            if candidate == Path(self.config.chezmoi_source_dir).resolve(strict=False):
                return "chezmoi"
            return ""

        def subject_for_path(raw_path: str) -> dict[str, str]:
            subject = next(
                (item for item in sync_subjects if item["path"] == raw_path),
                None,
            )
            if subject is None:
                subject = {
                    "path": raw_path,
                    "project": project_for_path(raw_path),
                    "remote": "origin",
                    "branch": "main",
                    "head_sha": "",
                    "remote_sha": "",
                    "worktree": "unknown",
                    "status": "unknown",
                    "reason": "",
                }
                sync_subjects.append(subject)
            return subject

        def record_sync_failure(path: str, reason: str) -> None:
            sync_failures.append(path)
            sync_failure_reasons.append({"path": path, "reason": reason})
            subject = subject_for_path(path)
            subject["status"] = "problem"
            subject["reason"] = reason

        for raw_path in self.config.synchronization_paths:
            path = Path(raw_path)
            subject = subject_for_path(raw_path)
            if not path.is_dir() or not (path / ".git").exists() or not git:
                record_sync_failure(raw_path, "not_git_repository_or_git_unavailable")
                continue
            checked += 1
            status = self._run_bounded_command(
                [git, "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            if status.returncode != 0 or status.stdout.strip():
                subject["worktree"] = "dirty" if status.stdout.strip() else "unknown"
                record_sync_failure(
                    raw_path,
                    "worktree_status_failed" if status.returncode != 0 else "uncommitted_worktree",
                )
                continue
            subject["worktree"] = "clean"
            head = self._run_bounded_command(
                [git, "-C", str(path), "rev-parse", "HEAD"],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            remote = self._run_bounded_command(
                [git, "-C", str(path), "ls-remote", "--heads", "origin", "main"],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            head_text = head.stdout.decode("utf-8", errors="replace").strip()
            remote_text = remote.stdout.decode("utf-8", errors="replace").strip()
            remote_sha = remote_text.split()[0] if remote_text else ""
            subject["head_sha"] = head_text[:40]
            subject["remote_sha"] = remote_sha[:40]
            if (
                head.returncode != 0
                or remote.returncode != 0
                or not head_text
                or not remote_sha
                or head_text != remote_sha
            ):
                record_sync_failure(raw_path, "local_head_or_origin_main_mismatch")
            else:
                subject["status"] = "ok"
                subject["reason"] = ""

        chezmoi = shutil.which("chezmoi.exe") or shutil.which("chezmoi")
        chezmoi_path = self.config.chezmoi_source_dir
        chezmoi_subject = subject_for_path(chezmoi_path)
        if chezmoi:
            verify = self._run_bounded_command(
                [chezmoi, "verify", "--skip-secrets", "--no-tty", "--source", chezmoi_path],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            if verify.returncode != 0:
                chezmoi_subject["chezmoi_verify"] = "failed"
                record_sync_failure(chezmoi_path, "chezmoi_verify_failed")
            else:
                chezmoi_subject["chezmoi_verify"] = "ok"
        else:
            chezmoi_subject["chezmoi_verify"] = "unavailable"
            record_sync_failure(chezmoi_path, "chezmoi_unavailable")

        known_garbage = [
            raw_path for raw_path in self.config.known_garbage_paths if Path(raw_path).exists()
        ]
        return {
            "recovery_ok": bool(self.config.recovery_paths) and not missing_recovery,
            "recovery_missing_paths": missing_recovery,
            "synchronization_ok": bool(self.config.synchronization_paths)
            and not sync_failures,
            "synchronization_failures": sync_failures,
            "synchronization_failure_reasons": sync_failure_reasons,
            "synchronization_checked": checked,
            "synchronization_subjects": sync_subjects,
            "known_garbage_count": len(known_garbage),
            "known_garbage_paths": known_garbage,
        }

    def _run_windows_probe(self) -> Mapping[str, Any]:
        script = """
$ErrorActionPreference = 'SilentlyContinue'
$v2 = @(Get-CimInstance Win32_Process -Filter \"Name='v2rayN.exe'\" | ForEach-Object {
  $path = [string]$_.ExecutablePath
  if (-not $path) { try { $path = [string](Get-Process -Id $_.ProcessId -ErrorAction Stop).Path } catch { } }
  [pscustomobject]@{ ProcessId = $_.ProcessId; ExecutablePath = $path }
})
$docker = Get-Command docker -ErrorAction SilentlyContinue
$dockerAvailable = $false
$exited = $null
$exitedItems = @()
$cache = $null
if ($docker) {
  docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
  $dockerAvailable = $LASTEXITCODE -eq 0
}
if ($dockerAvailable) {
  $exitedItems = @(docker ps -a --filter status=exited --format '{{json .}}' | ConvertFrom-Json)
  $exited = $exitedItems.Count
  $cache = @(docker system df --format '{{json .}}' 2>$null | ConvertFrom-Json | Where-Object { $_.Type -match 'Build Cache' } | Select-Object -ExpandProperty Size -First 1)
}
[pscustomobject]@{
  docker_available = $dockerAvailable
  docker_exited_count = $exited
  docker_exited_container_names = @($exitedItems | ForEach-Object { [string]$_.Names })
  docker_build_cache_size = if ($cache) { [string]$cache } else { '' }
  v2rayn_running = ($v2.Count -gt 0)
  v2rayn_path = if ($v2.Count -gt 0) { [string]$v2[0].ExecutablePath } else { '' }
} | ConvertTo-Json -Depth 5 -Compress
"""
        executable = shutil.which("powershell.exe") or shutil.which("pwsh") or "powershell.exe"
        result = self._run_bounded_command(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            timeout=self.config.environment_probe_timeout_seconds,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).decode("utf-8", errors="replace")[:500])
        raw = result.stdout.decode("utf-8", errors="replace").strip()
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "CHECK_NAMES",
    "HISTORICAL_SAFETY_HINT_ALERT_NAMES",
    "CheckObservation",
    "EnvironmentInspectionProvider",
    "EnvironmentSnapshot",
    "evaluate_environment",
]
