"""Explicit private physical boundary for Git workspace maintenance.

The portable runtime owns semantic authority and the typed personal providers
own business-visible Git effects.  A few operations are execution scaffolding
needed to keep an Agent session isolated (worktree creation/removal, branch
restoration, and stale candidate cleanup).  They still mutate the local Git
state, so they live behind this small, fail-closed boundary rather than being
scattered through orchestration code.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import ControlPlaneConfig
from .tools import CommandExecutor, ToolError, resolve_repo


class GitPhysicalBoundary:
    """Guard local Git mutations that are not portable business effects."""

    def __init__(self, config: ControlPlaneConfig, executor: CommandExecutor) -> None:
        self.config = config
        self.executor = executor

    def canonical_repo(self, repo: str) -> str:
        roots = tuple(
            dict.fromkeys(
                (*self.config.allowed_repo_roots, *self.config.project_dirs.values())
            )
        )
        return resolve_repo(
            repo,
            roots,
            blocked=self.config.blocked_paths,
        )

    async def assert_clean(self, repo: str) -> None:
        """Require a known-clean worktree; unknown is never treated as clean."""

        canonical = self.canonical_repo(repo)
        output = await self.executor.run(
            ["git", "-C", canonical, "status", "--porcelain"],
            timeout=30,
        )
        if output.strip():
            raise ToolError(f"workspace is dirty: {canonical}: {output.strip()[:500]}")

    async def create_isolated_worktree(
        self,
        repo: str,
        worktree_dir: Path,
        base_ref: str = "main",
    ) -> tuple[str, Path]:
        canonical = self.canonical_repo(repo)
        target = worktree_dir.resolve(strict=False)
        source = Path(canonical).resolve(strict=False)
        if target == source or source in target.parents:
            raise ToolError("isolated worktree must not be inside the source repository")
        if target.exists():
            raise ToolError(f"isolated worktree target already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        await self.executor.run(
            ["git", "-C", canonical, "worktree", "add", "--detach", str(target), base_ref],
            timeout=120,
        )
        return canonical, target

    async def remove_isolated_worktree(self, repo: str, worktree_dir: Path) -> None:
        canonical = self.canonical_repo(repo)
        target = worktree_dir.resolve(strict=False)
        source = Path(canonical).resolve(strict=False)
        if target == source or source in target.parents:
            raise ToolError("refusing to remove a path inside the source repository")
        await self.executor.run(
            ["git", "-C", canonical, "worktree", "remove", "--force", str(target)],
            timeout=120,
        )

    async def restore_ref(self, repo: str, ref: str, head: str) -> None:
        canonical = self.canonical_repo(repo)
        await self.assert_clean(canonical)
        if ref:
            await self.executor.run(
                ["git", "-C", canonical, "switch", "--quiet", ref],
                timeout=120,
            )
        else:
            await self.executor.run(
                ["git", "-C", canonical, "switch", "--quiet", "--detach", head],
                timeout=120,
            )

    async def delete_candidate_branch(
        self,
        repo: str,
        branch: str,
        reasons: list[str],
    ) -> None:
        """Delete a revalidated stale candidate branch behind a physical guard."""

        canonical = self.canonical_repo(repo)
        prefix = self.config.candidate_branch_prefix
        suffix = branch[len(prefix) :] if branch.startswith(prefix) else ""
        if not suffix or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", suffix):
            raise ToolError(f"candidate branch is outside the allowlist: {branch}")
        if not reasons:
            raise ToolError(f"candidate branch has no stale reason: {branch}")
        current = await self.executor.run(
            ["git", "-C", canonical, "branch", "--show-current"],
            timeout=30,
        )
        if current.strip() == branch:
            raise ToolError(f"refusing to delete the checked-out candidate branch: {branch}")
        await self.assert_clean(canonical)
        worktrees = await self.executor.run(
            ["git", "-C", canonical, "worktree", "list", "--porcelain"],
            timeout=60,
        )
        if any(line.strip() == f"branch refs/heads/{branch}" for line in worktrees.splitlines()):
            raise ToolError(f"refusing to delete a branch used by another worktree: {branch}")
        await self.executor.run(
            ["git", "-C", canonical, "show-ref", "--verify", f"refs/heads/{branch}"],
            timeout=30,
        )
        await self.executor.run(
            ["git", "-C", canonical, "branch", "-D", branch],
            timeout=60,
        )
