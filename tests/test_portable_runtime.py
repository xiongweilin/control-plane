from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from portable_runtime.api.http import create_app
from portable_runtime.compat.legacy_control_plane import import_legacy_repair
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext
from portable_runtime.core.runtime import Runtime
from portable_runtime.plugin import provider
from portable_runtime.plugin.conformance import check_provider
from portable_runtime.plugin.loader import load_manifest, validate_manifest
from portable_runtime.providers.fake import EchoProvider, FailingProvider
from portable_runtime.providers.stdio import StdioJsonlProvider
from portable_runtime.stores.filesystem import FilesystemArtifactStore
from portable_runtime.stores.memory import InMemoryStateStore
from portable_runtime.stores.sqlite import SQLiteStateStore


def test_zero_provider_runtime_can_create_and_export_work() -> None:
    runtime = Runtime()
    work = runtime.create_work(title="portable", description="no provider required")
    assert runtime.get_work(work.id) == work
    assert runtime.export_state()["work"][0]["id"] == work.id


def test_provider_replacement_routes_without_changing_work() -> None:
    import asyncio

    async def scenario() -> None:
        runtime = Runtime()
        first = EchoProvider("first", priority=10)
        second = EchoProvider("second", priority=1)
        runtime.registry.register(first)
        runtime.registry.register(second)
        work = runtime.create_work(title="route")
        result = await runtime.run_capability(work.id, "text.echo", instruction="one")
        assert result.provider_id == "first"
        runtime.registry.disable("first")
        result = await runtime.run_capability(work.id, "text.echo", instruction="two")
        assert result.provider_id == "second"
        assert runtime.get_work(work.id) is not None
        exported = runtime.export_state()
        assert exported["action"]
        assert exported["outcome"]

    asyncio.run(scenario())


def test_provider_conformance_isolated_from_runtime_state() -> None:
    import asyncio

    async def scenario() -> None:
        assert await check_provider(EchoProvider()) == []
        assert await check_provider(FailingProvider())

    asyncio.run(scenario())


def test_provider_decorator_supports_minimal_one_argument_handler() -> None:
    @provider(id="upper", version="1", capabilities=["text.upper"])
    async def upper(request: CapabilityRequest) -> str:
        return (request.instruction or "").upper()

    import asyncio

    result = asyncio.run(
        upper.invoke(
            CapabilityRequest(id="req", capability="text.upper", instruction="hello"),
            InvocationContext(runtime_id="test"),
        )
    )
    assert result.message == "HELLO"


def test_sqlite_state_export_import_preserves_ids(tmp_path: Path) -> None:
    source = SQLiteStateStore(tmp_path / "source.db")
    runtime = Runtime(store=source)
    work = runtime.create_work(title="migrate", kind="research")
    state = runtime.export_state()
    target = SQLiteStateStore(tmp_path / "target.db")
    restored = Runtime(store=target)
    restored.import_state(state)
    assert restored.get_work(work.id) is not None
    assert restored.get_work(work.id).kind == "research"  # type: ignore[union-attr]
    source.close()
    target.close()


def test_legacy_repair_adapter_keeps_stable_mapping() -> None:
    store = InMemoryStateStore()
    work, run = import_legacy_repair(
        {"id": "repair-1", "fingerprint": "fp", "status": "closed", "payload_json": "{}"},
        store,
    )
    assert work.id == "work_legacy_repair-1"
    assert run.id == "run_legacy_repair-1"
    assert run.status == "succeeded"


def test_filesystem_artifact_store_is_content_addressed_and_scoped(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    uri = store.put(b"hello", media_type="text/plain")
    assert store.get(uri) == b"hello"
    try:
        store.get((tmp_path / "outside").as_uri())
    except ValueError as exc:
        assert "invalid" in str(exc) or "outside" in str(exc)
    else:
        raise AssertionError("out-of-scope artifact URI was accepted")


def test_http_surface_works_without_provider() -> None:
    client = TestClient(create_app(Runtime()))
    response = client.post("/v1/work", json={"title": "api"})
    assert response.status_code == 200
    work_id = response.json()["id"]
    assert client.get(f"/v1/work/{work_id}").json()["id"] == work_id
    result = client.post(f"/v1/work/{work_id}/run", json={"capability": "text.echo"})
    assert result.status_code == 200
    assert result.json()["status"] == "unavailable"


def test_manifest_and_stdio_provider(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    provider_path = tmp_path / "provider.py"
    provider_path.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); "
        "print(json.dumps({'type':'result','request_id':r['id'],"
        "'status':'succeeded','message':r.get('instruction')}),flush=True)\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            {
                "id": "tmp",
                "name": "tmp",
                "version": "1",
                "command": [sys.executable, str(provider_path)],
                "capabilities": ["text.echo"],
            }
        ),
        encoding="utf-8",
    )
    assert validate_manifest(manifest_path) == []
    provider = StdioJsonlProvider(load_manifest(manifest_path), working_directory=manifest_path.parent)

    import asyncio

    async def scenario() -> None:
        health = await provider.health()
        assert health.available
        result = await provider.invoke(
            CapabilityRequest(id="req", capability="text.echo", instruction="hi"),
            context=InvocationContext(runtime_id="test"),
        )
        assert result.status == "succeeded"
        assert result.message == "hi"

    asyncio.run(scenario())


def test_core_import_boundary_script() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_portable_core_imports.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
