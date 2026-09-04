from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig


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
            version="2.0.0",
            capabilities=["git.merge", "git.push", "git.rollback", "docker.restart", "docker.compose.up"],
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
        return ProviderHealth(
            provider_id=self.descriptor.id,
            available=git_ok or docker_ok,
            detail=f"git={git_ok} docker={docker_ok}",
        )

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        del context
        try:
            if request.capability.startswith("git."):
                message = await self._git(request)
            elif request.capability.startswith("docker."):
                message = await self._docker(request)
            else:
                return self._result(request, "unavailable", f"unsupported capability {request.capability}")
        except TimeoutError as exc:
            return self._result(request, "unknown", str(exc))
        except (OSError, RuntimeError, ValueError) as exc:
            return self._result(request, "failed", str(exc))
        return self._result(request, "succeeded", message)

    async def cancel(self, request_id: str) -> None:
        del request_id

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        # No profile-local durable effect ledger is retained. Agent Kernel must
        # preserve ambiguous effects as unknown rather than inventing certainty.
        del request_id
        return None

    def _result(self, request: CapabilityRequest, status: str, message: str) -> CapabilityResult:
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status=status,
            message=message,
            metadata={"capability": request.capability, "resource_ref": request.resource_ref or ""},
        )

    async def _run(self, argv: list[str], *, cwd: str | None = None, timeout: float = 120) -> str:
        executable = shutil.which(argv[0]) or argv[0]
        proc = await asyncio.create_subprocess_exec(  # noqa: S603
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
            raise TimeoutError(f"command outcome is ambiguous after timeout: {argv[0]}") from None
        out = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {(err or out)[:800]}")
        return out or "ok"

    def _repo(self, request: CapabilityRequest) -> str:
        repo = str(request.parameters.get("repo", "")).strip()
        if not repo:
            raise ValueError("git operation requires parameters.repo")
        if not self.config.repo_allowed(repo):
            raise ValueError(f"repo is outside personal allowlist: {repo}")
        return repo

    async def _git(self, request: CapabilityRequest) -> str:
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
            return await self._run(["docker", "compose", "restart", service], cwd=project_dir, timeout=180)
        if request.capability == "docker.compose.up":
            return await self._run(["docker", "compose", "up", "-d"], cwd=project_dir, timeout=300)
        raise ValueError(f"unsupported docker capability: {request.capability}")
