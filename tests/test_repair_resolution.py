from __future__ import annotations

import json
import sqlite3

import pytest

from control_plane.repair_resolution import ResolutionKind, RestorationStatus
from control_plane.storage import Store


def _create_legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE repairs (
            id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            agent_call_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            finished_at INTEGER,
            result TEXT,
            error TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO repairs(
            id, fingerprint, payload_json, status, attempt, created_at, updated_at
        ) VALUES ('legacy-closed', 'fp-closed', '{}', 'closed', 1, 1000, 1000);
        INSERT INTO repairs(
            id, fingerprint, payload_json, status, attempt, created_at, updated_at
        ) VALUES ('legacy-verified', 'fp-verified', '{}', 'verified', 1, 1000, 1000);
        """
    )
    conn.commit()
    conn.close()


def test_legacy_rows_gain_unknown_unresolved_without_retrospective_knowledge(tmp_path) -> None:
    db = tmp_path / "legacy.db"
    _create_legacy_db(db)

    store = Store(db)
    for repair_id in ("legacy-closed", "legacy-verified"):
        row = store.get_repair(repair_id)
        assert row is not None
        assert row["restoration_status"] == RestorationStatus.UNKNOWN.value
        assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
        assert json.loads(row["restoration_proof_refs_json"]) == []
        assert row["resolution_updated_at"] is None
    store.close()


def test_new_repair_starts_unverified_and_unresolved(tmp_path) -> None:
    store = Store(tmp_path / "new.db")
    store.create_repair("repair-1", "fp-1", "{}")
    row = store.get_repair("repair-1")
    assert row is not None
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    assert json.loads(row["restoration_proof_refs_json"]) == []
    assert row["resolution_updated_at"] is None
    store.close()


def test_set_repair_status_cannot_write_resolution_axes(tmp_path) -> None:
    store = Store(tmp_path / "status.db")
    store.create_repair("repair-2", "fp-2", "{}")
    before = store.get_repair("repair-2")
    assert before is not None
    axes_before = (
        before["restoration_status"],
        before["resolution_kind"],
        before["restoration_proof_refs_json"],
        before["resolution_updated_at"],
    )

    store.set_repair_status("repair-2", "diagnosing", result="started")
    after = store.get_repair("repair-2")
    assert after is not None
    assert (
        after["restoration_status"],
        after["resolution_kind"],
        after["restoration_proof_refs_json"],
        after["resolution_updated_at"],
    ) == axes_before

    with pytest.raises(ValueError, match="Unsupported repair field"):
        store.set_repair_status("repair-2", "closed", restoration_status="verified")
    store.close()


def test_set_repair_resolution_does_not_change_repair_state(tmp_path) -> None:
    store = Store(tmp_path / "resolution.db")
    store.create_repair("repair-3", "fp-3", "{}")
    store.set_repair_resolution(
        "repair-3",
        resolution_kind=ResolutionKind.REJECTED,
        restoration_status=RestorationStatus.UNVERIFIED,
        basis_refs=("basis-1",),
    )
    row = store.get_repair("repair-3")
    assert row is not None
    assert row["status"] == "queued"
    assert row["resolution_kind"] == ResolutionKind.REJECTED.value
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    store.close()


@pytest.mark.parametrize(
    "resolution_kind",
    [ResolutionKind.RESTORED, ResolutionKind.NO_ACTION_REQUIRED],
)
def test_positive_resolution_fails_closed_without_verified_proof(
    tmp_path, resolution_kind: ResolutionKind
) -> None:
    store = Store(tmp_path / f"{resolution_kind.value}.db")
    store.create_repair("repair-4", "fp-4", "{}")

    with pytest.raises(ValueError, match="restoration_status=verified"):
        store.set_repair_resolution(
            "repair-4",
            resolution_kind=resolution_kind,
            restoration_status=RestorationStatus.UNVERIFIED,
            basis_refs=("basis-1",),
            proof_refs=("proof-1",),
        )
    with pytest.raises(ValueError, match="restoration_status=verified requires restoration proof refs"):
        store.set_repair_resolution(
            "repair-4",
            resolution_kind=resolution_kind,
            restoration_status=RestorationStatus.VERIFIED,
            basis_refs=("basis-1",),
        )

    row = store.get_repair("repair-4")
    assert row is not None
    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    store.close()


@pytest.mark.parametrize(
    ("resolution_kind", "restoration_status"),
    [
        (ResolutionKind.REJECTED, RestorationStatus.VERIFIED),
        (ResolutionKind.ESCALATED, RestorationStatus.FAILED),
    ],
)
def test_evidence_bearing_restoration_requires_proof_refs(
    tmp_path, resolution_kind: ResolutionKind, restoration_status: RestorationStatus
) -> None:
    store = Store(tmp_path / f"{resolution_kind.value}-{restoration_status.value}.db")
    store.create_repair("repair-evidence", "fp-evidence", "{}")
    with pytest.raises(ValueError, match="requires restoration proof refs"):
        store.set_repair_resolution(
            "repair-evidence",
            resolution_kind=resolution_kind,
            restoration_status=restoration_status,
            basis_refs=("basis-1",),
        )
    row = store.get_repair("repair-evidence")
    assert row is not None
    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value
    store.close()


def test_non_restored_disposition_can_carry_verified_restoration_with_proof(tmp_path) -> None:
    store = Store(tmp_path / "rejected-verified.db")
    store.create_repair("repair-independent", "fp-independent", "{}")
    store.set_repair_resolution(
        "repair-independent",
        resolution_kind=ResolutionKind.REJECTED,
        restoration_status=RestorationStatus.VERIFIED,
        basis_refs=("basis-1",),
        proof_refs=("proof-independent",),
    )
    row = store.get_repair("repair-independent")
    assert row is not None
    assert row["resolution_kind"] == ResolutionKind.REJECTED.value
    assert row["restoration_status"] == RestorationStatus.VERIFIED.value
    assert json.loads(row["restoration_proof_refs_json"]) == ["proof-independent"]
    store.close()


def test_restored_verified_with_proof_refs_persists(tmp_path) -> None:
    store = Store(tmp_path / "restored.db")
    store.create_repair("repair-5", "fp-5", "{}")
    store.set_repair_resolution(
        "repair-5",
        resolution_kind=ResolutionKind.RESTORED,
        restoration_status=RestorationStatus.VERIFIED,
        basis_refs=("basis-1",),
        proof_refs=("proof-a", "proof-a", "proof-b"),
    )
    row = store.get_repair("repair-5")
    assert row is not None
    assert row["resolution_kind"] == ResolutionKind.RESTORED.value
    assert row["restoration_status"] == RestorationStatus.VERIFIED.value
    assert json.loads(row["restoration_proof_refs_json"]) == ["proof-a", "proof-b"]
    assert row["resolution_updated_at"] is not None
    store.close()


def test_reopen_initialization_is_idempotent_and_preserves_resolution(tmp_path) -> None:
    db = tmp_path / "reopen.db"
    store = Store(db)
    store.create_repair("repair-6", "fp-6", "{}")
    store.set_repair_resolution(
        "repair-6",
        resolution_kind=ResolutionKind.REJECTED,
        restoration_status=RestorationStatus.UNKNOWN,
        basis_refs=("basis-1",),
        proof_refs=("observation-1",),
    )
    stored = store.get_repair("repair-6")
    assert stored is not None
    expected = dict(stored)
    store.close()

    Store(db).close()
    reopened = Store(db)
    row = reopened.get_repair("repair-6")
    assert row is not None
    for field in (
        "status",
        "restoration_status",
        "resolution_kind",
        "restoration_proof_refs_json",
        "resolution_updated_at",
    ):
        assert row[field] == expected[field]
    reopened.close()


def test_resolution_write_never_touches_portable_store(tmp_path) -> None:
    class ExplodingPortableStore:
        def __getattr__(self, name: str):
            raise AssertionError(
                f"portable store must not be touched by C2 resolution write: {name}"
            )

    store = Store(tmp_path / "portable-boundary.db")
    store.create_repair("repair-7", "fp-7", "{}")
    store._portable_store = ExplodingPortableStore()
    store.set_repair_resolution(
        "repair-7",
        resolution_kind=ResolutionKind.REJECTED,
        restoration_status=RestorationStatus.UNVERIFIED,
        basis_refs=("basis-1",),
    )
    row = store.get_repair("repair-7")
    assert row is not None
    assert row["status"] == "queued"
    store.close()
