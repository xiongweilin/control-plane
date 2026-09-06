from __future__ import annotations

import os
import shutil
import tomllib
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


def _resolve_codex_cli(explicit: str = "") -> Path:
    if explicit.strip():
        return Path(explicit)
    found = shutil.which("codex.cmd") or shutil.which("codex")
    return Path(found) if found else Path("codex")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return dict(value) if isinstance(value, dict) else {}


def _normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    """Only profile-specific configuration.

    Durable execution, cognitive control, responsibility, records, authority,
    recovery, and verification are owned by the installed Agent Kernel.
    """

    host: str = "127.0.0.1"
    port: int = 18083
    api_key: str = ""
    owner_principal: str = "human:owner"

    state_db: Path = PROJECT_ROOT / "data" / "agent-kernel.db"
    artifact_root: Path = PROJECT_ROOT / "data" / "artifacts"
    agent_session_dir: Path = PROJECT_ROOT / "data" / "agent-sessions"

    diagnosis_model: str = "gpt-5.6-luna"
    execution_model: str = "gpt-5.6-luna"
    codex_cli: Path = field(default_factory=_resolve_codex_cli)
    gateway_base_url: str = "http://127.0.0.1:4101/v1"
    codex_isolate_worktree: bool = True
    codex_disable_docker: bool = True
    codex_disable_ssh_credentials: bool = True
    codex_worktree_root: Path = PROJECT_ROOT.parent / ".control-plane-codex-worktrees"
    max_agent_output_bytes: int = 200_000
    # A Codex invocation is a complete agent session, not a single HTTP
    # round-trip. Keep enough time for multi-turn reasoning before treating
    # the provider as unavailable.
    gateway_timeout_seconds: float = 900.0
    diagnosis_timeout_seconds: float = 900.0
    execution_timeout_seconds: float = 900.0

    prometheus_url: str = "http://127.0.0.1:19090"
    alertmanager_url: str = ""
    notification_enabled: bool = True
    cooldown_seconds: int = 600
    max_concurrent: int = 2

    environment_enabled: bool = True
    environment_cache_seconds: int = 60
    environment_probe_timeout_seconds: int = 30
    docker_build_cache_max_bytes: int = 5 * 1024**3
    docker_expected_exited_containers: tuple[str, ...] = ("dify-init_permissions-1",)
    recovery_paths: tuple[str, ...] = (
        r"D:\agent\ratio",
        r"D:\agent\docker备份",
    )
    synchronization_paths: tuple[str, ...] = (
        r"D:\agent\ratio",
        r"D:\agent\control-plane",
        r"D:\infrastructure\compose\dify",
        r"D:\infrastructure\compose\observability",
        r"D:\infrastructure\compose\commerce-orchestrator",
        r"D:\infrastructure\compose\feishu-dify-gateway",
        r"C:\Users\metra\.local\share\chezmoi",
    )
    chezmoi_source_dir: str = r"C:\Users\metra\.local\share\chezmoi"
    known_garbage_paths: tuple[str, ...] = (
        r"D:\agent\portable-runtime-worktrees",
    )
    garbage_quarantine_dir: str = r"D:\agent\_recovery-quarantine"
    automatic_handling_enabled: bool = False
    auto_maintenance_alertnames: tuple[str, ...] = (
        "ControlPlaneGarbageDetected",
    )
    v2rayn_expected_path: str | None = r"D:\agent\v2rayN-windows-64\v2rayN.exe"

    game_mode_enabled: bool = True
    game_mode_state_path: Path | None = None
    game_mode_active_max_age_seconds: int = 12 * 60 * 60
    game_mode_restore_grace_seconds: int = 10 * 60
    game_mode_alertnames: tuple[str, ...] = (
        "ContainerRestartStorm",
        "PrometheusScrapeFailed",
        "ControlPlaneStaleReady",
    )
    game_mode_scrape_jobs: tuple[str, ...] = (
        "node",
        "cadvisor",
        "loki",
        "prometheus",
        "alertmanager",
        "blackbox",
        "blackbox-protected",
        "feedback-analysis",
        "feishu-dify-gateway",
        "control-plane-ready",
    )

    allowed_auto_projects: tuple[str, ...] = (
        "dify",
        "docker",
        "observability",
        "feishu-dify-gateway",
        "commerce-orchestrator",
        "control-plane",
        "ratio",
        "chezmoi",
    )
    project_dirs: dict[str, str] = field(
        default_factory=lambda: {
            "docker": r"D:\infrastructure\compose\dify",
            "dify": r"D:\infrastructure\compose\dify",
            "observability": r"D:\infrastructure\compose\observability",
            "feishu-dify-gateway": r"D:\infrastructure\compose\feishu-dify-gateway",
            "commerce-orchestrator": r"D:\infrastructure\compose\commerce-orchestrator",
            "control-plane": r"D:\agent\control-plane",
            "ratio": r"D:\agent\ratio",
            "chezmoi": r"C:\Users\metra\.local\share\chezmoi",
        }
    )
    allowed_repo_roots: tuple[str, ...] = (
        r"D:\infrastructure\compose",
        r"D:\agent",
        r"C:\Users\metra\.local\share\chezmoi",
    )

    @property
    def model(self) -> str:
        """Compatibility alias for the provider default model."""
        return self.diagnosis_model

    def repo_allowed(self, repo: str | Path) -> bool:
        candidate = _normalized(repo)
        for root in self.allowed_repo_roots:
            normalized_root = _normalized(root)
            try:
                if os.path.commonpath([candidate, normalized_root]) == normalized_root:
                    return True
            except ValueError:
                continue
        return False

    def auto_project_for_repo(self, repo: str | Path) -> str | None:
        """Return the standing auto-repair project for an exact configured repo."""
        candidate = _normalized(repo)
        for project in self.allowed_auto_projects:
            project_dir = self.project_dirs.get(project)
            if project_dir and candidate == _normalized(project_dir):
                return project
        return None

    @classmethod
    def load(cls, path: Path | None = None) -> ControlPlaneConfig:
        base = cls()
        config_path = path or PROJECT_ROOT / "control_plane.toml"
        data: dict[str, Any] = {}
        if config_path.is_file():
            with config_path.open("rb") as handle:
                data = tomllib.load(handle)

        server = _section(data, "server")
        kernel = _section(data, "kernel")
        model = _section(data, "model")
        agent = _section(data, "agent")
        monitoring = _section(data, "monitoring")
        policy = _section(data, "policy")
        environment = _section(data, "environment")
        game_mode = _section(data, "game_mode")
        projects = _section(data, "projects")

        api_key = os.getenv("CONTROL_PLANE_API_KEY", "").strip()
        if not api_key:
            raise ConfigurationError("CONTROL_PLANE_API_KEY is required")

        raw_project_dirs = projects.get("project_dirs", base.project_dirs)
        project_dirs = (
            {str(k): str(v) for k, v in raw_project_dirs.items()}
            if isinstance(raw_project_dirs, dict)
            else dict(base.project_dirs)
        )
        legacy_model = str(model.get("name", "")).strip()

        gateway_timeout_seconds = float(
            agent.get("gateway_timeout_seconds", base.gateway_timeout_seconds)
        )
        diagnosis_timeout_seconds = float(
            agent.get("diagnosis_timeout_seconds", gateway_timeout_seconds)
        )
        execution_timeout_seconds = float(
            agent.get("execution_timeout_seconds", gateway_timeout_seconds)
        )

        return cls(
            host=str(server.get("host", base.host)),
            port=int(server.get("port", base.port)),
            api_key=api_key,
            owner_principal=str(
                os.getenv(
                    "CONTROL_PLANE_OWNER_PRINCIPAL",
                    kernel.get("owner_principal", base.owner_principal),
                )
            ),
            state_db=Path(str(kernel.get("state_db", base.state_db))),
            artifact_root=Path(str(kernel.get("artifact_root", base.artifact_root))),
            agent_session_dir=Path(str(model.get("session_dir", base.agent_session_dir))),
            diagnosis_model=str(
                model.get("diagnosis_model", legacy_model or base.diagnosis_model)
            ),
            execution_model=str(model.get("execution_model", base.execution_model)),
            codex_cli=_resolve_codex_cli(str(model.get("codex_cli", ""))),
            gateway_base_url=str(model.get("gateway_base_url", base.gateway_base_url)),
            codex_isolate_worktree=bool(
                model.get("isolate_worktree", base.codex_isolate_worktree)
            ),
            codex_disable_docker=bool(model.get("disable_docker", base.codex_disable_docker)),
            codex_disable_ssh_credentials=bool(
                model.get("disable_ssh_credentials", base.codex_disable_ssh_credentials)
            ),
            codex_worktree_root=Path(str(model.get("worktree_root", base.codex_worktree_root))),
            max_agent_output_bytes=int(model.get("max_output_bytes", base.max_agent_output_bytes)),
            gateway_timeout_seconds=gateway_timeout_seconds,
            diagnosis_timeout_seconds=diagnosis_timeout_seconds,
            execution_timeout_seconds=execution_timeout_seconds,
            prometheus_url=str(
                monitoring.get("prometheus_url", policy.get("prometheus_url", base.prometheus_url))
            ),
            alertmanager_url=str(
                monitoring.get(
                    "alertmanager_url", policy.get("alertmanager_url", base.alertmanager_url)
                )
            ),
            notification_enabled=_env_bool(
                "CONTROL_PLANE_NOTIFICATIONS",
                bool(
                    monitoring.get(
                        "notification_enabled",
                        _section(data, "notifications").get(
                            "enabled", base.notification_enabled
                        ),
                    )
                ),
            ),
            cooldown_seconds=int(policy.get("cooldown_seconds", base.cooldown_seconds)),
            max_concurrent=max(1, int(policy.get("max_concurrent", base.max_concurrent))),
            environment_enabled=_env_bool(
                "CONTROL_PLANE_ENVIRONMENT_CHECKS",
                bool(environment.get("enabled", base.environment_enabled)),
            ),
            environment_cache_seconds=int(
                environment.get("cache_seconds", base.environment_cache_seconds)
            ),
            environment_probe_timeout_seconds=int(
                environment.get(
                    "probe_timeout_seconds", base.environment_probe_timeout_seconds
                )
            ),
            docker_build_cache_max_bytes=int(
                environment.get(
                    "docker_build_cache_max_bytes", base.docker_build_cache_max_bytes
                )
            ),
            docker_expected_exited_containers=tuple(
                str(v)
                for v in environment.get(
                    "docker_expected_exited_containers", base.docker_expected_exited_containers
                )
            ),
            recovery_paths=tuple(
                str(v) for v in environment.get("recovery_paths", base.recovery_paths)
            ),
            synchronization_paths=tuple(
                str(v)
                for v in environment.get(
                    "synchronization_paths", base.synchronization_paths
                )
            ),
            chezmoi_source_dir=str(
                environment.get("chezmoi_source_dir", base.chezmoi_source_dir)
            ),
            known_garbage_paths=tuple(
                str(v)
                for v in environment.get("known_garbage_paths", base.known_garbage_paths)
            ),
            garbage_quarantine_dir=str(
                environment.get("garbage_quarantine_dir", base.garbage_quarantine_dir)
            ),
            automatic_handling_enabled=_env_bool(
                "CONTROL_PLANE_AUTOMATIC_HANDLING",
                bool(
                    environment.get(
                        "automatic_handling_enabled", base.automatic_handling_enabled
                    )
                ),
            ),
            auto_maintenance_alertnames=tuple(
                str(v)
                for v in environment.get(
                    "auto_maintenance_alertnames", base.auto_maintenance_alertnames
                )
            ),
            v2rayn_expected_path=(
                str(environment["v2rayn_expected_path"])
                if environment.get("v2rayn_expected_path")
                else base.v2rayn_expected_path
            ),
            game_mode_enabled=_env_bool(
                "CONTROL_PLANE_GAME_MODE",
                bool(game_mode.get("enabled", base.game_mode_enabled)),
            ),
            game_mode_state_path=(
                Path(str(game_mode["state_path"]))
                if game_mode.get("state_path")
                else base.game_mode_state_path
            ),
            game_mode_active_max_age_seconds=int(
                game_mode.get("active_max_age_seconds", base.game_mode_active_max_age_seconds)
            ),
            game_mode_restore_grace_seconds=int(
                game_mode.get("restore_grace_seconds", base.game_mode_restore_grace_seconds)
            ),
            game_mode_alertnames=tuple(
                str(v) for v in game_mode.get("alertnames", base.game_mode_alertnames)
            ),
            game_mode_scrape_jobs=tuple(
                str(v) for v in game_mode.get("scrape_jobs", base.game_mode_scrape_jobs)
            ),
            allowed_auto_projects=tuple(
                str(v) for v in projects.get("allowed_auto", base.allowed_auto_projects)
            ),
            project_dirs=project_dirs,
            allowed_repo_roots=tuple(
                str(v) for v in projects.get("allowed_repo_roots", base.allowed_repo_roots)
            ),
        )
