"""Private deployment adapter for the provider-neutral Codex boundary.

The vendored provider receives this object through dependency injection.  This
module is the only place that knows about the personal Windows worktree,
credential, and Docker restrictions implemented by :mod:`codex_runner`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .audit import redact_text, truncate_bytes
from .codex_runner import CodexRunner, _CodexExecutionBoundary
from .config import ControlPlaneConfig


class CodexExecutionBoundaryAdapter:
    """Adapt the private runner boundary to the portable provider protocol."""

    def __init__(self, config: ControlPlaneConfig) -> None:
        self._runner = CodexRunner(config)
        self.session_dir = Path(config.agent_session_dir)

    def prepare(
        self,
        repo: str,
        sandbox: Literal["read-only", "workspace-write"],
    ) -> _CodexExecutionBoundary:
        return self._runner._prepare_execution_boundary(repo, sandbox)

    @staticmethod
    def redact_transcript(text: str) -> str:
        stored, _ = truncate_bytes(redact_text(text), 200_000)
        return stored

