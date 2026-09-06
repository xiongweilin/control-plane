"""Read-only environment inspection for the personal control-plane profile.

The inspector deliberately reports facts and escalation guidance only.  It has
no repair capabilities: process/service enablement, firewall changes, Docker
garbage collection, cloud writes, and security remediation stay outside this
provider and require an explicit human-owned workflow.
"""

from __future__ import annotations

# Probe commands and Chinese escalation text are intentionally kept readable;
# their long lines are bounded and contain no secret values.
# ruff: noqa: E501, RUF001
import asyncio
import json
import os
import shlex
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
    "windows_defender",
    "third_party_protection",
    "ditto_listener",
    "smb_rpc_listeners",
    "windows_recursive_scan",
    "docker_exited_containers",
    "docker_build_cache",
    "v2rayn_path",
    "v2rayn_status",
    "cloud_protected_root",
    "cloud_tailscale_profile",
    "cloud_swap",
    "cloud_selinux",
    "cloud_cve",
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
        "WinDefendStopped",
        "ThirdPartyProtectionUnknown",
        "DittoExposed",
        "SMBOrRPCListenerDetected",
        "ProtectedDirectoryRootVerificationMissing",
        "WindowsRecursiveScanAccessErrors",
        "DockerExitedContainers",
        "DockerBuildCacheAccumulating",
        "V2rayNPathDrift",
        "V2rayNStatusDrift",
        "CloudTailscaleProfileMissing",
        "CloudSwapMissing",
        "CloudSELinuxPermissive",
        "CloudCVEDetected",
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


