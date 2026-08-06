from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from .config import ControlPlaneConfig

logger = logging.getLogger(__name__)


def wsl_path_to_windows(path: str, wsl_distro: str = "Ubuntu-22.04") -> str:
    """Convert a WSL path to a Windows-accessible path for native executables."""
    drive = re.match(r"^/mnt/([a-zA-Z])/(.*)$", path)
    if drive:
        return f"{drive.group(1).upper()}:\\{drive.group(2).replace('/', '\\')}"
    if path.startswith("/"):
        return f"\\\\wsl.localhost\\{wsl_distro}\\{path.lstrip('/').replace('/', '\\')}"
    return path


@dataclass(slots=True)
class CodexSessionResult:
    exit_code: int
    last_message: str
    timed_out: bool = False
    stderr_tail: str = ""


class CodexRunner:
    """Runs a full Codex agent session with the configured model.

    The control plane is the authoritative boundary: it injects hard constraints,
    verifies outcomes independently, gates code merges behind approval, and rolls
    back on failure. The Codex session therefore runs with bypass flags so repairs
    can execute non-interactively inside that boundary.
    """

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
    ) -> CodexSessionResult:
        session_dir = self.config.agent_session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / f"{repair_id}.jsonl"
        last_path = session_dir / f"{repair_id}-last.md"
        windows_repo = wsl_path_to_windows(repo, self.config.wsl_distro)
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
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=self.config.per_repair_timeout_seconds,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("codex session timed out for %s", repair_id)
            return CodexSessionResult(
                exit_code=124,
                last_message="",
                timed_out=True,
                stderr_tail="codex session timed out",
            )
        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        jsonl_path.write_text(stdout_text, encoding="utf-8")
        last_message = ""
        if last_path.is_file():
            last_message = last_path.read_text(encoding="utf-8", errors="replace")[:20_000]
        if proc.returncode != 0:
            logger.warning(
                "codex session failed for %s: exit %s",
                repair_id,
                proc.returncode,
            )
        return CodexSessionResult(
            exit_code=proc.returncode or 0,
            last_message=last_message,
            stderr_tail=stderr_text[-2_000:],
        )
