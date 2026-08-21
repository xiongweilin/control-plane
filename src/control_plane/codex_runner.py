from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .audit import redact_args, redact_text, truncate_bytes
from .config import ControlPlaneConfig
from .runtime import current_run_id, terminate_process_tree_async
from .storage import Store

logger = logging.getLogger(__name__)


def _git_executable() -> str:
    """Resolve Git once through PATH while keeping subprocess argv explicit."""

    return shutil.which("git.exe") or shutil.which("git") or "git"


class CodexCliUnavailableError(RuntimeError):
    """Raised when the Codex CLI cannot be located or its version probe fails."""


def repo_path_to_windows(path: str) -> str:
    """Normalize a repo path for native Windows executables (WSL retired 2026-08-07).

    On non-Windows hosts (e.g. Linux CI running the portable suite) the path is
    returned unchanged so it stays usable as a subprocess cwd.
    """
    if os.name != "nt":
        return path
    return path.replace("/", "\\")


@dataclass(slots=True)
class CodexSessionResult:
    exit_code: int
    last_message: str
    timed_out: bool = False
    stderr_tail: str = ""
    truncated: bool = False


@dataclass(slots=True)
class _CodexExecutionBoundary:
    """Ephemeral process boundary for one Codex invocation.

    ``workspace-write`` is always pointed at a detached Git worktree when the
    target is a repository.  The support directory contains only deny-by-
    default Git/SSH and Docker configuration; it is removed after the child
    exits.  The typed personal providers never reuse this environment.
    """

    cwd: Path
    env: dict[str, str]
    repository: Path | None = None
    worktree: Path | None = None
    support_dir: Path | None = None

    def cleanup(self) -> None:
        if self.worktree is not None and self.repository is not None:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(  # noqa: S603, S607 - fixed Git worktree cleanup command
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
                    shell=False,
                    check=False,
                )
        if self.support_dir is not None:
            shutil.rmtree(self.support_dir, ignore_errors=True)


