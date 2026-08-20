"""Verifier capability providers: each exposes a distinct verify.* capability.

Each provider delegates to a lightweight check but remains independent from the
LLM self-report. The legacy Verifier facade can call these via CapabilityService.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

logger = logging.getLogger(__name__)


class HttpVerifierProvider:
    def __init__(self, provider_id: str = "verifier-http") -> None:
        self._descriptor = ProviderDescriptor(id=provider_id, name="HTTP Verifier", version="1.0.0", capabilities=["verify.http"], tags={"verify", "side-effect-free"}, priority=5)  # noqa: E501

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True, detail="http verifier ready")

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        url = str(request.parameters.get("url", "") or request.parameters.get("probe_url", ""))
        if not url:
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": "invalid_request", "message": "verify.http requires parameters.url"})  # noqa: E501
        expected: set[int] = set(request.parameters.get("expected_status", [200, 301, 302]))
        if "expected" in request.parameters:
            expected = set(request.parameters["expected"])
        body_contains = request.parameters.get("body_contains")
        timeout = float(request.parameters.get("timeout_seconds", 10))
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                ok = resp.status_code in expected
                if body_contains and body_contains not in resp.text:
                    ok = False
                evidence_status = "supported" if ok else "contested"
                return CapabilityResult(
                    request_id=request.id,
                    provider_id=self.descriptor.id,
                    status="succeeded" if ok else "failed",
                    message=f"GET {url} -> {resp.status_code} {'PASS' if ok else 'FAIL'}",
                    metadata={"status_code": resp.status_code, "evidence_status": evidence_status, "body_snippet": resp.text[:2000]},  # noqa: E501
                    evidence_refs=[],
                )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": type(exc).__name__, "message": str(exc)[:2000]})  # noqa: E501

    async def cancel(self, request_id: str) -> None:
        return None


class PromqlVerifierProvider:
    def __init__(self, provider_id: str = "verifier-promql", prometheus_url: str = "http://127.0.0.1:19090") -> None:
        self._prometheus_url = prometheus_url.rstrip("/")
        self._descriptor = ProviderDescriptor(id=provider_id, name="PromQL Verifier", version="1.0.0", capabilities=["verify.promql"], tags={"verify"}, priority=5, metadata={"prometheus_url": self._prometheus_url})  # noqa: E501

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        # Non-fatal: prometheus may be unavailable in portable-local profile
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self._prometheus_url}/-/healthy")
                if resp.status_code == 200:
                    return ProviderHealth(provider_id=self.descriptor.id, available=True, detail="prometheus healthy")
                return ProviderHealth(provider_id=self.descriptor.id, available=False, detail=f"prometheus {resp.status_code}")  # noqa: E501
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(provider_id=self.descriptor.id, available=False, detail=f"prometheus unreachable: {exc}"[:300])  # noqa: E501

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        query = str(request.parameters.get("query", "") or request.parameters.get("promql", ""))
        if not query:
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": "invalid_request", "message": "verify.promql requires parameters.query"})  # noqa: E501
        expected = request.parameters.get("expected")
        timeout = float(request.parameters.get("timeout_seconds", 10))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self._prometheus_url}/api/v1/query", params={"query": query})
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                ok = resp.status_code == 200
                # Minimal expected check: if expected is set, compare result
                if expected is not None:
                    ok = ok and str(data)[:500].find(str(expected)) != -1
                return CapabilityResult(
                    request_id=request.id,
                    provider_id=self.descriptor.id,
                    status="succeeded" if ok else "failed",
                    message=f"promql {query[:200]} -> {resp.status_code} {'PASS' if ok else 'FAIL'}",
                    metadata={"prometheus_url": self._prometheus_url, "response": str(data)[:2000]},
                )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": type(exc).__name__, "message": str(exc)[:2000]})  # noqa: E501

    async def cancel(self, request_id: str) -> None:
        return None


class ContainerVerifierProvider:
    def __init__(self, provider_id: str = "verifier-container") -> None:
        self._descriptor = ProviderDescriptor(id=provider_id, name="Container Verifier", version="1.0.0", capabilities=["verify.container"], tags={"verify"}, priority=5)  # noqa: E501

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        import shutil

        docker = shutil.which("docker")
        return ProviderHealth(provider_id=self.descriptor.id, available=docker is not None, detail="docker found" if docker else "docker not on PATH (optional)")  # noqa: E501

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        targets = request.parameters.get("targets") or request.parameters.get("containers") or []
        if isinstance(targets, str):
            targets = [targets]
        if not targets:
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": "invalid_request", "message": "verify.container requires parameters.targets"})  # noqa: E501
        # Delegate to control_plane.tools container check if available
        try:
            from control_plane.tools import check_container_status  # type: ignore[attr-defined]

            # check_container_status is async? In verifier it is injected; we attempt generic
            ok, message, ref = await check_container_status(targets)
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded" if ok else "failed", message=message, metadata={"evidence_ref": ref})  # noqa: E501
        except Exception:
            # Fallback: try docker inspect via subprocess
            try:
                proc = await asyncio.create_subprocess_exec("docker", "ps", "--format", "{{.Names}}\t{{.Status}}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)  # noqa: E501
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                text = stdout.decode(errors="replace")
                missing = [t for t in targets if t not in text]
                ok = not missing
                return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded" if ok else "failed", message=f"containers {targets} missing={missing}" if missing else "all containers running")  # noqa: E501
            except Exception as exc:  # noqa: BLE001
                return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": type(exc).__name__, "message": str(exc)[:2000]})  # noqa: E501

    async def cancel(self, request_id: str) -> None:
        return None


class GitVerifierProvider:
    def __init__(self, provider_id: str = "verifier-git") -> None:
        self._descriptor = ProviderDescriptor(id=provider_id, name="Git Verifier", version="1.0.0", capabilities=["verify.git"], tags={"verify", "side-effect-free"}, priority=5)  # noqa: E501

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        import shutil

        git = shutil.which("git")
        return ProviderHealth(provider_id=self.descriptor.id, available=git is not None, detail="git found" if git else "git not found")  # noqa: E501

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        repo = str(request.parameters.get("repo", "") or "")
        branch = str(request.parameters.get("branch", "") or request.parameters.get("ref", "") or "")
        if not repo:
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": "invalid_request", "message": "verify.git requires parameters.repo"})  # noqa: E501
        try:
            # Use git ls-remote style check via subprocess
            cmd = ["git", "ls-remote", "--heads", repo, branch] if branch else ["git", "ls-remote", repo]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)  # noqa: E501
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            ok = proc.returncode == 0
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded" if ok else "failed", message=stdout.decode(errors="replace")[:2000] or stderr.decode(errors="replace")[:2000], metadata={"exit_code": proc.returncode})  # noqa: E501
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="failed", error={"type": type(exc).__name__, "message": str(exc)[:2000]})  # noqa: E501

    async def cancel(self, request_id: str) -> None:
        return None
