"""Typed personal Git/Docker operations for the private profile.

These operations are deliberately separate from the Codex provider. Codex may
recommend a merge, push, or runtime restart, but only this provider can perform
the corresponding side effect after the portable Runtime has evaluated a
scoped request and AuthorizationGrant.
"""

from __future__ import annotations

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


class PersonalOperationsProvider:
    """Execute only explicitly named Git and Docker profile operations."""

    def __init__(self, config: ControlPlaneConfig, executor: CommandExecutor) -> None:
        self.config = config
        self.executor = executor
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
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="failed",
                message=str(exc),
                error={"code": "PersonalOperationFailed", "reason": str(exc)},
            )
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=output,
            metadata={"operation": request.capability, "resource_ref": request.resource_ref or ""},
        )

    async def _git_merge(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
        target = str(request.parameters.get("target", "main"))
        await self.executor.run(["git", "-C", repo, "checkout", "-q", target], timeout=120)
        try:
            return await self.executor.run(["git", "-C", repo, "merge", "--ff-only", branch], timeout=120)
        except ToolError:
            return await self.executor.run(["git", "-C", repo, "merge", "-q", "--no-edit", branch], timeout=120)

    async def _git_push(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        remote = str(request.parameters.get("remote", "origin"))
        branch = str(request.parameters.get("branch", "main"))
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
        return detail

    async def _git_rollback(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
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
        return await self.executor.run(command, cwd=project_dir, timeout=180)

    @staticmethod
    def _required(request: CapabilityRequest, name: str) -> str:
        value = request.parameters.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"missing required operation parameter: {name}")
        return value

    async def cancel(self, request_id: str) -> None:
        return None

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        return None
