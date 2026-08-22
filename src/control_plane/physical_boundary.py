"""Explicit private physical boundary for Git workspace maintenance.

The portable runtime owns semantic authority and the typed personal providers
own business-visible Git effects.  A few operations are execution scaffolding
needed to keep an Agent session isolated (worktree creation/removal and branch
restoration).  Candidate-branch cleanup is deliberately advisory-only: stale
enumeration is safe, but deletion requires a separate owner-authorized
recovery workflow and is rejected by this boundary.
"""

from __future__ import annotations

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
        """Reject autonomous candidate deletion.

        A stale branch is a recovery/retention fact, not proof that deletion is
        authorized.  The old implementation performed a destructive ``git
        branch -D`` after local shape checks, which still let CLI ``--apply``
        and startup ``cleanup_policy=auto`` erase a branch without an owner
        decision or durable recovery record.  Candidate cleanup is therefore
        advisory-only until a separate owner-authorized recovery workflow is
        implemented.  Keeping the method as an explicit rejection preserves a
        narrow physical boundary for callers and makes accidental re-enablement
        fail closed.
        """

        del repo, branch, reasons
        raise ToolError(
            "candidate branch deletion is disabled; enumerate the stale branch "
            "and use an owner-authorized recovery workflow with durable evidence"
        )
