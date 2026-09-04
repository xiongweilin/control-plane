from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .audit import redact_text, truncate_bytes
from .config import ControlPlaneConfig

Sandbox = Literal["read-only", "workspace-write"]


def _git_executable() -> str:
    return shutil.which("git.exe") or shutil.which("git") or "git"


@dataclass(slots=True)
class _PreparedBoundary:
    cwd: Path
    env: Mapping[str, str]
    repository: Path | None = None
    worktree: Path | None = None
    support_dir: Path | None = None

    def cleanup(self) -> None:
        if self.worktree is not None and self.repository is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [
                        _git_executable(),
                        "-C",
                        str(self.repository),
                        "worktree",
                        "remove",
                        "--force",
                        str(self.worktree),
                    ],
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
        if self.support_dir is not None:
            shutil.rmtree(self.support_dir, ignore_errors=True)


class CodexExecutionBoundary:
    """Windows/profile-specific process isolation injected into Agent Kernel.

    Workspace-write remains isolated by default. Exact repositories configured as
    standing auto-repair projects may be edited in place after a clean-tree
    takeover. That ownership remains in memory across the two bounded repair
    attempts so the second attempt sees the first attempt's changes. Docker and
    remote Git credentials remain physically denied to Codex.
    """

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self.session_dir = Path(config.agent_session_dir)
        self._managed_auto_repos: set[Path] = set()

    @staticmethod
    def _is_git_repository(repo: Path) -> bool:
        try:
            proc = subprocess.run(
                [_git_executable(), "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and bool((proc.stdout or b"").strip())

    @staticmethod
    def _working_tree_clean(repo: Path) -> bool:
        try:
            proc = subprocess.run(
                [_git_executable(), "-C", str(repo), "status", "--porcelain"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and not (proc.stdout or b"").strip()

    def _candidate_worktree(self, repo: Path) -> tuple[Path, Path]:
        root = self.config.codex_worktree_root
        root.mkdir(parents=True, exist_ok=True)
        worktree = root / f"candidate-{uuid.uuid4().hex}"
        proc = subprocess.run(
            [
                _git_executable(),
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                "HEAD",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"unable to create isolated Codex worktree: {detail[:500]}")
        return repo, worktree

    @staticmethod
    def _deny_command(path: Path, message: str) -> str:
        if os.name == "nt":
            target = path.with_suffix(".cmd")
            target.write_text(
                f"@echo off\r\necho {message} 1>&2\r\nexit /b 126\r\n",
                encoding="ascii",
            )
            return f'"{target.as_posix()}"'
        path.write_text(f"#!/bin/sh\necho {message} >&2\nexit 126\n", encoding="utf-8")
        path.chmod(0o700)
        return shlex.quote(str(path))

    def _restricted_env(self, support_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        if self.config.codex_disable_ssh_credentials:
            for key in (
                "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_SYSTEM",
                "GIT_SSH",
                "GIT_SSH_COMMAND",
                "GIT_ASKPASS",
                "SSH_ASKPASS",
                "SSH_AUTH_SOCK",
                "SSH_AGENT_PID",
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "AZURE_DEVOPS_EXT_PAT",
            ):
                env.pop(key, None)
            deny_ssh = self._deny_command(
                support_dir / "deny-ssh", "Codex Git/SSH credentials are disabled"
            )
            deny_askpass = self._deny_command(
                support_dir / "deny-askpass", "Codex credential prompts are disabled"
            )
            git_config = support_dir / "gitconfig"
            git_config.write_text(
                f"[credential]\n\thelper =\n[core]\n\tsshCommand = {deny_ssh}\n",
                encoding="utf-8",
            )
            env.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(git_config),
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "Never",
                    "GIT_SSH": deny_ssh,
                    "GIT_SSH_COMMAND": deny_ssh,
                    "GIT_SSH_VARIANT": "ssh",
                    "GIT_ASKPASS": deny_askpass,
                    "SSH_ASKPASS": deny_askpass,
                    "SSH_AUTH_SOCK": "",
                }
            )
        if self.config.codex_disable_docker:
            docker_config = support_dir / "docker"
            docker_config.mkdir(parents=True, exist_ok=True)
            (docker_config / "config.json").write_text(
                json.dumps({"auths": {}}), encoding="utf-8"
            )
            disabled_host = (
                "npipe:////./pipe/control-plane-codex-disabled"
                if os.name == "nt"
                else "unix:///tmp/control-plane-codex-disabled.sock"
            )
            env.update(
                {
                    "DOCKER_HOST": disabled_host,
                    "DOCKER_CONTEXT": "control-plane-codex-disabled",
                    "DOCKER_CONFIG": str(docker_config),
                    "BUILDX_CONFIG": str(docker_config),
                }
            )
        env["CONTROL_PLANE_CODEX_PHYSICAL_BOUNDARY"] = "1"
        return env

    def prepare(self, repo: str, sandbox: Sandbox) -> _PreparedBoundary:
        source = Path(repo).resolve()
        support_dir = (
            self.config.codex_worktree_root.parent / f".codex-boundary-{uuid.uuid4().hex}"
        )
        support_dir.mkdir(parents=True, exist_ok=True)
        pair: tuple[Path, Path] | None = None
        try:
            if sandbox == "workspace-write" and self.config.codex_isolate_worktree:
                if not self._is_git_repository(source):
                    raise RuntimeError("workspace-write requires a confirmed Git repository")
                auto_project = self.config.auto_project_for_repo(source)
                if auto_project is not None:
                    if source not in self._managed_auto_repos:
                        if not self._working_tree_clean(source):
                            raise RuntimeError(
                                "auto-repair repo is not clean; human review required"
                            )
                        self._managed_auto_repos.add(source)
                else:
                    pair = self._candidate_worktree(source)
            if pair is None:
                return _PreparedBoundary(
                    cwd=source,
                    env=self._restricted_env(support_dir),
                    support_dir=support_dir,
                )
            return _PreparedBoundary(
                cwd=pair[1],
                env=self._restricted_env(support_dir),
                repository=pair[0],
                worktree=pair[1],
                support_dir=support_dir,
            )
        except Exception:
            if pair is not None:
                _PreparedBoundary(
                    cwd=pair[1], env={}, repository=pair[0], worktree=pair[1]
                ).cleanup()
            shutil.rmtree(support_dir, ignore_errors=True)
            raise

    def redact_transcript(self, text: str) -> str:
        stored, _ = truncate_bytes(redact_text(text), self.config.max_agent_output_bytes)
        return stored
