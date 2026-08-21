from __future__ import annotations

import os
import shutil
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(host: str) -> bool:
    lowered = host.strip().lower()
    return lowered in {"127.0.0.1", "localhost", "::1"} or lowered.startswith("127.")


def _validate_gateway_network(base_url: str) -> None:
    """Enforce the loopback-only network boundary for the model gateway.

    The gateway diagnostics only talk to the local LiteLLM gateway; a
    non-loopback endpoint is rejected outright rather than silently carrying
    unauthenticated model-list requests (OpenCodex 已退役，不再有远端代理形态).
    """
    parsed = urllib.parse.urlparse(base_url)
    if not parsed.hostname:
        return  # malformed URL; the client surfaces a clearer error at call time
    if not _is_loopback_host(parsed.hostname):
        raise ConfigurationError(
            "gateway_base_url must point to loopback; the model gateway "
            "diagnostics only support the local LiteLLM endpoint."
        )


def resolve_codex_cli(explicit: str = "") -> Path:
    """Resolve the Codex CLI entry with stable, explicit priority.

    1. Explicit ``[agent] codex_cli`` configuration wins.
    2. A ``codex`` executable on PATH (scoop shim).
    3. Bare ``codex`` so subprocess resolution fails with a clear message
       surfaced by :meth:`CodexRunner.cli_info`.
    """
    if explicit:
        return Path(explicit)
    found = shutil.which("codex.cmd") or shutil.which("codex")
    if found:
        return Path(found)
    return Path("codex")


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """Runtime configuration for the control plane.

    ``run_id`` is filled in at startup by :func:`control_plane.runtime.bootstrap`
    and is empty for configs constructed directly (e.g. in tests).
    """

    host: str = "127.0.0.1"
    port: int = 18083
    api_key: str = ""
    run_id: str = ""

    gateway_base_url: str = "http://127.0.0.1:4001/v1"
    model: str = "opencode-go/deepseek-v4-flash"
    codex_cli: Path = field(default_factory=lambda: resolve_codex_cli())
    model_preflight_enabled: bool = True
    codex_branch_prefix: str = "fix/control-plane-"
    # ``workspace-write`` Codex sessions run from a detached candidate
    # worktree by default.  The worktree is removed after the session while
    # the candidate branch remains visible to the control plane for review.
    codex_isolate_worktree: bool = True
    # A Codex session must not inherit host Docker or Git/SSH control planes.
    # These are environment-level guards in addition to the Codex sandbox and
    # prompt constraints; the typed providers retain the real credentials.
    codex_disable_docker: bool = True
    codex_disable_ssh_credentials: bool = True
    codex_worktree_root: Path = PROJECT_ROOT.parent / ".control-plane-codex-worktrees"
    agent_session_dir: Path = PROJECT_ROOT / "data" / "agent-sessions"
    gateway_timeout_seconds: int = 120
    # 启动预检失败后，每隔该秒数重试一次模型来源探测，直至全部恢复（失败驱动，非周期探测）。
    # 默认 600s 与 ControlPlaneModelConnectivityDown 的 for: 10m 对齐：恢复后下一次重试即刷新指标。
    model_recovery_retry_seconds: int = 600
    max_agent_calls_per_repair: int = 8

    cooldown_seconds: int = 600
    max_attempts: int = 2
    daily_agent_budget: int = 20
    per_repair_timeout_seconds: int = 900
    max_concurrent: int = 2
    paused: bool = False

    # ---- timeout classification (batch2 item 3) ----
    exec_timeout_seconds: int = 900
    comm_timeout_seconds: int = 30
    verify_timeout_seconds: int = 60
    approval_timeout_seconds: int = 0  # 0 = wait indefinitely for human approval
    git_push_timeout_seconds: int = 120
    lease_ttl_seconds: int = 1_800

    # ---- candidate branches (batch2 item 9) ----
    candidate_branch_prefix: str = "fix/control-plane-"
    candidate_retention_days: int = 14
    candidate_cleanup_policy: str = "manual"  # manual | auto; default keeps branches

    # ---- dirty worktree policy (batch2 item 8) ----
    dirty_worktree_policy: str = "reject"  # reject | isolate

    # ---- output caps (batch2 item 11) ----
    max_agent_output_bytes: int = 200_000

    # ---- github ssh 443 fallback (batch2 item 14) ----
    github_ssh_fallback: bool = True
    github_ssh_host_port: str = "ssh.github.com:443"

    # ---- readiness (batch2 item 6) ----
    alertmanager_url: str = ""

    # ---- side-effect gate (batch2 item 15) ----
    external_side_effects_require_approval: bool = False
    blocked_paths: tuple[str, ...] = (
        ".env",
        ".ssh",
        "credentials",
        "secrets",
        "id_rsa",
        ".pem",
        ".key",
        "token",
        "password",
    )

    allowed_auto_projects: tuple[str, ...] = (
        "dify",
        "docker",
        "feedback-analysis-agent",
        "catalog-ops-automation",
        "observability",
        "feishu-dify-gateway",
    )
    project_dirs: dict[str, str] = field(
        default_factory=lambda: {
            "docker": "D:\\infrastructure\\compose\\dify",
            "dify": "D:\\infrastructure\\compose\\dify",
            "feedback-analysis-agent": "D:\\infrastructure\\compose\\feedback-analysis-agent",
            "catalog-ops-automation": "D:\\infrastructure\\compose\\catalog-ops-automation",
            "observability": "D:\\infrastructure\\compose\\observability",
            "feishu-dify-gateway": "D:\\infrastructure\\compose\\feishu-dify-gateway",
        }
    )
    allowed_repo_roots: tuple[str, ...] = (
        "D:\\infrastructure\\compose",
        "D:\\agent",
        "D:\\agent\\ratio",
    )
    allowed_url_origins: tuple[str, ...] = (
        "http://127.0.0.1",
        "http://localhost",
        "https://metratio.com",
        "https://dify.metratio.com",
        "https://grafana.metratio.com",
        "https://prometheus.metratio.com",
    )
    docker_cleanup_min_reclaimable_gb: float = 5.0

    data_dir: Path = PROJECT_ROOT / "data"
    patch_dir: Path = PROJECT_ROOT / "data" / "patches"
    evidence_dir: Path = PROJECT_ROOT / "data" / "evidence"
    state_db: Path = PROJECT_ROOT / "data" / "control-plane.db"
    pid_file: Path = PROJECT_ROOT / "data" / "control-plane.pid"

    candidate_trial_days: int = 90
    candidate_wip_limit: int = 20
    default_disposition: str = "archive"
    approval_poll_seconds: float = 5.0

    feishu_notify_script: Path = (
        Path(os.getenv("USERPROFILE", "C:\\Users\\metra"))
        / ".local"
        / "bin"
        / "feishu-notify.ps1"
    )
    notification_enabled: bool = True
    default_alert_policy: str = "auto"
    notify_cooldown_skip: bool = True
    notify_ignored_noise: bool = True
    test_alert_alertnames: tuple[str, ...] = ("AlertmanagerE2E",)
    test_alert_instance_prefixes: tuple[str, ...] = ("smoke-",)
    digest_enabled: bool = True
    digest_time: str = "21:30"
    digest_max_candidates: int = 20
    scan_enabled: bool = True
    scan_time: str = "06:00"
    scan_disk_free_gb_min: float = 30.0
    scan_cloud_free_gb_min: float = 10.0
    scan_cert_days_warn: int = 30

    prometheus_url: str = "http://127.0.0.1:19090"

    extra: dict[str, object] = field(default_factory=dict)

    @classmethod
    def load(cls, toml_path: Path | None = None) -> ControlPlaneConfig:
        base = cls()
        if toml_path is None:
            toml_path = PROJECT_ROOT / "control_plane.toml"
        values: dict[str, object] = {}
        if toml_path.is_file():
            with toml_path.open("rb") as handle:
                values = tomllib.load(handle)

        def section(name: str) -> dict[str, Any]:
            raw = values.get(name)
            return dict(raw) if isinstance(raw, dict) else {}

        server = section("server")
        agent = section("agent")
        policy = section("policy")
        projects = section("projects")
        evidence = section("evidence")
        notifications = section("notifications")
        timeouts = section("timeouts")
        candidates = section("candidates")

        allowed_projects = projects.get("allowed_auto")
        project_dirs = projects.get("project_dirs")
        repo_roots = projects.get("allowed_repo_roots")
        url_origins = projects.get("allowed_url_origins")

        api_key = os.getenv("CONTROL_PLANE_API_KEY", "")
        if not api_key:
            raise ConfigurationError(
                "CONTROL_PLANE_API_KEY is required (set it in the environment)."
            )
        gateway_base_url = str(agent.get("gateway_base_url", base.gateway_base_url))
        _validate_gateway_network(gateway_base_url)

        return cls(
            host=str(server.get("host", base.host)),
            port=int(server.get("port", base.port)),
            api_key=api_key,
            gateway_base_url=gateway_base_url,
            model=str(agent.get("model", base.model)),
            model_preflight_enabled=bool(
                agent.get("model_preflight_enabled", base.model_preflight_enabled)
            ),
            codex_cli=resolve_codex_cli(str(agent.get("codex_cli", ""))),
            codex_branch_prefix=str(
                agent.get("codex_branch_prefix", base.codex_branch_prefix)
            ),
            codex_isolate_worktree=bool(
                agent.get("isolate_worktree", base.codex_isolate_worktree)
            ),
            codex_disable_docker=bool(
                agent.get("disable_docker", base.codex_disable_docker)
            ),
            codex_disable_ssh_credentials=bool(
                agent.get(
                    "disable_ssh_credentials", base.codex_disable_ssh_credentials
                )
            ),
            codex_worktree_root=Path(
                str(agent.get("worktree_root", base.codex_worktree_root))
            ),
            candidate_branch_prefix=str(
                candidates.get(
                    "branch_prefix",
                    agent.get("codex_branch_prefix", base.candidate_branch_prefix),
                )
            ),
            candidate_retention_days=int(
                candidates.get("retention_days", base.candidate_retention_days)
            ),
            candidate_cleanup_policy=str(
                candidates.get("cleanup_policy", base.candidate_cleanup_policy)
            ),
            agent_session_dir=Path(
                str(agent.get("agent_session_dir", base.agent_session_dir))
            ),
            gateway_timeout_seconds=int(
                agent.get("gateway_timeout_seconds", base.gateway_timeout_seconds)
            ),
            max_agent_calls_per_repair=int(
                agent.get("max_agent_calls_per_repair", base.max_agent_calls_per_repair)
            ),
            cooldown_seconds=int(policy.get("cooldown_seconds", base.cooldown_seconds)),
            max_attempts=int(policy.get("max_attempts", base.max_attempts)),
            daily_agent_budget=int(policy.get("daily_agent_budget", base.daily_agent_budget)),
            per_repair_timeout_seconds=int(
                policy.get("per_repair_timeout_seconds", base.per_repair_timeout_seconds)
            ),
            exec_timeout_seconds=int(
                timeouts.get(
                    "exec_seconds",
                    policy.get(
                        "per_repair_timeout_seconds",
                        base.exec_timeout_seconds,
                    ),
                )
            ),
            comm_timeout_seconds=int(
                timeouts.get("comm_seconds", base.comm_timeout_seconds)
            ),
            verify_timeout_seconds=int(
                timeouts.get("verify_seconds", base.verify_timeout_seconds)
            ),
            approval_timeout_seconds=int(
                timeouts.get("approval_seconds", base.approval_timeout_seconds)
            ),
            git_push_timeout_seconds=int(
                timeouts.get("git_push_seconds", base.git_push_timeout_seconds)
            ),
            lease_ttl_seconds=int(
                timeouts.get("lease_ttl_seconds", base.lease_ttl_seconds)
            ),
            dirty_worktree_policy=str(
                policy.get("dirty_worktree_policy", base.dirty_worktree_policy)
            ),
            max_agent_output_bytes=int(
                agent.get("max_agent_output_bytes", base.max_agent_output_bytes)
            ),
            github_ssh_fallback=_env_bool(
                "CONTROL_PLANE_SSH_443_FALLBACK",
                bool(policy.get("github_ssh_fallback", base.github_ssh_fallback)),
            ),
            github_ssh_host_port=str(
                policy.get("github_ssh_host_port", base.github_ssh_host_port)
            ),
            alertmanager_url=str(
                policy.get("alertmanager_url", base.alertmanager_url)
            ),
            prometheus_url=str(
                policy.get("prometheus_url", base.prometheus_url)
            ),
            external_side_effects_require_approval=_env_bool(
                "CONTROL_PLANE_SIDE_EFFECT_APPROVAL",
                bool(
                    policy.get(
                        "external_side_effects_require_approval",
                        base.external_side_effects_require_approval,
                    )
                ),
            ),
            blocked_paths=tuple(blocked_paths_raw)
            if isinstance(blocked_paths_raw := policy.get("blocked_paths"), list)
            else base.blocked_paths,
            max_concurrent=int(policy.get("max_concurrent", base.max_concurrent)),
            paused=_env_bool("CONTROL_PLANE_PAUSED", bool(policy.get("paused", base.paused))),
            allowed_auto_projects=tuple(
                allowed_projects
                if isinstance(allowed_projects, list)
                else base.allowed_auto_projects
            ),
            project_dirs={
                str(k): str(v)
                for k, v in (
                    dict(project_dirs) if isinstance(project_dirs, dict) else {}
                ).items()
            }
            or base.project_dirs,
            allowed_repo_roots=tuple(
                repo_roots if isinstance(repo_roots, list) else base.allowed_repo_roots
            ),
            allowed_url_origins=tuple(
                url_origins if isinstance(url_origins, list) else base.allowed_url_origins
            ),
            docker_cleanup_min_reclaimable_gb=float(
                policy.get(
                    "docker_cleanup_min_reclaimable_gb",
                    base.docker_cleanup_min_reclaimable_gb,
                )
            ),
            candidate_trial_days=int(
                evidence.get("candidate_trial_days", base.candidate_trial_days)
            ),
            candidate_wip_limit=int(
                evidence.get("candidate_wip_limit", base.candidate_wip_limit)
            ),
            default_disposition=str(
                evidence.get("default_disposition", base.default_disposition)
            ),
            notification_enabled=_env_bool(
                "CONTROL_PLANE_NOTIFICATIONS",
                bool(notifications.get("enabled", base.notification_enabled)),
            ),
            feishu_notify_script=Path(
                str(
                    notifications.get(
                        "feishu_notify_script",
                        base.feishu_notify_script,
                    )
                )
            ),
            extra=values,
        )
