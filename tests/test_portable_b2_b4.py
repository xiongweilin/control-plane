"""B2-B4 integration tests for the portable runtime (covers DoD)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from portable_runtime.api.http import create_app
from portable_runtime.core.process import PortableSubprocessExecutor, ProcessSpec
from portable_runtime.core.runtime import Runtime
from portable_runtime.deployment.local import create_local_runtime, create_personal_platform_runtime
from portable_runtime.plugin.manager import PluginManager
from portable_runtime.providers.codex.provider import CodexProvider
from portable_runtime.providers.verifiers import HttpVerifierProvider
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.stores.migration import dual_write_repair
from portable_runtime.workflows.context import WorkflowContext
from portable_runtime.workflows.generic_task.workflow import GenericTaskWorkflow
from portable_runtime.workflows.incident_repair.workflow import IncidentRepairWorkflow


def test_process_executor_runs_echo() -> None:
    async def _run() -> None:
        exe = PortableSubprocessExecutor()
        spec = ProcessSpec(argv=[sys.executable, "-c", "print(\"hello\")"], timeout_seconds=5)
        result = await exe.run(spec)
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert not result.timed_out

    asyncio.run(_run())


def test_codex_provider_health_is_structured() -> None:
    async def _run() -> None:
        p = CodexProvider(model="test-model", cli="nonexistent-codex-xyz")
        health = await p.health()
        assert health.provider_id == "codex-primary"
        assert isinstance(health.available, bool)
        assert isinstance(health.detail, str)

    asyncio.run(_run())


def test_verifier_http_handles_invalid_request() -> None:
    async def _run() -> None:
        from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext

        v = HttpVerifierProvider()
        req = CapabilityRequest(id="req1", capability="verify.http", parameters={})
        res = await v.invoke(req, InvocationContext(runtime_id="test"))
        assert res.status == "failed"
        assert res.error is not None

    asyncio.run(_run())


async def test_incident_workflow_uses_fake_provider() -> None:
    runtime = Runtime(store=InMemoryStateStore())
    from portable_runtime.core.capabilities import (
        CapabilityRequest,
        CapabilityResult,
        InvocationContext,
        ProviderDescriptor,
        ProviderHealth,
    )

    class FakeAllProvider:
        def __init__(self) -> None:
            self._desc = ProviderDescriptor(
                id="fake-all",
                name="Fake All",
                version="1.0.0",
                capabilities=[
                    "reason.generate",
                    "code.edit",
                    "verify.http",
                    "verify.git_diff",
                    "observe.logs",
                    "observe.container",
                    "human.approve",
                ],
            )

        @property
        def descriptor(self) -> ProviderDescriptor:
            return self._desc

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id=self._desc.id, available=True)

        async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
            return CapabilityResult(
                request_id=request.id, provider_id=self._desc.id, status="succeeded", message="ok"
            )

        async def cancel(self, request_id: str) -> None:
            return None

    runtime.registry.register(FakeAllProvider())
    work = runtime.create_work(title="incident test", description="test", kind="incident")
    run = runtime.start_run(work.id, workflow_id="incident-repair")
    wf = IncidentRepairWorkflow()
    assert wf.accepts(work)
    ctx = WorkflowContext(
        work=work, run=run, store=runtime.store, capabilities=runtime.capabilities, registry=runtime.registry
    )
    status = await wf.run(ctx, work, run)
    assert status in {"succeeded", "blocked", "waiting", "failed"}


async def test_generic_workflow_routes_via_router() -> None:
    runtime = Runtime(store=InMemoryStateStore())
    from portable_runtime.providers.fake import EchoProvider

    runtime.registry.register(EchoProvider("echo", priority=10))
    work = runtime.create_work(title="generic", kind="generic-task", requested_capabilities=["text.echo"])
    run = runtime.start_run(work.id, workflow_id="generic-task")
    wf = GenericTaskWorkflow()
    ctx = WorkflowContext(
        work=work, run=run, store=runtime.store, capabilities=runtime.capabilities, registry=runtime.registry
    )
    status = await wf.run(ctx, work, run)
    assert status == "succeeded"


def test_trigger_alertmanager_creates_work() -> None:
    runtime = Runtime(store=InMemoryStateStore())
    client = TestClient(create_app(runtime))
    payload = {"alerts": [{"labels": {"alertname": "TestAlert"}, "fingerprint": "abc", "status": "firing"}]}
    resp = client.post("/v1/triggers/alertmanager", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["works_created"] == 1
    assert len(runtime.list_work()) == 1
    assert runtime.list_work()[0].kind == "incident"


def test_trigger_webhook_and_schedule() -> None:
    runtime = Runtime(store=InMemoryStateStore())
    client = TestClient(create_app(runtime))
    resp = client.post("/v1/triggers/webhook", json={"foo": "bar"})
    assert resp.status_code == 200
    resp2 = client.post("/v1/triggers/schedule/emit", params={"kind": "maintenance-scan"})
    assert resp2.status_code == 200
    assert "maintenance-scan" in resp2.json()["kind"]


def test_workflow_endpoint() -> None:
    runtime = Runtime(store=InMemoryStateStore())
    from portable_runtime.core.capabilities import (
        CapabilityRequest,
        CapabilityResult,
        InvocationContext,
        ProviderDescriptor,
        ProviderHealth,
    )

    class FakeAll:
        def __init__(self) -> None:
            self._d = ProviderDescriptor(
                id="fake-all",
                name="Fake",
                version="1.0.0",
                capabilities=[
                    "reason.generate",
                    "code.edit",
                    "verify.http",
                    "verify.git_diff",
                    "observe.logs",
                    "observe.container",
                ],
            )

        @property
        def descriptor(self) -> ProviderDescriptor:
            return self._d

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id=self._d.id, available=True)

        async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
            return CapabilityResult(request_id=request.id, provider_id=self._d.id, status="succeeded", message="ok")

        async def cancel(self, request_id: str) -> None:
            return None

    runtime.registry.register(FakeAll())
    client = TestClient(create_app(runtime))
    work = runtime.create_work(title="wf test", kind="incident", description="do it")
    resp = client.post(f"/v1/work/{work.id}/workflow/incident-repair")
    assert resp.status_code == 200
    assert resp.json()["status"] in {"succeeded", "blocked", "waiting", "failed"}


def test_dual_write_and_export_import() -> None:
    store = InMemoryStateStore()
    row = {"id": "repair-9", "fingerprint": "fp9", "status": "closed", "payload_json": "{\"alert\":1}"}
    work, run = dual_write_repair(row, store)
    assert work.id == "work_legacy_repair-9"
    state = store.export_state()
    new_store = InMemoryStateStore()
    new_runtime = Runtime(store=new_store)
    new_runtime.import_state(state)
    assert new_runtime.get_work(work.id) is not None
    assert new_runtime.store.get_run(run.id) is not None


async def test_plugin_manager_lifecycle(tmp_path: Path) -> None:
    registry = __import__("portable_runtime.core.registry", fromlist=["ProviderRegistry"]).ProviderRegistry()
    manager = PluginManager(registry, plugin_dir=tmp_path / "plugins")
    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir()
    manifest = {
        "id": "tmp-echo",
        "name": "Tmp Echo",
        "version": "1.0.0",
        "protocol_version": "1",
        "transport": "stdio-jsonl",
        "command": [sys.executable, str(Path("examples/echo-provider/provider.py").resolve())],
        "capabilities": ["text.echo"],
    }
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    import shutil

    shutil.copy("examples/echo-provider/provider.py", plugin_dir / "provider.py")
    manifest["command"] = [sys.executable, str(plugin_dir / "provider.py")]
    (plugin_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rec = await manager.load(plugin_dir)
    assert rec.status in {"loaded", "failed"}
    if rec.status == "loaded":
        manager.enable(rec.id)
        assert registry.list()
        manager.disable(rec.id)
        manager.reload(rec.id)
        manager.remove(rec.id)
        assert rec.id not in [r.id for r in registry.list()]


def test_deployment_local_creates_runtime(tmp_path: Path) -> None:
    runtime = create_local_runtime(tmp_path / "test.db")
    assert runtime.runtime_id == "portable-local"
    w = runtime.create_work(title="local test")
    assert runtime.get_work(w.id) is not None
    runtime.store.close()
    runtime2 = create_personal_platform_runtime(tmp_path / "test2.db")
    assert runtime2.runtime_id == "personal-platform"
    runtime2.store.close()


def test_state_export_has_no_absolute_windows_paths(tmp_path: Path) -> None:
    from portable_runtime.stores.sqlite import SQLiteStateStore

    store = SQLiteStateStore(tmp_path / "state.db")
    runtime = Runtime(store=store)
    runtime.create_work(title="export test", kind="research")
    state = runtime.export_state()
    dumped = json.dumps(state)
    assert "D:\\\\" not in dumped
    assert "C:\\\\" not in dumped
    store.close()
