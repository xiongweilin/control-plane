from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


class PersonalOperationsProvider:
    """Profile-only Git/Docker effect provider.

    Agent Kernel owns admission, authority, dispatch, recovery and verification.
    This provider only translates an already-admitted capability invocation into
    the personal Windows/Git/Docker command boundary.
    """

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self._descriptor = ProviderDescriptor(
            id="personal-operations",
            name="Personal Git/Docker Operations",
            version="2.1.0",
            capabilities=[
                "git.merge",
                "git.push",
                "git.rollback",
                "git.fast_forward",
                "git.push_exact_ref",
                "chezmoi.apply",
                "docker.restart",
                "docker.compose.up",
                "maintenance.cleanup_known_garbage",
            ],
            priority=20,
            tags={"personal-profile", "side-effect"},
            effect_semantics="reconcilable",
            side_effect_class="reconcilable",
            reversibility="compensatable",
            provider_family="personal-operations",
            execution_domain="windows-local",
            network_domain="github-docker",
            trust_boundary="agent-kernel-authorized",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        git_ok = shutil.which("git.exe") is not None or shutil.which("git") is not None
        docker_ok = shutil.which("docker.exe") is not None or shutil.which("docker") is not None
        chezmoi_ok = shutil.which("chezmoi.exe") is not None or shutil.which("chezmoi") is not None
        maintenance_ok = self._maintenance_boundary_is_valid()
        return ProviderHealth(
            provider_id=self.descriptor.id,
            available=git_ok or docker_ok or chezmoi_ok or maintenance_ok,
            detail=(
                f"git={git_ok} docker={docker_ok} chezmoi={chezmoi_ok} "
                f"maintenance={maintenance_ok}"
            ),
            metadata={
                "maintenance_cleanup_available": maintenance_ok,
                "synchronization_capabilities": [
                    "git.fast_forward",
                    "git.push_exact_ref",
                    "chezmoi.apply",
                ],
            },
        )

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        del context
        try:
            if request.capability.startswith("git."):
                message = await self._git(request)
            elif request.capability == "chezmoi.apply":
                message = await self._chezmoi_apply(request)
            elif request.capability.startswith("docker."):
                message = await self._docker(request)
            elif request.capability == "maintenance.cleanup_known_garbage":
                message = await self._cleanup_known_garbage()
            else:
                return self._result(
                    request,
                    "unavailable",
                    f"unsupported capability {request.capability}",
                )
        except TimeoutError as exc:
            return self._result(request, "unknown", str(exc))
        except (OSError, RuntimeError, ValueError) as exc:
            return self._result(request, "failed", str(exc))
        return self._result(request, "succeeded", message)

    async def _cleanup_known_garbage(self) -> str:
        """Move only exact configured disposable paths to a rollback location.

        This is intentionally narrower than Docker prune or recursive cleanup:
        the source list is configuration-owned, and moving to quarantine keeps a
        recovery path if a path was classified incorrectly.
        """

        if not self.config.automatic_handling_enabled:
            raise ValueError("automatic handling is disabled")
        if not self._maintenance_boundary_is_valid():
            raise ValueError("known-garbage quarantine boundary is invalid")
        quarantine = Path(self.config.garbage_quarantine_dir)
        moved: list[str] = []
        quarantine_resolved = quarantine.resolve(strict=False)
        for raw_path in self.config.known_garbage_paths:
            source = Path(raw_path)
            if not source.exists() and not source.is_symlink():
                continue
            if source.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(source):
                raise ValueError(f"refusing symlink/reparse garbage path: {raw_path}")
            resolved_source = source.resolve(strict=False)
            configured = {
                Path(item).resolve(strict=False) for item in self.config.known_garbage_paths
            }
            if resolved_source not in configured:
                raise ValueError(f"garbage path is outside exact allowlist: {raw_path}")
            if quarantine_resolved == resolved_source or str(quarantine_resolved).startswith(
                str(resolved_source) + "\\"
            ):
                raise ValueError("quarantine directory must not be inside a source path")
            if source.stat().st_dev != quarantine.parent.stat().st_dev:
                raise ValueError("cross-volume garbage quarantine is not reversible")
            quarantine.mkdir(parents=True, exist_ok=True)
            destination = quarantine / f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}"
            suffix = 0
            while destination.exists():
                suffix += 1
                destination = quarantine / (
                    f"{source.name}-{time.strftime('%Y%m%d-%H%M%S')}-{suffix}"
                )
            shutil.move(str(source), str(destination))
            moved.append(raw_path)
        return f"quarantined {len(moved)} configured known-garbage path(s)"

    def _maintenance_boundary_is_valid(self) -> bool:
        if not self.config.automatic_handling_enabled or not self.config.known_garbage_paths:
            return False
        try:
            quarantine = Path(self.config.garbage_quarantine_dir).resolve(strict=False)
            if any(
                quarantine == Path(raw).resolve(strict=False)
                or str(quarantine).startswith(str(Path(raw).resolve(strict=False)) + "\\")
                for raw in self.config.known_garbage_paths
            ):
                return False
            return not (quarantine.exists() and not quarantine.is_dir())
        except OSError:
            return False

    async def cancel(self, request_id: str) -> None:
        del request_id

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        # No profile-local durable effect ledger is retained. Agent Kernel must
        # preserve ambiguous effects as unknown rather than inventing certainty.
        del request_id
        return None

    def _result(
        self,
        request: CapabilityRequest,
        status: str,
        message: str,
    ) -> CapabilityResult:
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status=status,
            message=message,
            metadata={
                "capability": request.capability,
                "resource_ref": request.resource_ref or "",
            },
        )

    async def _run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout: float = 120,
    ) -> str:
        executable = shutil.which(argv[0]) or argv[0]
        proc = await asyncio.create_subprocess_exec(
            executable,
            *argv[1:],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(
                f"command outcome is ambiguous after timeout: {argv[0]}"
            ) from None
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {(err or out)[:800]}")
        return out

    def _repo(self, request: CapabilityRequest) -> str:
        repo = str(request.parameters.get("repo") or request.resource_ref or "").strip()
        if not repo:
            raise ValueError("git operation requires parameters.repo")
        if not self.config.repo_allowed(repo):
            raise ValueError(f"repo is outside personal allowlist: {repo}")
        return repo

    def _sync_repo(self, request: CapabilityRequest) -> str:
        if not self.config.automatic_handling_enabled:
            raise ValueError("automatic handling is disabled")
        project = str(request.parameters.get("project", "")).strip()
        if project not in self.config.allowed_auto_projects:
            raise ValueError(f"synchronization project is outside personal allowlist: {project}")
        configured_repo = self.config.project_dirs.get(project)
        repo = str(request.parameters.get("repo") or request.resource_ref or "").strip()
        if not configured_repo or not repo:
            raise ValueError("synchronization requires an exact project and repo")
        if Path(repo).resolve(strict=False) != Path(configured_repo).resolve(strict=False):
            raise ValueError("synchronization repo does not match the configured project")
        if not self.config.repo_allowed(repo):
            raise ValueError(f"synchronization repo is outside personal allowlist: {repo}")
        if request.actor_ref != self.config.owner_principal:
            raise ValueError("synchronization actor does not match the configured owner")
        return repo

    @staticmethod
    def _required_sha(request: CapabilityRequest, name: str) -> str:
        value = str(request.parameters.get(name, "")).strip().lower()
        if not _GIT_SHA.fullmatch(value):
            raise ValueError(f"{name} must be a full 40-character SHA")
        return value

    @staticmethod
    def _required_remote(request: CapabilityRequest) -> str:
        remote = str(request.parameters.get("remote", "")).strip()
        if not _REMOTE_NAME.fullmatch(remote):
            raise ValueError("remote is not a valid Git remote name")
        return remote

    @staticmethod
    def _required_branch(request: CapabilityRequest) -> str:
        branch = str(request.parameters.get("branch", "")).strip()
        if (
            not _BRANCH_NAME.fullmatch(branch)
            or ".." in branch
            or "@{" in branch
            or branch.endswith(".")
            or branch.endswith("/")
        ):
            raise ValueError("branch is not a valid Git branch name")
        return branch

    async def _git_clean(self, repo: str) -> None:
        status = await self._run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
        )
        if status:
            raise ValueError("repository worktree is not clean")

    async def _git_head(self, repo: str) -> str:
        value = (await self._run(["git", "rev-parse", "HEAD"], cwd=repo)).strip().lower()
        if not _GIT_SHA.fullmatch(value):
            raise RuntimeError("repository HEAD is not a full commit SHA")
        return value

    async def _git_branch(self, repo: str) -> str:
        try:
            return (
                await self._run(
                    ["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=repo
                )
            ).strip()
        except RuntimeError as exc:
            raise ValueError("repository is detached or has no symbolic branch") from exc

    async def _remote_sha(self, repo: str, remote: str, branch: str) -> str:
        output = await self._run(
            ["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"], cwd=repo
        )
        rows = [line.split() for line in output.splitlines() if line.strip()]
        if len(rows) != 1 or len(rows[0]) < 2 or rows[0][1] != f"refs/heads/{branch}":
            raise RuntimeError("target remote branch is missing or ambiguous")
        remote_sha = rows[0][0].lower()
        if not _GIT_SHA.fullmatch(remote_sha):
            raise RuntimeError("target remote branch did not return a full commit SHA")
        return remote_sha

    async def _is_ancestor(self, repo: str, old_sha: str, new_sha: str) -> bool:
        try:
            await self._run(["git", "merge-base", "--is-ancestor", old_sha, new_sha], cwd=repo)
        except RuntimeError:
            return False
        return True

    async def _git_fast_forward(self, request: CapabilityRequest) -> str:
        repo = self._sync_repo(request)
        remote = self._required_remote(request)
        branch = self._required_branch(request)
        old_sha = self._required_sha(request, "expected_old_sha")
        remote_sha = self._required_sha(request, "expected_remote_sha")

        await self._git_clean(repo)
        if await self._git_branch(repo) != branch:
            raise ValueError("current branch does not match the synchronization target")
        current_sha = await self._git_head(repo)
        observed_remote_sha = await self._remote_sha(repo, remote, branch)
        if observed_remote_sha != remote_sha:
            raise ValueError("remote branch changed after diagnosis")
        if current_sha == remote_sha:
            return "synchronization already complete"
        if current_sha != old_sha:
            raise ValueError("local HEAD changed after diagnosis")
        if not await self._is_ancestor(repo, old_sha, remote_sha):
            raise ValueError("fast-forward target is not a descendant of local HEAD")

        await self._run(
            ["git", "fetch", "--no-tags", remote, f"refs/heads/{branch}"],
            cwd=repo,
            timeout=900,
        )
        fetched_sha = (
            await self._run(["git", "rev-parse", "FETCH_HEAD"], cwd=repo)
        ).strip().lower()
        if fetched_sha != remote_sha:
            raise ValueError("fetched target changed after diagnosis")
        await self._git_clean(repo)
        if await self._git_head(repo) != old_sha:
            raise ValueError("local HEAD changed during synchronization preflight")
        await self._run(["git", "merge", "--ff-only", "FETCH_HEAD"], cwd=repo, timeout=900)
        if await self._git_head(repo) != remote_sha:
            raise RuntimeError("fast-forward postcondition did not reach the expected SHA")
        await self._git_clean(repo)
        return f"fast-forwarded {branch} to {remote_sha}"

    async def _git_push_exact_ref(self, request: CapabilityRequest) -> str:
        repo = self._sync_repo(request)
        remote = self._required_remote(request)
        branch = self._required_branch(request)
        old_sha = self._required_sha(request, "expected_old_sha")
        new_sha = self._required_sha(request, "expected_new_sha")

        await self._git_clean(repo)
        if await self._git_branch(repo) != branch:
            raise ValueError("current branch does not match the synchronization target")
        if await self._git_head(repo) != new_sha:
            raise ValueError("local HEAD does not match the exact push target")
        observed_remote_sha = await self._remote_sha(repo, remote, branch)
        if observed_remote_sha == new_sha:
            return "synchronization already complete"
        if observed_remote_sha != old_sha:
            raise ValueError("remote branch changed after diagnosis")
        if not await self._is_ancestor(repo, old_sha, new_sha):
            raise ValueError("push target is not a fast-forward descendant")

        try:
            await self._run(
                ["git", "push", remote, f"{new_sha}:refs/heads/{branch}"],
                cwd=repo,
                timeout=900,
            )
        except TimeoutError:
            if await self._remote_sha(repo, remote, branch) == new_sha:
                return "exact push completed before the provider timeout"
            raise
        if await self._remote_sha(repo, remote, branch) != new_sha:
            raise RuntimeError("push postcondition did not reach the expected remote SHA")
        await self._git_clean(repo)
        return f"pushed {branch} at exact SHA {new_sha}"

    async def _chezmoi_apply(self, request: CapabilityRequest) -> str:
        repo = self._sync_repo(request)
        source_dir = str(request.parameters.get("source_dir", "")).strip()
        configured_source = self.config.chezmoi_source_dir
        expected_source_sha = self._required_sha(request, "expected_source_sha")
        if (
            not source_dir
            or Path(source_dir).resolve(strict=False)
            != Path(configured_source).resolve(strict=False)
            or Path(repo).resolve(strict=False) != Path(configured_source).resolve(strict=False)
        ):
            raise ValueError("chezmoi source is outside the configured exact boundary")
        if await self._git_head(repo) != expected_source_sha:
            raise ValueError("chezmoi source revision changed after diagnosis")
        await self._run(
            ["chezmoi", "verify", "--skip-secrets", "--no-tty", "--source", source_dir],
            timeout=900,
        )
        await self._run(
            ["chezmoi", "apply", "--skip-secrets", "--no-tty", "--source", source_dir],
            timeout=900,
        )
        await self._run(
            ["chezmoi", "verify", "--skip-secrets", "--no-tty", "--source", source_dir],
            timeout=900,
        )
        return "chezmoi source verified and applied"

    async def _git(self, request: CapabilityRequest) -> str:
        if request.capability == "git.fast_forward":
            return await self._git_fast_forward(request)
        if request.capability == "git.push_exact_ref":
            return await self._git_push_exact_ref(request)
        repo = self._repo(request)
        if request.capability == "git.push":
            remote = str(request.parameters.get("remote", "origin"))
            branch = str(request.parameters.get("branch", "main"))
            return await self._run(["git", "push", remote, branch], cwd=repo)
        if request.capability == "git.merge":
            branch = str(request.parameters.get("branch", "")).strip()
            target = str(request.parameters.get("target", "main"))
            if not branch:
                raise ValueError("git.merge requires parameters.branch")
            await self._run(["git", "checkout", target], cwd=repo)
            return await self._run(["git", "merge", "--no-ff", branch], cwd=repo)
        if request.capability == "git.rollback":
            target = str(request.parameters.get("target", "")).strip()
            if not target:
                raise ValueError("git.rollback requires parameters.target")
            return await self._run(["git", "reset", "--hard", target], cwd=repo)
        raise ValueError(f"unsupported git capability: {request.capability}")

    async def _docker(self, request: CapabilityRequest) -> str:
        project = str(request.parameters.get("project", "")).strip()
        if project not in self.config.allowed_auto_projects:
            raise ValueError(f"docker project is outside personal allowlist: {project}")
        project_dir = self.config.project_dirs.get(project)
        if not project_dir:
            raise ValueError(f"no compose directory configured for project: {project}")
        if request.capability == "docker.restart":
            service = str(request.parameters.get("service", "")).strip()
            if not service:
                raise ValueError("docker.restart requires parameters.service")
            return await self._run(
                ["docker", "compose", "restart", service],
                cwd=project_dir,
                timeout=180,
            )
        if request.capability == "docker.compose.up":
            return await self._run(
                ["docker", "compose", "up", "-d"],
                cwd=project_dir,
                timeout=300,
            )
        raise ValueError(f"unsupported docker capability: {request.capability}")
