from __future__ import annotations

import asyncio
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .audit import redact_args, redact_text, truncate_bytes
from .config import ControlPlaneConfig
from .runtime import current_run_id, terminate_process_tree_async
from .storage import Store

logger = logging.getLogger(__name__)


class CodexCliUnavailableError(RuntimeError):
    """Raised when the Codex CLI cannot be located or its version probe fails."""


def repo_path_to_windows(path: str) -> str:
    """Normalize a repo path for native Windows executables (WSL retired 2026-08-07)."""
    return path.replace("/", "\\")


@dataclass(slots=True)
class CodexSessionResult:
    exit_code: int
    last_message: str
    timed_out: bool = False
    stderr_tail: str = ""
    truncated: bool = False


class CodexRunner:
    """Runs a full Codex agent session with the configured model.

    The control plane is the authoritative boundary: it injects hard constraints,
    verifies outcomes independently, gates code merges behind approval, and rolls
    back on failure. The Codex session therefore runs with bypass flags so repairs
    can execute non-interactively inside that boundary.
    """

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self.store: Store | None = None

    def attach_store(self, store: Store) -> None:
        """Attach the persistence store for audit records (set at app startup)."""
        self.store = store

    def cli_info(self) -> tuple[Path, str]:
        """Probe the resolved CLI and return ``(path, version)``.

        Raises :class:`CodexCliUnavailableError` with a clear diagnostic when the
        CLI is missing, not executable, or the version probe fails. This is the
        fail-fast guard for batch5 item 1 (no hardcoded npm-internal paths).
        """
        path = self.config.codex_cli
        try:
            proc = subprocess.run(  # noqa: S603 - fixed command line; path resolved by config
                [str(path), "--version"],
                capture_output=True,
                timeout=15,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexCliUnavailableError(
                f"Codex CLI not runnable at {path}: {exc}"
            ) from exc
        output = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not output:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise CodexCliUnavailableError(
                f"Codex CLI version probe failed (exit {proc.returncode}) at {path}: "
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
    ) -> CodexSessionResult:
        self.cli_info()  # fail fast with a clear error instead of a raw spawn error
        session_dir = self.config.agent_session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / f"{repair_id}.jsonl"
        last_path = session_dir / f"{repair_id}-last.md"
        windows_repo = repo_path_to_windows(repo)
        started_at = time.monotonic()
        args = [
            str(self.config.codex_cli),
            "exec",
            "-m",
            self.config.model,
            "-C",
            windows_repo,
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--json",
            "-o",
            str(last_path),
            "-",
        ]
        run_id = run_id or current_run_id()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timeout_seconds = (
            self.config.exec_timeout_seconds
            if self.config.exec_timeout_seconds
            else self.config.per_repair_timeout_seconds
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
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
        last_message = ""
        if last_path.is_file():
            last_message = last_path.read_text(encoding="utf-8", errors="replace")[:20_000]
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