def _unknown(name: str, detail: str, *, configured: bool = True) -> CheckObservation:
    return CheckObservation(
        name=name,
        status="unknown",
        severity="warning",
        automation="fail-safe",
        detail=detail,
        manual_action="确认探针权限/配置后，由人工执行只读复核；不要据此自动修复。",
        metadata={"configured": configured},
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
    automation: str = "fail-safe",
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

    Recovery and synchronization are evidence gates and therefore fail-safe.
    Only the exact configured known-garbage set is eligible for the reversible
    automatic handler; the inspector never performs that handler itself.
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
                    "checked_count": int(payload.get("synchronization_checked", 0) or 0)
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
                "保留本地与远端证据，按 ratio 清单逐仓库复核；只允许在确认无未提交变更后执行 fast-forward，同步失败不得自动 push 或覆盖本地内容。",
                severity="critical",
                metadata={
                    "failure_count": len(failures),
                    "failures": failures,
                    "checked_count": int(payload.get("synchronization_checked", 0) or 0),
                },
            )
        )
    else:
        observations.append(
            _unknown(
                "synchronization",
                "仓库/chezmoi 同步状态未能核验",
                configured=bool(config.synchronization_paths),
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
                "恢复 control-plane 自动处理开关并验证 allowlist；在自动处理不可用期间，所有异常必须进入 fail-safe 等待人工指令。",
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


def _ports(payload: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    raw = payload.get("listeners")
    if not isinstance(raw, list):
        return None
    return [item for item in raw if isinstance(item, dict)]


def _path_status(payload: Mapping[str, Any], expected: str | None) -> tuple[bool | None, str]:
    actual = str(payload.get("v2rayn_path", "")).strip()
    running = payload.get("v2rayn_running")
    if not actual and not running:
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

    defender = str(payload.get("win_defend_status", "")).strip().lower()
    if defender in {"stopped", "stop", "disabled"}:
        observations.append(
            _problem(
                "windows_defender",
                f"WinDefend status={defender}",
                "人工确认设备安全责任和维护窗口；不要由 control-plane 自动启用杀毒或修改防火墙。",
                severity="critical",
            )
        )
        observations.append(
            _unknown(
                "third_party_protection",
                "WinDefend 未运行，且第三方防护责任/接管状态无法由只读探针证明。",
            )
        )
    elif defender in {"running", "start", "started"}:
        observations.extend(
            [
                _ok("windows_defender", "WinDefend is running"),
                _ok("third_party_protection", "Windows protection owner is observable"),
            ]
        )
    else:
        observations.extend(
            [
                _unknown("windows_defender", "WinDefend 状态未能核验", configured=os.name == "nt"),
                _unknown("third_party_protection", "防护责任未能核验", configured=os.name == "nt"),
            ]
        )

    listeners = _ports(payload)
    if listeners is None:
        observations.append(_unknown("ditto_listener", "监听端口未能核验"))
        observations.append(_unknown("smb_rpc_listeners", "SMB/RPC 监听未能核验"))
    else:
        ditto = [
            item
            for item in listeners
            if int(item.get("port", 0) or 0) == 23443
            and str(item.get("address", "")) in {"0.0.0.0", "::", "*"}
        ]
        smb_rpc = [
            item
            for item in listeners
            if int(item.get("port", 0) or 0) in {135, 139, 445, 593}
        ]
        if ditto:
            observations.append(
                _problem(
                    "ditto_listener",
                    "Ditto is listening on a wildcard address at port 23443",
                    "人工确认 Ditto 是否需要远程监听、绑定范围和 Windows 防火墙策略；不要自动改防火墙或停服务。",
                    severity="critical",
                    metadata={"listeners": ditto},
                )
            )
        else:
            observations.append(_ok("ditto_listener", "no wildcard Ditto listener at port 23443"))
        if smb_rpc:
            observations.append(
                _problem(
                    "smb_rpc_listeners",
                    f"SMB/RPC listeners detected: {len(smb_rpc)}",
                    "人工确认暴露面、网络分区和业务依赖；不要自动关闭端口或修改防火墙。",
                    severity="critical",
                    metadata={"listeners": smb_rpc},
                )
            )
        else:
            observations.append(_ok("smb_rpc_listeners", "no SMB/RPC listener in the inspected port set"))

    scan_errors = payload.get("recursive_scan_access_errors")
    if isinstance(scan_errors, (int, float)):
        if scan_errors > 0:
            observations.append(
                _problem(
                    "windows_recursive_scan",
                    f"recursive scan access errors={int(scan_errors)}",
                    "人工核对 ACL、受保护目录和扫描范围；保留 access errors 证据，不要静默当作全量扫描成功。",
                    metadata={"access_errors": int(scan_errors)},
                )
            )
        else:
            observations.append(_ok("windows_recursive_scan", "recursive scan completed without access errors"))
    else:
        observations.append(
            _unknown(
                "windows_recursive_scan",
                "递归扫描未配置或未能核验",
                configured=bool(config.windows_scan_roots),
            )
        )

    docker_available = payload.get("docker_available")
    exited = payload.get("docker_exited_count")
    cache_raw = payload.get("docker_build_cache_bytes", payload.get("docker_build_cache_size"))
    cache_bytes = _parse_size(cache_raw)
    if game_mode_suppresses_docker:
        observations.extend(
            [
                _ok(
                    "docker_exited_containers",
                    "Docker containers are intentionally stopped by active CS2 game mode",
                    metadata={"expected_down": True, "suppressed_by_game_mode": True},
                ),
                _ok(
                    "docker_build_cache",
                    "Docker Desktop is intentionally stopped by active CS2 game mode",
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
        observations.append(
            _problem(
                "docker_exited_containers",
                f"exited containers={int(exited)}",
                "人工检查退出原因和 compose 期望状态；可在 allowlisted 项目内人工决定是否重启，但不要删除容器、卷或镜像。",
                metadata={"exited_count": int(exited)},
            )
            if exited > 0
            else _ok("docker_exited_containers", "no exited containers", metadata={"exited_count": 0})
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

    cloud = payload.get("cloud")
    cloud_data = dict(cloud) if isinstance(cloud, dict) else {}
    cloud_configured = bool(config.cloud_ssh_target)
    if not cloud_configured:
        observations.extend(
            [
                _unknown("cloud_protected_root", "云端 SSH target 未配置", configured=False),
                _unknown("cloud_tailscale_profile", "云端 SSH target/profile 未配置", configured=False),
                _unknown("cloud_swap", "云端 SSH target 未配置", configured=False),
                _unknown("cloud_selinux", "云端 SSH target 未配置", configured=False),
                _unknown("cloud_cve", "云端 SSH target 未配置", configured=False),
            ]
        )
    else:
        root_checked = cloud_data.get("root_checked") is True
        root_readable = cloud_data.get("root_readable") is True
        if root_checked and root_readable:
            observations.append(_ok("cloud_protected_root", "cloud protected-directory probe includes the root itself"))
        else:
            observations.append(
                _problem(
                    "cloud_protected_root",
                    "cloud protected-directory root-level verification is missing or failed",
                    "人工在云端以最小权限复核保护目录根节点及权限覆盖范围；不要由本机自动修改云端目录/权限。",
                    severity="critical",
                )
            )
        if not config.cloud_tailscale_profile:
            observations.append(
                _unknown(
                    "cloud_tailscale_profile",
                    "云端 Tailscale profile 路径未配置",
                    configured=False,
                )
            )
        elif cloud_data.get("tailscale_profile_present") is True:
            observations.append(_ok("cloud_tailscale_profile", "configured Tailscale profile is present"))
        else:
            observations.append(
                _problem(
                    "cloud_tailscale_profile",
                    "configured cloud Tailscale profile is missing",
                    "人工确认 profile 文件归属、权限和 Tailscale 登录状态；不要自动生成、复制或删除云端凭据。",
                    severity="critical",
                )
            )
        swap = cloud_data.get("swap_present")
        if swap is True:
            observations.append(_ok("cloud_swap", "cloud swap is present"))
        elif swap is False:
            observations.append(
                _problem(
                    "cloud_swap",
                    "cloud host has no swap",
                    "人工评估内存余量和业务风险后配置 swap；不要自动改云主机磁盘或系统配置。",
                )
            )
        else:
            observations.append(_unknown("cloud_swap", "swap state未能核验"))
        selinux = str(cloud_data.get("selinux_mode", "unknown")).lower()
        if selinux == "enforcing":
            observations.append(_ok("cloud_selinux", "SELinux is enforcing"))
        elif selinux == "permissive":
            observations.append(
                _problem(
                    "cloud_selinux",
                    "SELinux is permissive",
                    "人工评估策略兼容性并恢复 enforcing；不要自动切换 SELinux 模式。",
                    severity="critical",
                )
            )
        else:
            observations.append(_unknown("cloud_selinux", f"SELinux mode={selinux}"))
        cves = cloud_data.get("cve_findings")
        if isinstance(cves, (int, float)) and cves > 0:
            observations.append(
                _problem(
                    "cloud_cve",
                    f"cloud CVE findings={int(cves)}",
                    "人工确认 CVE 影响、补丁来源、维护窗口和回滚方案；control-plane 只告警，不自动修复 CVE。",
                    severity="critical",
                    metadata={"findings": int(cves)},
                )
            )
        elif isinstance(cves, (int, float)) and cves == 0:
            observations.append(_ok("cloud_cve", "cloud CVE probe reported no findings"))
        else:
            observations.append(_unknown("cloud_cve", "CVE 状态未能核验"))

    observations = _lifecycle_observations(
        config,
        payload,
        provider_health=provider_health,
    ) + observations
    return EnvironmentSnapshot(checked_at=time.time(), observations=tuple(observations))


class EnvironmentInspectionProvider:
    """Read-only provider that turns local/cloud probe facts into metrics."""

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
        if self.config.cloud_ssh_target:
            payload["cloud"] = self._run_cloud_probe()
        payload.update(self._run_lifecycle_probe())
        return payload

    def _run_lifecycle_probe(self) -> Mapping[str, Any]:
        """Probe only the configured recovery, sync, and garbage boundaries.

        Git and chezmoi output is intentionally discarded.  The probe records
        booleans and bounded subject names, never command output or credentials.
        """

        missing_recovery: list[str] = []
        for raw_path in self.config.recovery_paths:
            path = Path(raw_path)
            if not path.exists() or (path.is_dir() and not any(path.iterdir())):
                missing_recovery.append(raw_path)

        sync_failures: list[str] = []
        checked = 0
        git = shutil.which("git.exe") or shutil.which("git")
        for raw_path in self.config.synchronization_paths:
            path = Path(raw_path)
            if not path.is_dir() or not (path / ".git").exists() or not git:
                sync_failures.append(raw_path)
                continue
            checked += 1
            status = self._run_bounded_command(
                [git, "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            if status.returncode != 0 or status.stdout.strip():
                sync_failures.append(raw_path)
                continue
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
            if (
                head.returncode != 0
                or remote.returncode != 0
                or not head_text
                or not remote_sha
                or head_text != remote_sha
            ):
                sync_failures.append(raw_path)

        chezmoi = shutil.which("chezmoi.exe") or shutil.which("chezmoi")
        if chezmoi:
            verify = self._run_bounded_command(
                [chezmoi, "verify", "--skip-secrets", "--no-tty"],
                timeout=min(self.config.environment_probe_timeout_seconds, 10),
            )
            if verify.returncode != 0:
                sync_failures.append("chezmoi-runtime")
        else:
            sync_failures.append("chezmoi-runtime")

        known_garbage = [
            raw_path for raw_path in self.config.known_garbage_paths if Path(raw_path).exists()
        ]
        return {
            "recovery_ok": bool(self.config.recovery_paths) and not missing_recovery,
            "recovery_missing_paths": missing_recovery,
            "synchronization_ok": bool(self.config.synchronization_paths)
            and not sync_failures,
            "synchronization_failures": sync_failures,
            "synchronization_checked": checked,
            "known_garbage_count": len(known_garbage),
            "known_garbage_paths": known_garbage,
        }

    def _run_windows_probe(self) -> Mapping[str, Any]:
        roots = json.dumps(list(self.config.windows_scan_roots), ensure_ascii=True)
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$service = Get-CimInstance Win32_Service -Filter \"Name='WinDefend'\"
$listeners = @(Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess)
$v2 = @(Get-CimInstance Win32_Process -Filter \"Name='v2rayN.exe'\" | Select-Object ProcessId,ExecutablePath)
$scanErrors = 0
$roots = '{roots}' | ConvertFrom-Json
foreach ($root in @($roots)) {{
  if (Test-Path -LiteralPath $root) {{
    $errors = @()
    Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue -ErrorVariable +errors | Out-Null
    $scanErrors += @($errors).Count
  }}
}}
$docker = Get-Command docker -ErrorAction SilentlyContinue
$dockerAvailable = $false
$exited = $null
$cache = $null
if ($docker) {{
  docker info --format '{{{{.ServerVersion}}}}' 2>$null | Out-Null
  $dockerAvailable = $LASTEXITCODE -eq 0
}}
if ($dockerAvailable) {{
  $exited = @(docker ps -a --filter status=exited --format '{{{{json .}}}}').Count
  $cache = @(docker system df --format '{{{{json .}}}}' 2>$null | ConvertFrom-Json | Where-Object {{ $_.Type -match 'Build Cache' }} | Select-Object -ExpandProperty Size -First 1)
}}
[pscustomobject]@{{
  win_defend_status = if ($service) {{ [string]$service.State }} else {{ '' }}
  listeners = $listeners | ForEach-Object {{ @{{ address = [string]$_.LocalAddress; port = [int]$_.LocalPort }} }}
  recursive_scan_access_errors = $scanErrors
  docker_available = $dockerAvailable
  docker_exited_count = $exited
  docker_build_cache_size = if ($cache) {{ [string]$cache }} else {{ '' }}
  v2rayn_running = ($v2.Count -gt 0)
  v2rayn_path = if ($v2.Count -gt 0) {{ [string]$v2[0].ExecutablePath }} else {{ '' }}
}} | ConvertTo-Json -Depth 5 -Compress
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

    def _run_cloud_probe(self) -> Mapping[str, Any]:
        root = shlex.quote(self.config.cloud_protected_root or "/")
        profile = shlex.quote(self.config.cloud_tailscale_profile or "")
        remote = (
            "root_checked=0; root_readable=0; [ -n "
            + shlex.quote(self.config.cloud_protected_root)
            + " ] && root_checked=1; [ -d "
            + root
            + " ] && [ -r "
            + root
            + " ] && root_readable=1; "
            "tailscale_profile_present=0; [ -n "
            + profile
            + " ] && [ -f "
            + profile
            + " ] && tailscale_profile_present=1; "
            "swap_present=0; command -v swapon >/dev/null 2>&1 && [ -n \"$(swapon --show --noheadings 2>/dev/null)\" ] && swap_present=1; "
            "selinux_mode=unknown; command -v getenforce >/dev/null 2>&1 && selinux_mode=$(getenforce 2>/dev/null); "
            "cve_findings=-1; if command -v dnf >/dev/null 2>&1; then cve_findings=$(dnf -q updateinfo list cves --installed 2>/dev/null | grep -Eo 'CVE-[0-9]{4}-[0-9]+' | sort -u | wc -l); fi; "
            "printf '{\"root_checked\":%s,\"root_readable\":%s,\"tailscale_profile_present\":%s,\"swap_present\":%s,\"selinux_mode\":\"%s\",\"cve_findings\":%s}\n' \"$root_checked\" \"$root_readable\" \"$tailscale_profile_present\" \"$swap_present\" \"$selinux_mode\" \"$cve_findings\""
        )
        args = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.config.cloud_ssh_identity_file:
            args.extend(["-i", self.config.cloud_ssh_identity_file])
        args.extend([self.config.cloud_ssh_target, remote])
        executable = shutil.which("ssh.exe") or shutil.which("ssh") or "ssh"
        result = self._run_bounded_command(
            [executable, *args],
            timeout=self.config.cloud_probe_timeout_seconds,
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
