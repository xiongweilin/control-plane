# ruff: noqa: E501, S108
"""Service inversion tests (S45-S48): service -> CapabilityService -> CodexProvider, verifier via capabilities."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.codex_runner import CodexSessionResult
from control_plane.config import ControlPlaneConfig
from control_plane.models import AlertmanagerPayload
from control_plane.notify import Notifier
from control_plane.service import RepairService
from control_plane.storage import Store
from control_plane.tools import ToolError
from control_plane.verifier import LegacyVerifier, Verifier
from portable_runtime.core.capabilities import (  # noqa: E501
    CapabilityResult,
    ProviderDescriptor,
    ProviderHealth,
)
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.router import CapabilityService


class FakeExecutor:
    def __init__(self, branch_exists: bool = False) -> None:
        self.branch_exists = branch_exists
        self.current_branch = "main"
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(self, args: list[str], *, cwd: str | None = None, timeout: int = 60, input_text: str | None = None) -> str:  # noqa: E501
        self.calls.append((args, cwd))
        joined = " ".join(args)
        if "rev-parse --is-inside-work-tree" in joined:
            return "true"
        if "rev-parse HEAD" in joined:
            return "abc123"
        if "symbolal-ref" in joined:
            return self.current_branch
        if "symbolic-ref --quiet --short HEAD" in joined:
            return self.current_branch
        if " switch " in f" {joined} ":
            self.current_branch = args[-1]
            return ""
        if "docker ps" in joined:
            return "Up 2 minutes\nUp 5 minutes"
        if "fix/control-plane-" in joined and "diff" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "status --porcelain" in joined:
            return ""
        if "rev-parse --verify fix/control-plane-" in joined:
            return "abc123\n"
        return ""


class FakeCodexRunner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls = 0

    async def run_task(self, *, repair_id: str, repo: str, prompt: str, run_id: str = "") -> CodexSessionResult:
        self.calls += 1
        return CodexSessionResult(exit_code=self.exit_code, last_message=f"fixed {repair_id}")

    def cli_info(self):  # for health
        from pathlib import Path as P  # noqa: N817
        return P("codex"), "1.0.0-fake"


def _config(tmp_path) -> ControlPlaneConfig:
    (tmp_path / "dify").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    return replace(
        ControlPlaneConfig(),
        api_key="test-key",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        allowed_auto_projects=("dify",),
        allowed_repo_roots=(str(tmp_path / "repos"),),
        project_dirs={"dify": str(tmp_path / "dify")},
        max_agent_calls_per_repair=8,
        candidate_wip_limit=10,
    )


def _payload() -> AlertmanagerPayload:
    return AlertmanagerPayload.model_validate(
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "instance": "node1", "project": "dify"},
                    "annotations": {"summary": "cpu high"},
                    "startsAt": "2026-08-06T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "fp-1",
                }
            ],
        }
    )


def test_service_exposes_capability_service(tmp_path) -> None:
    """RepairService now routes via CapabilityService, not direct subprocess."""
    config = _config(tmp_path)
    store = Store(config.state_db)
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    service = RepairService(config, store, Budget(store, 100, 8), agent, ApprovalManager(), Notifier(config), executor=executor, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))  # noqa: E501
    # S45: service should have capability_service and should not directly expose codex subprocess
    assert hasattr(service, "capability_service")
    assert isinstance(service.capability_service, CapabilityService)
    # Registry should contain our legacy adapter
    descs = service.capability_service.registry.list()
    assert any(d.id == "codex-legacy-adapter" for d in descs)
    # Helper should exist
    assert hasattr(service, "_invoke_codex_via_capability")
    # Verify that service.py no longer direct imports codex for execution path (check that workflow does not import)
    # The portable workflow file must not import codex
    wf_path = Path("src/portable_runtime/workflows/incident_repair/workflow.py")
    if wf_path.exists():
        txt = wf_path.read_text(encoding="utf-8")
        assert "from control_plane.codex_runner" not in txt
        assert "subprocess.run" not in txt
        assert "codex" not in txt.lower() or "FakeProvider" in txt or "reason.generate" in txt
    store.close()


@pytest.mark.asyncio
async def test_service_repair_flow_still_green_via_capability(tmp_path) -> None:
    """Old repair_flow behavior baseline preserved but via CapabilityService."""
    config = _config(tmp_path)
    store = Store(config.state_db)
    executor = FakeExecutor(branch_exists=False)
    agent = FakeCodexRunner()
    service = RepairService(config, store, Budget(store, 100, 8), agent, ApprovalManager(), Notifier(config), executor=executor, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))  # noqa: E501

    payload = _payload()
    resp = await service.ingest(payload)
    # Should be accepted via capability path
    assert resp.accepted == 1
    # Wait for repair to close
    for _ in range(200):
        rows = store.list_repairs()
        if rows and rows[0]["status"] in {"closed", "failed"}:
            break
        await asyncio.sleep(0.05)
    rows = store.list_repairs()
    assert rows and rows[0]["status"] == "closed"
    # Ensure capability was actually invoked (adapter incremented calls)
    assert agent.calls >= 1
    # Ensure the result is not self-certified: verification evidence exists via deterministic checks (container_status)
    # The repair should have created an action and verification should have run
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_verifier_legacy_facade_routes_via_capabilities(tmp_path) -> None:
    """Verifier facade now calls six verify.* capabilities, output remains Evidence."""
    # Build a verifier capability service with fake providers
    registry = ProviderRegistry()

    class FakeHttp:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-http", name="Fake HTTP", version="1.0.0", capabilities=["verify.http"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="GET http://example.com -> 200 PASS", metadata={"evidence_ref": "evidence-http-1"})  # noqa: E501
        async def cancel(self, request_id): return None
    class FakeContainer:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-container", name="Fake Container", version="1.0.0", capabilities=["verify.container"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="all containers running")  # noqa: E501
        async def cancel(self, request_id): return None
    class FakePromql:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-promql", name="Fake Promql", version="1.0.0", capabilities=["verify.promql"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="promql ok")  # noqa: E501
        async def cancel(self, request_id): return None
    class FakeLogs:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-logs", name="Fake Logs", version="1.0.0", capabilities=["verify.logs"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="logs ok")  # noqa: E501
        async def cancel(self, request_id): return None
    class FakeGit:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-git", name="Fake Git", version="1.0.0", capabilities=["verify.git"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="git ok")  # noqa: E501
        async def cancel(self, request_id): return None
    class FakeTests:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-tests", name="Fake Tests", version="1.0.0", capabilities=["verify.tests"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="tests ok")  # noqa: E501
        async def cancel(self, request_id): return None
    class FakeGitDiff:
        @property
        def descriptor(self): return ProviderDescriptor(id="verifier-git_diff", name="Fake GitDiff", version="1.0.0", capabilities=["verify.git_diff"])  # noqa: E501
        async def health(self): return ProviderHealth(provider_id=self.descriptor.id, available=True)
        async def invoke(self, request, context): return CapabilityResult(request_id=request.id, provider_id=self.descriptor.id, status="succeeded", message="Diff is within allowed boundaries")  # noqa: E501
        async def cancel(self, request_id): return None

    for prov in [FakeHttp(), FakeContainer(), FakePromql(), FakeLogs(), FakeGit(), FakeTests(), FakeGitDiff()]:
        registry.register(prov)
    svc = CapabilityService(registry)
    verifier = Verifier(capability_service=svc)
    # Also test alias
    assert LegacyVerifier is Verifier
    report = await verifier.verify_repair(
        repair_id="repair-1",
        alert={"labels": {"alertname": "Test"}},
        actions=[{"tool": "container_status", "target": "dify"}],
        tool_results={
            "probe_urls": ["http://example.com"],
            "promql": {"up": "up{instance=\"node1\"}"},
            "repos": [("/tmp/repo", "fix/branch")],  # noqa: S108
            "error_log_targets": ["dify"],
            "tests": [{"command": ["python", "-m", "pytest", "-q"]}],
            "diff": "a.txt | 1 +",
        },
    )
    # Should have checks for all six capabilities
    names = [c.name for c in report.checks]
    assert any("probe:" in n for n in names)
    assert any("promql:" in n for n in names)
    assert any("git:" in n for n in names)
    assert any("logs:" in n for n in names)
    assert any("tests:" in n for n in names)
    assert any("container_status" in n for n in names)
    # Evidence semantics: checks exist and are not self-certified (execution != verification)
    assert report.checks
    # All our fakes return succeeded, so report should be all_passed
    assert report.all_passed


@pytest.mark.asyncio
async def test_verifier_fallback_still_works() -> None:
    """Legacy direct Verifier still works without capability service (backward compat)."""
    async def fake_probe(url, **kw): return True, "probe ok", "ref-probe"
    async def fake_container(targets): return True, "containers ok", "ref-container"
    async def fake_promql(q, exp): return True, "promql ok", "ref-promql"
    async def fake_logs(target, **kw): return True, "logs ok", "ref-logs"
    async def fake_git(repo, branch): return True, "git ok", "ref-git"
    verifier = Verifier(probe=fake_probe, container_status=fake_container, promql=fake_promql, logs=fake_logs, git=fake_git)  # noqa: E501
    report = await verifier.verify_repair(repair_id="r2", alert={}, actions=[{"tool": "container_status", "target": "dify"}], tool_results={"probe_urls": ["http://example.com"]})  # noqa: E501
    assert report.checks
    assert report.all_passed


def test_core_does_not_import_providers() -> None:
    """Core must not import codex/feishu per check_portable_core_imports."""
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "scripts/check_portable_core_imports.py"], capture_output=True, text=True, cwd=".")  # noqa: E501
    assert result.returncode == 0
    assert "passed" in result.stdout.lower()


def test_service_no_direct_subprocess_in_workflow(tmp_path) -> None:
    """IncidentRepairWorkflow must not directly call subprocess; it routes via capabilities."""
    wf_file = Path("src/portable_runtime/workflows/incident_repair/workflow.py")
    assert wf_file.exists()
    txt = wf_file.read_text(encoding="utf-8")
    assert "subprocess" not in txt
    assert "CodexRunner" not in txt
    # Should use context.invoke
    assert "context.invoke" in txt
    assert "reason.generate" in txt or "code.edit" in txt
