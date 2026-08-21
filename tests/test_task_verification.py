from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.notify import Notifier
from control_plane.portable_authority import PortableRuntimeAuthority
from control_plane.service import RepairService
from control_plane.storage import Store
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


def _service(tmp_path: Path) -> tuple[RepairService, Store, Runtime]:
    data_dir = tmp_path / "data"
    config = replace(
        ControlPlaneConfig(),
        data_dir=data_dir,
        patch_dir=data_dir / "patches",
        evidence_dir=data_dir / "evidence",
        state_db=data_dir / "control-plane.db",
        agent_session_dir=data_dir / "agent-sessions",
        notification_enabled=False,
    )
    store = Store(config.state_db)
    runtime = Runtime(store=InMemoryStateStore(), registry=ProviderRegistry())
    service = RepairService(
        config,
        store,
        Budget(store, 10, 2),
        agent=object(),
        notifier=Notifier(config),
        portable_runtime=runtime,
    )
    return service, store, runtime


def _admit_task(service: RepairService, store: Store, task_id: str) -> None:
    payload = {"kind": "task", "prompt": "produce a report", "repo": "D:/agent/control-plane"}
    payload_json = json.dumps(payload)
    store.create_repair(task_id, f"task:{task_id}", payload_json, 1)
    assert service.portable_authority is not None
    service.portable_authority.ensure_repair_projection(
        repair_id=task_id,
        fingerprint=f"task:{task_id}",
        payload_json=payload_json,
        attempt=1,
    )


def test_task_exit_zero_is_not_verification_without_artifact(tmp_path: Path) -> None:
    service, store, runtime = _service(tmp_path)
    task_id = "task-no-artifact"
    _admit_task(service, store, task_id)

    verified, reason, refs = service._verify_task_postcondition(task_id)

    assert verified is False
    assert "artifact" in reason
    assert refs == []
    work = runtime.store.get_work(f"work_legacy_{task_id}")
    assert work is not None
    assert work.metadata.get("verified") is not True
    store.close()


def test_task_result_artifact_must_match_canonical_run(tmp_path: Path) -> None:
    service, store, runtime = _service(tmp_path)
    task_id = "task-associated-artifact"
    _admit_task(service, store, task_id)
    run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert run is not None
    session_dir = service.config.agent_session_dir
    session_dir.mkdir(parents=True)
    path = session_dir / f"req-{task_id}-proof.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "control_plane_meta",
                "run_id": run.id,
                "request_id": f"req-{task_id}-proof",
            }
        )
        + "\n"
        + json.dumps({"type": "result", "text": "report produced"})
        + "\n",
        encoding="utf-8",
    )

    verified, reason, refs = service._verify_task_postcondition(task_id)

    assert verified is True
    assert "readable" in reason
    assert len(refs) == 1
    artifact = runtime.store.get_artifact(refs[0])
    assert artifact is not None
    assert artifact.created_by_run_id == run.id
    assert artifact.id in runtime.store.get_work(f"work_legacy_{task_id}").artifact_refs
    store.close()

