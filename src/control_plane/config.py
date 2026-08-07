from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigurationError(RuntimeError):
    pass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    host: str = "127.0.0.1"
    port: int = 18083
    api_key: str = ""

    opencodex_base_url: str = "http://127.0.0.1:10100/v1"
    model: str = "opencode-go/deepseek-v4-flash"
    codex_cli: Path = (
        Path(os.getenv("USERPROFILE", "C:\\Users\\metra"))
        / "AppData"
        / "Roaming"
        / "npm"
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    codex_branch_prefix: str = "fix/control-plane-"
    agent_session_dir: Path = PROJECT_ROOT / "data" / "agent-sessions"
    opencodex_timeout_seconds: int = 120
    max_agent_calls_per_repair: int = 8

    cooldown_seconds: int = 600
    max_attempts: int = 2
    daily_agent_budget: int = 20
    per_repair_timeout_seconds: int = 900
    max_concurrent: int = 2
    paused: bool = False

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
        "D:\\download\\agent",
        "D:\\download\\ratio",
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
    notify_heartbeat_seconds: int = 120
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

        def section(name: str) -> dict[str, object]:
            raw = values.get(name)
            return dict(raw) if isinstance(raw, dict) else {}

        server = section("server")
        agent = section("agent")
        policy = section("policy")
        projects = section("projects")
        evidence = section("evidence")
        notifications = section("notifications")

        allowed_projects = projects.get("allowed_auto")
        project_dirs = projects.get("project_dirs")
        repo_roots = projects.get("allowed_repo_roots")
        url_origins = projects.get("allowed_url_origins")

        api_key = os.getenv("CONTROL_PLANE_API_KEY", "")
        if not api_key:
            raise ConfigurationError(
                "CONTROL_PLANE_API_KEY is required (set it in the environment)."
            )

        return cls(
            host=str(server.get("host", base.host)),
            port=int(server.get("port", base.port)),
            api_key=api_key,
            opencodex_base_url=str(agent.get("opencodex_base_url", base.opencodex_base_url)),
            model=str(agent.get("model", base.model)),
            codex_cli=Path(str(agent.get("codex_cli", base.codex_cli))),
            codex_branch_prefix=str(
                agent.get("codex_branch_prefix", base.codex_branch_prefix)
            ),
            agent_session_dir=Path(
                str(agent.get("agent_session_dir", base.agent_session_dir))
            ),
            opencodex_timeout_seconds=int(
                agent.get("opencodex_timeout_seconds", base.opencodex_timeout_seconds)
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
