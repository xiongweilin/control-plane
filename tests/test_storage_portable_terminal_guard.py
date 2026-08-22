from __future__ import annotations

from pathlib import Path

from control_plane.storage import Store
from portable_runtime.core.models import Run, Work
from portable_runtime.records.models import EvidenceArtifact
from portable_runtime.stores.memory import InMemoryStateStore


def test_legacy_terminal_projection_does_not_close_canonical_pair(tmp_path: Path) -> None:
    legacy = Store(tmp_path / "legacy.db")
    portable = InMemoryStateStore()
    try:
        work = Work(id="work_legacy_r1", title="repair", status="waiting")
        run = Run(id="run_legacy_r1", work_id=work.id, status="waiting")
        portable.save_work(work)
        portable.save_run(run)
        legacy.attach_portable_store(portable, enable_read=True)
        legacy.create_repair("r1", "fp-r1", "{}")

        # The legacy projection may close its own row, but it must not invent
        # the canonical terminal Work/Run states without typed authority.
        legacy.set_repair_status("r1", "closed", result="legacy projection")

        assert legacy.get_repair("r1")["status"] == "closed"
        assert portable.get_work(work.id).status == "waiting"
        assert portable.get_run(run.id).status == "waiting"
    finally:
        legacy.close()


def test_legacy_terminal_projection_preserves_authority_committed_pair(tmp_path: Path) -> None:
    legacy = Store(tmp_path / "legacy.db")
    portable = InMemoryStateStore()
    try:
        work = Work(
            id="work_legacy_r2",
            title="repair",
            status="completed",
            metadata={"_completion_proof_refs": ["proof-r2"]},
        )
        run = Run(
            id="run_legacy_r2",
            work_id=work.id,
            status="succeeded",
            metadata={"_completion_proof_refs": ["proof-r2"]},
        )
        portable.save_record(
            EvidenceArtifact(
                id="proof-r2",
                kind="closed-verification",
                metadata={
                    "work_id": work.id,
                    "run_id": run.id,
                    "verification_result": {
                        "result": "pass",
                        "work_id": work.id,
                        "run_id": run.id,
                    }
                },
            )
        )
        with portable.terminal_completion(["proof-r2"]):
            portable.save_work(work)
            portable.save_run(run)
        legacy.attach_portable_store(portable, enable_read=True)
        legacy.create_repair("r2", "fp-r2", "{}")

        # A later legacy update must not rewrite or strip proof-bearing
        # canonical terminal records.
        legacy.set_repair_status("r2", "failed", error="legacy retry marker")

        current_work = portable.get_work(work.id)
        current_run = portable.get_run(run.id)
        assert current_work is not None and current_work.status == "completed"
        assert current_run is not None and current_run.status == "succeeded"
        assert current_work.metadata["_completion_proof_refs"] == ["proof-r2"]
        assert current_run.metadata["_completion_proof_refs"] == ["proof-r2"]
    finally:
        legacy.close()