class CodexRunner:
    """Runs a Codex headless agent session with an explicit sandbox ceiling.

    The personal profile only permits the two least-privilege Codex sandboxes.
    Remote/deployment effects must be handled by a separate provider and
    RealityBoundary, never by silently upgrading this runner to a full-access
    sandbox.

    The control plane is the authoritative boundary: it injects hard constraints,
    verifies outcomes independently, gates code merges behind approval, and rolls
    back on failure. The Codex CLI therefore runs non-interactively inside that
    boundary with the capability-derived least-privilege sandbox.
    """

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self.store: Store | None = None

    def attach_store(self, store: Store) -> None:
        """Attach the persistence store for audit records (set at app startup)."""
        self.store = store

    @staticmethod
    def _argv_for(cli: Path, args: list[str]) -> list[str]:
        """Build the subprocess argv for the resolved CLI entry."""
        return [str(cli), *args]

    @staticmethod
    def _is_git_repository(repo: Path) -> bool:
        try:
            proc = subprocess.run(  # noqa: S603, S607 - fixed Git repository probe
                [_git_executable(),
                 "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True,
                timeout=30,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and bool((proc.stdout or b"").strip())

    def _create_candidate_worktree(self, repo: Path) -> tuple[Path, Path] | None:
        """Create a detached worktree without touching the user's checkout.

        The control plane keeps the branch ref in the source repository so the
        approval workflow can inspect it after the ephemeral worktree is
        removed.  A non-Git compatibility target is returned unchanged; real
        ``code.edit`` repair targets are always Git repositories selected by
        ``RepairService``.
        """

        if not self._is_git_repository(repo):
            return None
        root = self.config.codex_worktree_root
        root.mkdir(parents=True, exist_ok=True)
        worktree = root / f"candidate-{uuid.uuid4().hex}"
        try:
            proc = subprocess.run(  # noqa: S603, S607 - fixed Git worktree creation command
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
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"unable to create Codex candidate worktree: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                f"unable to create Codex candidate worktree for {repo}: {detail[:500]}"
            )
        return repo, worktree

    @staticmethod
    def _write_deny_command(path: Path, message: str) -> str:
        """Write a platform-native non-interactive command that always denies."""

        if os.name == "nt":
            path = path.with_suffix(".cmd")
            path.write_text(
                f"@echo off\r\necho {message} 1>&2\r\nexit /b 126\r\n",
                encoding="ascii",
            )
            # Git config treats backslashes as escape characters.  Use forward
            # slashes in the temporary Windows command path so the config is
            # valid even when the process runs from a user profile directory.
            return f'"{path.as_posix()}"'
        path.write_text(f"#!/bin/sh\necho {message} >&2\nexit 126\n", encoding="utf-8")
        path.chmod(0o700)
        return shlex.quote(str(path))

    def _restricted_environment(self, support_dir: Path) -> dict[str, str]:
        """Build a child environment with host control-plane credentials removed."""

        env = dict(os.environ)
        if self.config.codex_disable_ssh_credentials:
            # Do not inherit user/global Git configuration, credential manager,
            # SSH agent or token-bearing GitHub variables.  The Codex session
            # still retains its own model configuration; only Git/SSH auth is
            # cut off here.
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
            deny_ssh = self._write_deny_command(
                support_dir / "deny-ssh",
                "Codex Git/SSH credentials are disabled by control-plane",
            )
            deny_askpass = self._write_deny_command(
                support_dir / "deny-askpass",
                "Codex Git credential prompts are disabled by control-plane",
            )
            git_config = support_dir / "gitconfig"
            git_config.write_text(
                "[credential]\n\thelper =\n"
                f"[core]\n\tsshCommand = {deny_ssh}\n",
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
                json.dumps({"auths": {}}, ensure_ascii=False), encoding="utf-8"
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
                    "COMPOSE_DOCKER_CLI_BUILD": "0",
                    "BUILDX_CONFIG": str(docker_config),
                }
            )
        env["CONTROL_PLANE_CODEX_PHYSICAL_BOUNDARY"] = "1"
        return env

    def _prepare_execution_boundary(
        self,
        repo: str,
        sandbox: Literal["read-only", "workspace-write"],
    ) -> _CodexExecutionBoundary:
        """Prepare an isolated cwd and restricted child environment."""

        source = Path(repo).resolve()
        support_dir = Path(self.config.codex_worktree_root).parent / (
            f".codex-boundary-{uuid.uuid4().hex}"
        )
        support_dir.mkdir(parents=True, exist_ok=True)
        worktree_pair: tuple[Path, Path] | None = None
        try:
            if sandbox == "workspace-write" and self.config.codex_isolate_worktree:
                if not self._is_git_repository(source):
                    raise RuntimeError(
                        "workspace-write requires a confirmed Git repository so the "
                        "Codex session cannot fall back to the source directory"
                    )
                worktree_pair = self._create_candidate_worktree(source)
                if worktree_pair is None:
                    raise RuntimeError(
                        "workspace-write isolation could not establish a detached "
                        "candidate worktree"
                    )
            if worktree_pair is None:
                return _CodexExecutionBoundary(
                    cwd=source,
                    env=self._restricted_environment(support_dir),
                    support_dir=support_dir,
                )
            repository, worktree = worktree_pair
            return _CodexExecutionBoundary(
                cwd=worktree,
                env=self._restricted_environment(support_dir),
                repository=repository,
                worktree=worktree,
                support_dir=support_dir,
            )
        except Exception:
            if worktree_pair is not None:
                _CodexExecutionBoundary(
                    cwd=worktree_pair[1],
                    env={},
                    repository=worktree_pair[0],
                    worktree=worktree_pair[1],
                ).cleanup()
            shutil.rmtree(support_dir, ignore_errors=True)
            raise

    def cli_info(self) -> tuple[Path, str]:
        """Probe the resolved CLI and return ``(path, version)``.

        Raises :class:`CodexCliUnavailableError` with a clear diagnostic when the
        CLI is missing, not executable, or the version probe fails.
        """
        path = self.config.codex_cli
        try:
            proc = subprocess.run(  # noqa: S603 - fixed command line; path resolved by config
                self._argv_for(path, ["--version"]),
                capture_output=True,
                timeout=15,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexCliUnavailableError(
                f"codex CLI not runnable at {path}: {exc}"
            ) from exc
        output = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not output:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise CodexCliUnavailableError(
                f"codex CLI version probe failed (exit {proc.returncode}) at {path}: "
                f"{stderr[:300] or 'no output'}"
            )
        return path, output.splitlines()[0].strip()

    def check_cli(self) -> tuple[Path, str, bool]:
        """Probe the CLI and detect version drift since the last probe.

        Returns ``(path, version, changed)``. The last seen version is persisted
        (``codex:cli_version`` / ``codex:cli_path`` settings) so version upgrades
        are visible; a change is logged at warning level.
        """
        path, version = self.cli_info()
        changed = False
        if self.store is not None:
            previous = self.store.get_setting("codex:cli_version", "")
            if previous and previous != version:
                changed = True
                logger.warning(
                    "codex CLI version changed from %s to %s (path %s)",
                    previous,
                    version,
                    path,
                )
            self.store.set_setting("codex:cli_version", version)
            self.store.set_setting("codex:cli_path", str(path))
        return path, version, changed

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
        sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
    ) -> CodexSessionResult:
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValueError(f"unsupported Codex sandbox: {sandbox!r}")
        self.cli_info()  # fail fast with a clear error instead of a raw spawn error
        session_dir = self.config.agent_session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / f"{repair_id}.jsonl"
        boundary = self._prepare_execution_boundary(repo, sandbox)
        windows_repo = repo_path_to_windows(str(boundary.cwd))
        started_at = time.monotonic()
        args = self._argv_for(
            self.config.codex_cli,
            [
                "exec",
                "--model",
                self.config.model,
                "--sandbox",
                sandbox,
                "--skip-git-repo-check",
                "--json",
                prompt,
            ],
        )
        run_id = run_id or current_run_id()
        timeout_seconds = (
            self.config.exec_timeout_seconds
            if self.config.exec_timeout_seconds
            else self.config.per_repair_timeout_seconds
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=windows_repo,
                env=boundary.env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_seconds,
                )
            except asyncio.CancelledError:
                if proc.returncode is None:
                    await terminate_process_tree_async(proc.pid)
                logger.warning("codex session cancelled for %s", repair_id)
                self._audit(run_id, repair_id, args, None, started_at, truncated=False)
                raise
            except TimeoutError:
                await terminate_process_tree_async(proc.pid)
                logger.warning("codex session timed out for %s", repair_id)
                self._audit(run_id, repair_id, args, 124, started_at, truncated=False)
                return CodexSessionResult(
                    exit_code=124,
                    last_message="",
                    timed_out=True,
                    stderr_tail="codex session timed out",
                )
            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            header = (
                f'{{"type": "control_plane_meta", "run_id": "{run_id}", '
                f'"repair_id": "{repair_id}", "started_at": {int(time.time())}}}\n'
            )
            stored, truncated = truncate_bytes(
                redact_text(stdout_text), max(4_096, self.config.max_agent_output_bytes)
            )
            jsonl_path.write_text(header + stored, encoding="utf-8")
            last_message = stdout_text[:20_000]
            if proc.returncode != 0:
                logger.warning(
                    "codex session failed for %s: exit %s",
                    repair_id,
                    proc.returncode,
                )
            self._audit(
                run_id,
                repair_id,
                args,
                proc.returncode,
                started_at,
                truncated=truncated,
            )
            return CodexSessionResult(
                exit_code=proc.returncode or 0,
                last_message=last_message,
                stderr_tail=stderr_text[-2_000:],
                truncated=truncated,
            )
        finally:
            boundary.cleanup()

    def _audit(
        self,
        run_id: str,
        repair_id: str,
        args: list[str],
        exit_code: int | None,
        started_at: float,
        *,
        truncated: bool,
    ) -> None:
        """Best-effort audit record; never raises (auditing must not break repair)."""
        if self.store is None:
            return
        import json

        try:
            self.store.add_audit_entry(
                f"aud-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                repair_id=repair_id,
                kind="agent",
                argv_json=json.dumps(redact_args(args), ensure_ascii=False),
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                truncated=truncated,
                error_class="",
            )
        except Exception:  # pragma: no cover - audit must never break the repair
            logger.debug("audit write failed for %s", repair_id)
            try:
                from .metrics import CONTROLLED_IGNORES

                CONTROLLED_IGNORES.labels(site="audit_write").inc()
            except Exception:  # pragma: no cover - metrics must never break control flow
                logger.debug("audit_write counter failed")
