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

    model: str = "opencode-go/deepseek-v4-flash"
    codex_cli: Path = field(default_factory=_resolve_codex_cli)
    gateway_base_url: str = "http://127.0.0.1:4101/v1"
    codex_isolate_worktree: bool = True
    codex_disable_docker: bool = True
    codex_disable_ssh_credentials: bool = True
    codex_worktree_root: Path = PROJECT_ROOT.parent / ".control-plane-codex-worktrees"
    max_agent_output_bytes: int = 200_000

    prometheus_url: str = "http://127.0.0.1:19090"
    alertmanager_url: str = ""
    notification_enabled: bool = True

    game_mode_enabled: bool = True
    game_mode_state_path: Path = Path(r"D:\agent\cs2-game-mode\state.json")
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
    )
    project_dirs: dict[str, str] = field(
        default_factory=lambda: {
            "docker": r"D:\infrastructure\compose\dify",
            "dify": r"D:\infrastructure\compose\dify",
            "observability": r"D:\infrastructure\compose\observability",
            "feishu-dify-gateway": r"D:\infrastructure\compose\feishu-dify-gateway",
        }
    )
    allowed_repo_roots: tuple[str, ...] = (
        r"D:\infrastructure\compose",
        r"D:\agent",
    )

    def repo_allowed(self, repo: str | Path) -> bool:
        candidate = os.path.normcase(os.path.abspath(str(repo)))
        for root in self.allowed_repo_roots:
            normalized_root = os.path.normcase(os.path.abspath(root))
            try:
                if os.path.commonpath([candidate, normalized_root]) == normalized_root:
                    return True
            except ValueError:
                continue
        return False

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
        monitoring = _section(data, "monitoring")
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

        return cls(
            host=str(server.get("host", base.host)),
            port=int(server.get("port", base.port)),
            api_key=api_key,
            owner_principal=str(os.getenv("CONTROL_PLANE_OWNER_PRINCIPAL", kernel.get("owner_principal", base.owner_principal))),
            state_db=Path(str(kernel.get("state_db", base.state_db))),
            artifact_root=Path(str(kernel.get("artifact_root", base.artifact_root))),
            agent_session_dir=Path(str(model.get("session_dir", base.agent_session_dir))),
            model=str(model.get("name", base.model)),
            codex_cli=_resolve_codex_cli(str(model.get("codex_cli", ""))),
            gateway_base_url=str(model.get("gateway_base_url", base.gateway_base_url)),
            codex_isolate_worktree=bool(model.get("isolate_worktree", base.codex_isolate_worktree)),
            codex_disable_docker=bool(model.get("disable_docker", base.codex_disable_docker)),
            codex_disable_ssh_credentials=bool(model.get("disable_ssh_credentials", base.codex_disable_ssh_credentials)),
            codex_worktree_root=Path(str(model.get("worktree_root", base.codex_worktree_root))),
            max_agent_output_bytes=int(model.get("max_output_bytes", base.max_agent_output_bytes)),
            prometheus_url=str(monitoring.get("prometheus_url", base.prometheus_url)),
            alertmanager_url=str(monitoring.get("alertmanager_url", base.alertmanager_url)),
            notification_enabled=_env_bool("CONTROL_PLANE_NOTIFICATIONS", bool(monitoring.get("notification_enabled", base.notification_enabled))),
            game_mode_enabled=_env_bool("CONTROL_PLANE_GAME_MODE", bool(game_mode.get("enabled", base.game_mode_enabled))),
            game_mode_state_path=Path(str(game_mode.get("state_path", base.game_mode_state_path))),
            game_mode_active_max_age_seconds=int(game_mode.get("active_max_age_seconds", base.game_mode_active_max_age_seconds)),
            game_mode_restore_grace_seconds=int(game_mode.get("restore_grace_seconds", base.game_mode_restore_grace_seconds)),
            game_mode_alertnames=tuple(str(v) for v in game_mode.get("alertnames", base.game_mode_alertnames)),
            game_mode_scrape_jobs=tuple(str(v) for v in game_mode.get("scrape_jobs", base.game_mode_scrape_jobs)),
            allowed_auto_projects=tuple(str(v) for v in projects.get("allowed_auto", base.allowed_auto_projects)),
            project_dirs=project_dirs,
            allowed_repo_roots=tuple(str(v) for v in projects.get("allowed_repo_roots", base.allowed_repo_roots)),
        )
