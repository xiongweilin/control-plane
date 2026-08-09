from __future__ import annotations

import sqlite3

from control_plane.storage import Store


def _create_old_schema(path) -> None:
    """A pre-batch2 repairs table without the lease/error columns."""
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
        """
    )
    conn.commit()
    conn.close()


def test_upgrade_from_old_schema_adds_new_columns(tmp_path) -> None:
    db = tmp_path / "old.db"
    _create_old_schema(db)
    store = Store(db)
    store.create_repair("repair-1", "fp-1", "{}")
    row = store.get_repair("repair-1")
    assert row["lease_owner"] == ""
    assert row["lease_expires_at"] is None
    assert row["error_class"] == ""
    assert row["original_error"] == ""
    assert row["recovery_error"] == ""
    assert row["timeout_kind"] == ""
    # the new columns are writable through the normal path
    store.set_repair_status(
        "repair-1",
        "failed",
        error="boom",
        error_class="deterministic",
        timeout_kind="exec",
    )
    row = store.get_repair("repair-1")
    assert row["error_class"] == "deterministic"
    assert row["timeout_kind"] == "exec"
    store.close()


def test_repeated_init_is_idempotent(tmp_path) -> None:
    db = tmp_path / "cp.db"
    Store(db).close()
    Store(db).close()  # second init over the already-migrated schema
    store = Store(db)
    store.create_repair("repair-2", "fp-2", "{}")
    store.close()


def test_migration_preserves_existing_rows(tmp_path) -> None:
    db = tmp_path / "old.db"
    _create_old_schema(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO repairs(id, fingerprint, payload_json, status, attempt, created_at, updated_at) "
        "VALUES ('repair-keep', 'fp-keep', '{}', 'failed', 1, 1000, 1000)",
    )
    conn.commit()
    conn.close()
    store = Store(db)
    row = store.get_repair("repair-keep")
    assert row["status"] == "failed"
    assert row["attempt"] == 1
    assert row["error_class"] == ""
    store.close()


def test_downgrade_old_code_reads_new_schema(tmp_path) -> None:
    """A pre-migration reader must still work against the migrated database."""
    db = tmp_path / "new.db"
    store = Store(db)
    store.create_repair("repair-3", "fp-3", "{}")
    store.set_repair_status("repair-3", "failed", error="boom")
    store.close()

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, fingerprint, status, attempt, error FROM repairs"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("repair-3", "fp-3", "failed", 1, "boom")
    # an old-style INSERT without the new columns still succeeds (defaults apply)
    conn.execute(
        "INSERT INTO repairs(id, fingerprint, payload_json, status, attempt, created_at, updated_at) "
        "VALUES ('repair-4', 'fp-4', '{}', 'queued', 1, 2000, 2000)",
    )
    conn.commit()
    conn.close()


def test_upgrade_runs_against_partial_new_schema(tmp_path) -> None:
    """Migration is safe when only some of the new columns already exist."""
    db = tmp_path / "partial.db"
    _create_old_schema(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE repairs ADD COLUMN error_class TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.close()
    store = Store(db)
    store.create_repair("repair-5", "fp-5", "{}")
    row = store.get_repair("repair-5")
    assert row["error_class"] == ""
    assert row["lease_owner"] == ""
    assert row["timeout_kind"] == ""
    store.close()
