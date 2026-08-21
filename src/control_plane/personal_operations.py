"""Typed personal Git/Docker operations for the private profile.

These operations are deliberately separate from the Codex provider. Codex may
recommend a merge, push, or runtime restart, but only this provider can perform
the corresponding side effect after the portable Runtime has evaluated a
scoped request and AuthorizationGrant.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig
from .gitpush import push_with_ssh_fallback
from .tools import CommandExecutor, ToolError


@dataclass(slots=True)
class _OperationJournalEntry:
    """Process-local recovery journal for provider invocations.

    The portable runtime persists the request/attempt identity.  This small
    provider journal keeps the operation-specific parameters needed to query
    reality again.  It is intentionally non-authoritative: reconciliation
    always reads Git/Docker state rather than trusting this record.
    """

    request: CapabilityRequest
    state: str = "running"
    metadata: dict[str, Any] = field(default_factory=dict)


class PersonalOperationsProvider:
    """Execute only explicitly named Git and Docker profile operations."""

    def __init__(self, config: ControlPlaneConfig, executor: CommandExecutor) -> None:
        self.config = config
        self.executor = executor
        self._journal: dict[str, _OperationJournalEntry] = {}
        self._descriptor = ProviderDescriptor(
            id="personal-operations",
            name="Personal Git/Docker Operations",
            version="1.0.0",
            capabilities=["git.merge", "git.push", "git.rollback", "docker.restart", "docker.compose.up"],
            priority=20,
            tags={"personal-profile", "side-effect"},
            effect_semantics="reconcilable",
            side_effect_class="reconcilable",
            reversibility="compensatable",
            provider_family="personal-operations",
            execution_domain="windows-local",
            network_domain="github-docker",
            trust_boundary="control-plane-authorized",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True, detail="personal operations ready")

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        self._journal[request.id] = _OperationJournalEntry(request=request.model_copy(deep=True))
        try:
            if request.capability == "git.merge":
                output = await self._git_merge(request)
            elif request.capability == "git.push":
                output = await self._git_push(request)
            elif request.capability == "git.rollback":
                output = await self._git_rollback(request)
            elif request.capability in {"docker.restart", "docker.compose.up"}:
                output = await self._docker(request)
            else:
                return CapabilityResult(
                    request_id=request.id,
                    provider_id=self.descriptor.id,
                    status="unavailable",
                    message=f"unsupported personal operation {request.capability}",
                    error={"code": "UnsupportedCapability"},
                )
        except ToolError as exc:
            entry = self._journal[request.id]
            ambiguous = self._is_ambiguous_error(exc)
            entry.state = "unknown" if ambiguous else "failed"
            entry.metadata.setdefault("error", str(exc))
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="unknown" if ambiguous else "failed",
                message=str(exc),
                error={
                    "code": "PersonalOperationUncertain" if ambiguous else "PersonalOperationFailed",
                    "reason": str(exc),
                },
                metadata={
                    "operation": request.capability,
                    "resource_ref": request.resource_ref or "",
                    **entry.metadata,
                },
            )
        entry = self._journal[request.id]
        entry.state = "succeeded"
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=output,
            metadata={
                "operation": request.capability,
                "resource_ref": request.resource_ref or "",
                **entry.metadata,
            },
        )

    async def _git_merge(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
        target = str(request.parameters.get("target", "main"))
        await self.executor.run(["git", "-C", repo, "checkout", "-q", target], timeout=120)
        try:
            output = await self.executor.run(["git", "-C", repo, "merge", "--ff-only", branch], timeout=120)
        except ToolError:
            try:
                output = await self.executor.run(["git", "-C", repo, "merge", "-q", "--no-edit", branch], timeout=120)
            except ToolError as exc:
                # A failed non-FF merge may leave MERGE_HEAD/index state.  An
                # abort is best-effort, but the post-abort status is always
                # captured so callers can distinguish a clean failure from an
                # operation that still needs recovery.
                abort_error = ""
                try:
                    await self.executor.run(["git", "-C", repo, "merge", "--abort"], timeout=120)
                except ToolError as abort_exc:
                    abort_error = str(abort_exc)
                status = await self._git_status(repo)
                self._journal[request.id].metadata.update(
                    {
                        "merge_aborted": not bool(abort_error),
                        "merge_status": status,
                        "merge_abort_error": abort_error,
                    }
                )
                raise ToolError(f"git merge failed and was aborted; status={status}") from exc
        status = await self._git_status(repo)
        if self._merge_in_progress(status):
            self._journal[request.id].metadata.update({"merge_status": status, "merge_in_progress": True})
            raise ToolError(f"git merge returned but merge is still in progress; status={status}")
        self._journal[request.id].metadata.update({"merge_status": status, "merge_in_progress": False})
        return output

    async def _git_push(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        remote = str(request.parameters.get("remote", "origin"))
        branch = str(request.parameters.get("branch", "main"))
        expected_commit = await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)
        expected_commit = expected_commit.strip()
        self._journal[request.id].metadata["expected_commit"] = expected_commit
        self._journal[request.id].metadata.update({"remote": remote, "branch": branch, "repo": repo})
        pushed, detail = await push_with_ssh_fallback(
            self.executor,
            repo,
            remote=remote,
            branch=branch,
            timeout=self.config.git_push_timeout_seconds,
            fallback_enabled=self.config.github_ssh_fallback,
            fallback_host=self.config.github_ssh_host_port,
        )
        if not pushed:
            raise ToolError(detail)
        remote_commit = await self._remote_ref(repo, remote, branch)
        if remote_commit is None:
            raise ToolError("git push completed but remote ref could not be confirmed")
        self._journal[request.id].metadata["remote_commit"] = remote_commit
        if remote_commit != expected_commit:
            raise ToolError(
                f"git push remote ref mismatch: expected {expected_commit}, observed {remote_commit}"
            )
        return detail

    async def _git_rollback(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
        self._journal[request.id].metadata.update({"repo": repo, "branch": branch})
        await self.executor.run(["git", "-C", repo, "checkout", "-q", "main"], timeout=120)
        return await self.executor.run(["git", "-C", repo, "branch", "-D", branch], timeout=120)

    async def _docker(self, request: CapabilityRequest) -> str:
        project = str(request.parameters.get("project", ""))
        if project not in self.config.allowed_auto_projects:
            raise ToolError(f"docker project is not allowlisted: {project}")
        project_dir = self.config.project_dirs.get(project, f"D:\\infrastructure\\compose\\{project}")
        command = (
            ["docker", "compose", "restart"]
            if request.capability == "docker.restart"
            else ["docker", "compose", "up", "-d"]
        )
        output = await self.executor.run(command, cwd=project_dir, timeout=180)
        status = await self._docker_status(project)
        self._journal[request.id].metadata.update(
            {"project": project, "project_dir": project_dir, "container_status": status}
        )
        if not self._containers_healthy(status):
            raise ToolError(f"docker operation completed but containers are not healthy: {status or '(none)'}")
        return output or status

    @staticmethod
    def _required(request: CapabilityRequest, name: str) -> str:
        value = request.parameters.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"missing required operation parameter: {name}")
        return value

    async def cancel(self, request_id: str) -> None:
        return None

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        entry = self._journal.get(request_id)
        if entry is None:
            return None
        request = entry.request
        try:
            if request.capability == "git.merge":
                status = await self._git_status(self._required(request, "repo"))
                if self._merge_in_progress(status):
                    with contextlib.suppress(ToolError):
                        await self.executor.run(
                            ["git", "-C", self._required(request, "repo"), "merge", "--abort"],
                            timeout=120,
                        )
                    status = await self._git_status(self._required(request, "repo"))
                    entry.state = "failed"
                    aborted = not self._merge_in_progress(status)
                    entry.metadata.update(
                        {"merge_status": status, "merge_aborted": aborted}
                    )
                    return self._reconciled(
                        request,
                        "failed",
                        "merge conflict aborted; merge was not applied"
                        if aborted
                        else "merge conflict remains unresolved; abort attempted",
                        entry.metadata,
                    )
                entry.state = "succeeded" if not self._merge_in_progress(status) else "unknown"
                entry.metadata["merge_status"] = status
                return self._reconciled(request, "succeeded", "git merge state confirmed", entry.metadata)

            if request.capability == "git.push":
                repo = self._required(request, "repo")
                remote = str(request.parameters.get("remote", "origin"))
                branch = str(request.parameters.get("branch", "main"))
                expected = str(entry.metadata.get("expected_commit") or "").strip()
                if not expected:
                    expected = (await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)).strip()
                observed = await self._remote_ref(repo, remote, branch)
                entry.metadata.update({"expected_commit": expected, "remote_commit": observed or ""})
                if observed is None:
                    entry.state = "unknown"
                    return self._reconciled(request, "unknown", "remote ref is not observable", entry.metadata)
                if observed != expected:
                    entry.state = "failed"
                    return self._reconciled(
                        request,
                        "failed",
                        f"remote ref mismatch: expected {expected}, observed {observed}",
                        entry.metadata,
                    )
                entry.state = "succeeded"
                return self._reconciled(request, "succeeded", "remote ref matches expected commit", entry.metadata)

            if request.capability in {"docker.restart", "docker.compose.up"}:
                project = str(request.parameters.get("project", ""))
                status = await self._docker_status(project)
                entry.metadata["container_status"] = status
                if self._containers_healthy(status):
                    entry.state = "succeeded"
                    return self._reconciled(request, "succeeded", "Docker container health confirmed", entry.metadata)
                entry.state = "unknown"
                return self._reconciled(
                    request,
                    "unknown",
                    "Docker state is not healthy or not yet observable",
                    entry.metadata,
                )

            if request.capability == "git.rollback":
                repo = self._required(request, "repo")
                branch = self._required(request, "branch")
                branch_listing = await self.executor.run(
                    ["git", "-C", repo, "branch", "--list", branch],
                    timeout=60,
                )
                exists = bool(branch_listing.strip())
                entry.metadata.update({"repo": repo, "branch": branch, "branch_exists": exists})
                entry.state = "failed" if exists else "succeeded"
                return self._reconciled(
                    request,
                    "failed" if exists else "succeeded",
                    "rollback branch still exists" if exists else "rollback branch absent",
                    entry.metadata,
                )
        except ToolError as exc:
            entry.state = "unknown"
            entry.metadata["reconcile_error"] = str(exc)
            return self._reconciled(request, "unknown", str(exc), entry.metadata)
        return None

    async def _git_status(self, repo: str) -> str:
        return await self.executor.run(["git", "-C", repo, "status", "--short", "--branch"], timeout=60)

    async def _remote_ref(self, repo: str, remote: str, branch: str) -> str | None:
        output = await self.executor.run(
            ["git", "-C", repo, "ls-remote", remote, f"refs/heads/{branch}"],
            timeout=self.config.git_push_timeout_seconds,
        )
        for line in output.splitlines():
            fields = line.strip().split()
            if fields and fields[0] and fields[0] != "-":
                return fields[0]
        return None

    async def _docker_status(self, project: str) -> str:
        if project not in self.config.allowed_auto_projects:
            raise ToolError(f"docker project is not allowlisted: {project}")
        return await self.executor.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            timeout=60,
        )

    @staticmethod
    def _containers_healthy(status: str) -> bool:
        lines = [line.strip() for line in status.splitlines() if line.strip()]
        if not lines:
            return False
        return all(
            len(line.split("\t", 1)) == 2
            and line.split("\t", 1)[1].startswith("Up")
            and "unhealthy" not in line.lower()
            and "restarting" not in line.lower()
            for line in lines
        )

    @staticmethod
    def _merge_in_progress(status: str) -> bool:
        lowered = status.lower()
        if "merge_head" in lowered or "you are in the middle of a merge" in lowered or "unmerged paths" in lowered:
            return True
        return any(
            line[:2] in {"uu", "aa", "dd", "au", "ua", "du", "ud"}
            for line in (part.strip().lower() for part in status.splitlines())
        )

    @staticmethod
    def _is_ambiguous_error(exc: ToolError) -> bool:
        lowered = str(exc).lower()
        return any(
            marker in lowered
            for marker in (
                "timed out",
                "timeout",
                "connection refused",
                "connection reset",
                "remote end hung up",
                "network is unreachable",
                "no route to host",
                "could not resolve host",
                "could not be confirmed",
                "not healthy",
                "not observable",
                "not yet observable",
            )
        )

    def _reconciled(
        self,
        request: CapabilityRequest,
        status: str,
        message: str,
        metadata: dict[str, Any],
    ) -> CapabilityResult:
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status=status,  # type: ignore[arg-type]
            message=message,
            metadata={"operation": request.capability, **metadata},
            reconciled=True,
        )
