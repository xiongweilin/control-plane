from __future__ import annotations

import sqlite3
import time

from control_plane.storage import Store


def test_run_records_roundtrip(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.start_run_record("run-1", 111, "host-a", "3.14")
    row = store.get_run_record("run-1")
    assert row["pid"] == 111
    assert row["status"] == "running"
    store.stop_run_record("run-1")
    assert store.get_run_record("run-1")["status"] == "stopped"
    assert store.list_run_records()[0]["run_id"] == "run-1"
    store.close()


def test_audit_entries(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.add_audit_entry(
        "aud-1",
        run_id="run-9",
        repair_id="repair-1",
        kind="command",
        argv_json='["git", "push"]',
        exit_code=0,
        duration_ms=12,
    )
    store.add_audit_entry(
        "aud-2",
        run_id="run-9",
        repair_id="repair-1",
        kind="agent",
        argv_json="[]",
        exit_code=124,
        duration_ms=300,
        truncated=True,
        error_class="retryable",
    )
    rows = store.list_audit("repair-1")
    assert len(rows) == 2
    assert {row["truncated"] for row in rows} == {0, 1}
    assert {row["error_class"] for row in rows} == {"retryable", ""}
    assert {row["kind"] for row in store.list_audit()} == {"command", "agent"}
    store.close()


def test_lease_acquire_renew_release(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.create_repair("repair-1", "fp-1", "{}")
    assert store.acquire_lease("fp-1", "run-a", "repair-1", 100) is True
    assert store.acquire_lease("fp-1", "run-b", "repair-2", 100) is False
    assert store.get_lease_owner("fp-1")["owner_run_id"] == "run-a"
    assert store.renew_lease("fp-1", "run-a", 500) is True
    assert store.renew_lease("fp-1", "run-b", 500) is False
    assert store.get_lease_owner("fp-1")["expires_at"] > int(time.time()) + 400
    store.release_lease("fp-1", "run-a")
    assert store.get_lease_owner("fp-1") is None
    assert store.acquire_lease("fp-1", "run-b", "repair-2", 100) is True
    store.close()


def test_lease_expiry_allows_reacquire(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.create_repair("repair-1", "fp-x", "{}")
    assert store.acquire_lease("fp-x", "run-a", "repair-1", 1) is True
    assert store.acquire_lease("fp-x", "run-b", "repair-2", 100) is False
    # expired lease no longer blocks
    store._connection.execute("UPDATE leases SET expires_at=0 WHERE fingerprint='fp-x'")
    assert store.acquire_lease("fp-x", "run-b", "repair-2", 100) is True
    store.close()


def test_repair_new_columns_populated(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.create_repair("repair-1", "fp-1", "{}", attempt=2)
    store.set_repair_status(
        "repair-1",
        "failed",
        error_class="deterministic",
        timeout_kind="exec",
        original_error="first",
        recovery_error="second",
        lease_owner="run-a",
        lease_expires_at=123,
        finished_at=int(time.time()),
    )
    row = store.get_repair("repair-1")
    assert row["error_class"] == "deterministic"
    assert row["timeout_kind"] == "exec"
    assert row["original_error"] == "first"
    assert row["recovery_error"] == "second"
    assert row["lease_owner"] == "run-a"
    store.close()


def test_schema_migration_adds_columns(tmp_path) -> None:
    """A pre-existing database without the new columns must be upgraded in place."""
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
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
        INSERT INTO repairs(id, fingerprint, payload_json, status, created_at, updated_at)
        VALUES ('repair-old', 'fp', '{}', 'failed', 1, 1);
        """
    )
    connection.close()

    store = Store(path)
    row = store.get_repair("repair-old")
    assert row["error_class"] == ""
    assert row["timeout_kind"] == ""
    assert row["original_error"] == ""
    store.set_repair_status("repair-old", "failed", error_class="comm")
    assert store.get_repair("repair-old")["error_class"] == "comm"
    store.close()


def test_check_writable(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    assert store.check_writable() is True
    assert store.get_setting("health:last_ready") != ""
    store.close()


def test_list_repairs_in_states(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    store.create_repair("r1", "fp-a", "{}")
    store.create_repair("r2", "fp-b", "{}")
    store.set_repair_status("r1", "needs_approval")
    store.set_repair_status("r2", "interrupted")
    pending = store.list_repairs_in_states(("needs_approval", "recovering"))
    assert [row["id"] for row in pending] == ["r1"]
    store.close()
