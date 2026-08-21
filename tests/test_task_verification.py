from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from control_plane.budget import Budget
from control_plane.codex_runner import CodexSessionResult
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


def _admit_task(
    service: RepairService,
    store: Store,
    task_id: str,
    *,
    prompt: str = "produce a report",
) -> None:
    payload = {"kind": "task", "prompt": prompt, "repo": "D:/agent/control-plane"}
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


def test_task_delivery_proof_does_not_verify_objective(tmp_path: Path) -> None:
    service, store, runtime = _service(tmp_path)
    task_id = "task-delivery-only"
    _admit_task(service, store, task_id)
    authority = service.portable_authority
    assert authority is not None

    refs = authority.record_task_delivery_verification(
        task_id,
        summary="result artifact was produced and bound to the canonical run",
        evidence_refs=["artifact-task-delivery"],
    )

    assert len(refs) == 3
    work = runtime.store.get_work(f"work_legacy_{task_id}")
    run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert work is not None
    assert run is not None
    assert work.status == "waiting"
    assert run.status == "waiting"
    assert work.metadata["delivery_verified"] is True
    assert work.metadata["verification_scope"] == "delivery"
    assert work.metadata["objective_status"] == "unverified"
    assert work.metadata["objective_verification_status"] == "unavailable"
    assert work.metadata.get("verified") is not True
    assert run.metadata["delivery_verified"] is True
    assert run.metadata["verification_scope"] == "delivery"
    assert run.metadata["objective_status"] == "unverified"
    assert run.metadata.get("verified") is not True
    store.close()


async def test_run_task_keeps_delivery_only_result_recoverable(tmp_path: Path, monkeypatch) -> None:
    service, store, runtime = _service(tmp_path)
    task_id = "task-run-delivery-only"
    _admit_task(service, store, task_id)
    run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert run is not None
    session_dir = service.config.agent_session_dir
    session_dir.mkdir(parents=True)
    (session_dir / f"req-{task_id}-proof.jsonl").write_text(
        json.dumps(
            {
                "type": "control_plane_meta",
                "run_id": run.id,
                "request_id": f"req-{task_id}-proof",
            }
        )
        + "\n"
        + json.dumps({"type": "result", "text": "the agent reported its result"})
        + "\n",
        encoding="utf-8",
    )

    async def fake_invoke(*, repair_id: str, repo: str, prompt: str) -> CodexSessionResult:
        return CodexSessionResult(exit_code=0, last_message="agent summary")

    monkeypatch.setattr(service, "_invoke_codex_via_capability", fake_invoke)
    await service._run_task(task_id, "D:/agent/control-plane", "fix the bug")

    row = store.get_repair(task_id)
    assert row is not None
    assert row["status"] == "recovering"
    work = runtime.store.get_work(f"work_legacy_{task_id}")
    canonical_run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert work is not None
    assert canonical_run is not None
    assert work.status == "waiting"
    assert canonical_run.status == "waiting"
    assert work.metadata["delivery_verified"] is True
    assert work.metadata["objective_status"] == "unverified"
    assert work.metadata.get("verified") is not True
    store.close()


@pytest.mark.asyncio
async def test_transcript_delivery_does_not_claim_arbitrary_task_objective(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run-associated transcript proves delivery, not natural-language success.

    The provider deliberately reports that it could not complete the requested
    objective while still exiting successfully.  The artifact verifier must
    record the narrower delivery fact and leave the canonical task waiting for
    a task-specific objective verifier.
    """

    service, store, runtime = _service(tmp_path)
    task_id = "task-delivery-only"
    prompt = "Fix bug X and prove that the complete test suite passes."
    _admit_task(service, store, task_id, prompt=prompt)
    run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert run is not None

    async def fake_invoke(*, repair_id: str, repo: str, prompt: str, capability: str = "code.edit") -> CodexSessionResult:
        del repo, prompt, capability
        session_dir = service.config.agent_session_dir
        session_dir.mkdir(parents=True, exist_ok=True)
        transcript = session_dir / f"req-{repair_id}-proof.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "control_plane_meta",
                    "run_id": run.id,
                    "request_id": repair_id,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "result",
                    "text": "I could not fix bug X; the requested tests were not run.",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return CodexSessionResult(
            exit_code=0,
            last_message="I could not complete the requested objective.",
        )

    monkeypatch.setattr(service, "_invoke_codex_via_capability", fake_invoke)

    await service._run_task(task_id, "D:/agent/control-plane", prompt)

    row = store.get_repair(task_id)
    work = runtime.store.get_work(f"work_legacy_{task_id}")
    persisted_run = runtime.store.get_run(f"run_legacy_{task_id}")
    assert row is not None
    assert work is not None
    assert persisted_run is not None

    assert row["status"] == "recovering"
    assert work.status == "waiting"
    assert persisted_run.status == "waiting"
    for metadata in (work.metadata, persisted_run.metadata):
        assert metadata.get("delivery_verified") is True
        assert metadata.get("verification_scope") == "delivery"
        assert metadata.get("objective_status") == "unverified"
        assert metadata.get("verified") is not True
        assert metadata.get("verification_status") != "passed"
    store.close()
