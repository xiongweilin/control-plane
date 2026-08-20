from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from portable_runtime.core.models import Work as PortableWork
else:
    try:
        from portable_runtime.core.models import Work as PortableWork  # pragma: no cover
    except ImportError:  # pragma: no cover
        PortableWork = Any  # type: ignore[assignment]


class Store:
    """SQLite persistence for alerts, repairs, actions, approvals, candidates and budget."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Portable read switch (S31-C, S52): optional delegated StateStore (Step C)
        self._portable_store: Any | None = None
        self._portable_read_enabled: bool = False
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;

                CREATE TABLE IF NOT EXISTS alerts (
                    fingerprint TEXT PRIMARY KEY,
                    alertname TEXT NOT NULL,
                    instance TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT '',
                    container TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    resolved_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS repairs (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    agent_call_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at INTEGER,
                    error_class TEXT NOT NULL DEFAULT '',
                    original_error TEXT NOT NULL DEFAULT '',
                    recovery_error TEXT NOT NULL DEFAULT '',
                    timeout_kind TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    result TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_repairs_fingerprint ON repairs(fingerprint);
                CREATE INDEX IF NOT EXISTS idx_repairs_status ON repairs(status);

                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    repair_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    target TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    status TEXT NOT NULL,
                    output TEXT,
                    created_at INTEGER NOT NULL,
                    finished_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_actions_repair ON actions(repair_id);

                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    ref_kind TEXT NOT NULL,
                    ref_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    decided_by TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    UNIQUE(ref_kind, ref_id, action)
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    pattern TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    tool_sequence TEXT NOT NULL,
                    verifier_ids TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trial_deadline INTEGER NOT NULL,
                    default_disposition TEXT NOT NULL,
                    times_supported INTEGER NOT NULL DEFAULT 0,
                    failure_signature TEXT NOT NULL DEFAULT '',
                    reopen_conditions TEXT NOT NULL DEFAULT '',
                    source_repair_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_pattern ON candidates(pattern);

                CREATE TABLE IF NOT EXISTS playbooks (
                    candidate_id TEXT PRIMARY KEY,
                    promoted_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS budget_usage (
                    date TEXT PRIMARY KEY,
                    calls INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_records (
                    run_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    hostname TEXT NOT NULL DEFAULT '',
                    python_version TEXT NOT NULL DEFAULT '',
                    started_at INTEGER NOT NULL,
                    stopped_at INTEGER,
                    status TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS command_audit (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL DEFAULT '',
                    repair_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    argv_json TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    truncated INTEGER NOT NULL DEFAULT 0,
                    error_class TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_repair ON command_audit(repair_id);
                CREATE INDEX IF NOT EXISTS idx_audit_run ON command_audit(run_id);

                CREATE TABLE IF NOT EXISTS leases (
                    fingerprint TEXT PRIMARY KEY,
                    owner_run_id TEXT NOT NULL,
                    repair_id TEXT NOT NULL,
                    acquired_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
        self._ensure_column("repairs", "lease_owner", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("repairs", "lease_expires_at", "INTEGER")
        self._ensure_column("repairs", "error_class", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("repairs", "original_error", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("repairs", "recovery_error", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("repairs", "timeout_kind", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Add a column to an existing table when it is missing (SQLite has no IF NOT EXISTS)."""
        with self._lock:
            existing = {
                row["name"]
                for row in self._connection.execute(f"PRAGMA table_info({table})")  # noqa: S608
            }
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"  # noqa: S608
                )

    # ---- alerts ----

    def upsert_alert(
        self,
        fingerprint: str,
        alertname: str,
        instance: str,
        project: str,
        container: str,
        status: str,
        starts_at: int,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO alerts(fingerprint, alertname, instance, project, container,
                                   status, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    alertname=excluded.alertname,
                    instance=excluded.instance,
                    project=excluded.project,
                    container=excluded.container,
                    status=excluded.status,
                    last_seen=excluded.last_seen,
                    resolved_at=NULL
                """,
                (
                    fingerprint,
                    alertname,
                    instance,
                    project,
                    container,
                    status,
                    starts_at,
                    now,
                ),
            )

    def mark_alert_resolved(self, fingerprint: str, ends_at: int | None = None) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE alerts SET status='resolved', resolved_at=? WHERE fingerprint=?",
                (ends_at or int(time.time()), fingerprint),
            )

    def get_alert(self, fingerprint: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM alerts WHERE fingerprint=?", (fingerprint,)
            ).fetchone()

    def list_firing_alerts(self) -> list[str]:
        """Fingerprints of alerts still recorded as firing (for startup reconciliation)."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT fingerprint FROM alerts WHERE status='firing'"
            ).fetchall()
        return [row[0] for row in rows]

    # ---- repairs ----

    def create_repair(
        self,
        repair_id: str,
        fingerprint: str,
        payload_json: str,
        attempt: int = 1,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                "INSERT INTO repairs(id, fingerprint, payload_json, status, attempt, created_at, updated_at) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (repair_id, fingerprint, payload_json, attempt, now, now),
            )

    def get_repair(self, repair_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM repairs WHERE id=?", (repair_id,)
            ).fetchone()

    def set_repair_status(self, repair_id: str, status: str, **fields: Any) -> None:
        allowed = {
            "attempt",
            "agent_call_count",
            "result",
            "error",
            "finished_at",
            "lease_owner",
            "lease_expires_at",
            "error_class",
            "original_error",
            "recovery_error",
            "timeout_kind",
        }
        columns = ["status=?", "updated_at=?"]
        values: list[Any] = [status, int(time.time())]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported repair field: {key}")
            columns.append(f"{key}=?")
            values.append(value)
        values.append(repair_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE repairs SET {', '.join(columns)} WHERE id=?",  # noqa: S608
                values,
            )

    def increment_agent_calls(self, repair_id: str, amount: int = 1) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE repairs SET agent_call_count = agent_call_count + ?, updated_at=? "
                "WHERE id=?",
                (amount, int(time.time()), repair_id),
            )

    def list_repairs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM repairs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            )

    def get_repair_state_for_fingerprint(self, fingerprint: str) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM repairs WHERE fingerprint=? "
                "AND status NOT IN ('closed','rolled_back','failed','escalated','interrupted') "
                "ORDER BY created_at DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
            return "in_progress" if row is not None else "none"

    # ---- actions ----

    def add_action(
        self,
        action_id: str,
        repair_id: str,
        tool: str,
        target: str,
        status: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        output: str = "",
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                "INSERT INTO actions(id, repair_id, tool, target, before_json, after_json, "
                "status, output, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    repair_id,
                    tool,
                    target,
                    json.dumps(before, ensure_ascii=False) if before is not None else None,
                    json.dumps(after, ensure_ascii=False) if after is not None else None,
                    status,
                    output[:20_000],
                    now,
                    now,
                ),
            )

    def list_actions(self, repair_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM actions WHERE repair_id=? ORDER BY created_at", (repair_id,)
                )
            )

    # ---- approvals ----

    def add_approval(
        self,
        approval_id: str,
        ref_kind: str,
        ref_id: str,
        action: str,
        decided_by: str,
        note: str,
    ) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO approvals(id, ref_kind, ref_id, action, decided_by, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (approval_id, ref_kind, ref_id, action, decided_by, note, int(time.time())),
            )
            return cursor.rowcount == 1

    def get_approval(self, ref_kind: str, ref_id: str, action: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM approvals WHERE ref_kind=? AND ref_id=? AND action=?",
                (ref_kind, ref_id, action),
            ).fetchone()

    # ---- candidates / playbooks ----

    def create_candidate(
        self,
        candidate_id: str,
        pattern: str,
        scope: str,
        tool_sequence: str,
        verifier_ids: str,
        status: str,
        trial_deadline: int,
        default_disposition: str,
        failure_signature: str,
        reopen_conditions: str,
        source_repair_id: str,
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                "INSERT INTO candidates(id, pattern, scope, tool_sequence, verifier_ids, status, "
                "trial_deadline, default_disposition, failure_signature, reopen_conditions, "
                "source_repair_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    pattern,
                    scope,
                    tool_sequence,
                    verifier_ids,
                    status,
                    trial_deadline,
                    default_disposition,
                    failure_signature,
                    reopen_conditions,
                    source_repair_id,
                    now,
                    now,
                ),
            )

    def get_candidate(self, candidate_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()

    def find_candidate(self, pattern: str, statuses: tuple[str, ...]) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            return self._connection.execute(
                f"SELECT * FROM candidates WHERE pattern=? AND status IN ({placeholders}) "  # noqa: S608
                "ORDER BY created_at DESC LIMIT 1",
                (pattern, *statuses),
            ).fetchone()

    def update_candidate(self, candidate_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "trial_deadline",
            "default_disposition",
            "times_supported",
            "failure_signature",
            "reopen_conditions",
            "scope",
            "tool_sequence",
            "verifier_ids",
        }
        columns = ["updated_at=?"]
        values: list[Any] = [int(time.time())]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported candidate field: {key}")
            columns.append(f"{key}=?")
            values.append(value)
        values.append(candidate_id)
        with self._lock:
            self._connection.execute(
                f"UPDATE candidates SET {', '.join(columns)} WHERE id=?",  # noqa: S608
                values,
            )

    def list_candidates(self, status: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if status is None:
                return list(
                    self._connection.execute(
                        "SELECT * FROM candidates ORDER BY created_at DESC"
                    )
                )
            return list(
                self._connection.execute(
                    "SELECT * FROM candidates WHERE status=? ORDER BY created_at DESC", (status,)
                )
            )

    def expire_candidates(self, now: int) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE candidates SET status='archived', updated_at=? "
                "WHERE status='candidate' AND trial_deadline < ?",
                (now, now),
            )
            return cursor.rowcount

    def promote_candidate(self, candidate_id: str) -> None:
        now = int(time.time())
        with self._lock:
            self._connection.execute(
                "UPDATE candidates SET status='official', updated_at=? WHERE id=?",
                (now, candidate_id),
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO playbooks(candidate_id, promoted_at) VALUES (?, ?)",
                (candidate_id, now),
            )

    def dismiss_candidate(self, candidate_id: str) -> bool:
        now = int(time.time())
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE candidates SET status='archived', updated_at=? "
                "WHERE id=? AND status='candidate'",
                (now, candidate_id),
            )
            return cursor.rowcount > 0

    def list_playbooks(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT c.*, p.promoted_at FROM playbooks p "
                    "JOIN candidates c ON c.id = p.candidate_id "
                    "WHERE c.status='official' ORDER BY p.promoted_at DESC"
                )
            )

    # ---- budget ----

    def budget_calls(self, date: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT calls FROM budget_usage WHERE date=?", (date,)
            ).fetchone()
            return int(row["calls"]) if row else 0

    def add_budget_calls(self, date: str, amount: int) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO budget_usage(date, calls) VALUES (?, ?) "
                "ON CONFLICT(date) DO UPDATE SET calls=calls+excluded.calls",
                (date, amount),
            )

    # ---- settings ----

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---- run records (stable run id) ----

    def start_run_record(
        self,
        run_id: str,
        pid: int,
        hostname: str,
        python_version: str,
    ) -> None:
        with self._lock:
            now = int(time.time())
            # Single-instance startup proves any older "running" record is stale. A hard
            # process kill cannot execute lifespan cleanup, so reconcile it here.
            self._connection.execute(
                "UPDATE run_records SET status='interrupted', stopped_at=? "
                "WHERE status='running' AND run_id<>?",
                (now, run_id),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO run_records(run_id, pid, hostname, python_version, "
                "started_at, status) VALUES (?, ?, ?, ?, ?, 'running')",
                (run_id, pid, hostname, python_version, now),
            )

    def stop_run_record(self, run_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE run_records SET status='stopped', stopped_at=? WHERE run_id=?",
                (int(time.time()), run_id),
            )

    def get_run_record(self, run_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM run_records WHERE run_id=?", (run_id,)
            ).fetchone()

    def list_run_records(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._connection.execute(
                    "SELECT * FROM run_records ORDER BY started_at DESC LIMIT ?", (limit,)
                )
            )

    # ---- command audit (redacted) ----

    def add_audit_entry(
        self,
        audit_id: str,
        *,
        run_id: str,
        repair_id: str,
        kind: str,
        argv_json: str,
        exit_code: int | None,
        duration_ms: int,
        truncated: bool = False,
        error_class: str = "",
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO command_audit(id, run_id, repair_id, kind, argv_json, exit_code, "
                "duration_ms, truncated, error_class, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_id,
                    run_id,
                    repair_id,
                    kind,
                    argv_json[:8_000],
                    exit_code,
                    duration_ms,
                    1 if truncated else 0,
                    error_class,
                    int(time.time()),
                ),
            )

    def list_audit(self, repair_id: str = "", limit: int = 200) -> list[sqlite3.Row]:
        with self._lock:
            if repair_id:
                return list(
                    self._connection.execute(
                        "SELECT * FROM command_audit WHERE repair_id=? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (repair_id, limit),
                    )
                )
            return list(
                self._connection.execute(
                    "SELECT * FROM command_audit ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            )

    # ---- mutual-exclusion leases (multi-instance guard) ----

    def acquire_lease(
        self,
        fingerprint: str,
        owner_run_id: str,
        repair_id: str,
        ttl_seconds: int,
    ) -> bool:
        now = int(time.time())
        with self._lock:
            existing = self._connection.execute(
                "SELECT owner_run_id, repair_id FROM leases WHERE fingerprint=? AND expires_at > ?",
                (fingerprint, now),
            ).fetchone()
            if existing is not None:
                return False
            self._connection.execute(
                "INSERT OR REPLACE INTO leases(fingerprint, owner_run_id, repair_id, "
                "acquired_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (fingerprint, owner_run_id, repair_id, now, now + ttl_seconds),
            )
            self._connection.execute(
                "UPDATE repairs SET lease_owner=?, lease_expires_at=? WHERE id=?",
                (owner_run_id, now + ttl_seconds, repair_id),
            )
            return True

    def renew_lease(self, fingerprint: str, owner_run_id: str, ttl_seconds: int) -> bool:
        now = int(time.time())
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE leases SET expires_at=? WHERE fingerprint=? AND owner_run_id=?",
                (now + ttl_seconds, fingerprint, owner_run_id),
            )
            return cursor.rowcount == 1

    def release_lease(self, fingerprint: str, owner_run_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM leases WHERE fingerprint=? AND owner_run_id=?",
                (fingerprint, owner_run_id),
            )

    def get_lease_owner(self, fingerprint: str) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(
                "SELECT * FROM leases WHERE fingerprint=?", (fingerprint,)
            ).fetchone()

    # ---- restart recovery ----

    def list_repairs_in_states(self, statuses: tuple[str, ...], limit: int = 200) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            return list(
                self._connection.execute(
                    f"SELECT * FROM repairs WHERE status IN ({placeholders}) "  # noqa: S608
                    "ORDER BY updated_at ASC LIMIT ?",
                    (*statuses, limit),
                )
            )

    def check_writable(self) -> bool:
        """Probe that the SQLite database accepts writes (used by /ready)."""
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT INTO settings(key, value) VALUES ('health:last_ready', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(int(time.time())),),
                )
            return True
        except sqlite3.Error:
            return False


    # ---- portable read switch (S31 Step C, S32, S52) ----
    # Reads are delegated to the portable StateStore when attached and enabled.
    # Legacy SQLite remains the source of truth until the switch is fully validated;
    # old rows are never auto-deleted (S31-D).

    def attach_portable_store(self, store: Any, *, enable_read: bool = True) -> None:
        """Attach a portable StateStore for dual-read. When enable_read is True,
        get_repair/list_repairs and candidate reads prefer portable data.
        """
        self._portable_store = store
        self._portable_read_enabled = enable_read

    def detach_portable_store(self) -> None:
        self._portable_store = None
        self._portable_read_enabled = False

    def _portable_work_id(self, repair_id: str) -> str:
        return f"work_legacy_{repair_id}"

    def get_portable_work(self, repair_id: str) -> Any | None:
        if self._portable_store is None:
            return None
        try:
            return self._portable_store.get_work(self._portable_work_id(repair_id))
        except Exception:
            return None

    def list_portable_works(self, status: str | None = None) -> list[Any]:
        if self._portable_store is None:
            return []
        try:
            return self._portable_store.list_work(status)
        except Exception:
            return []

    def get_repair_via_portable(self, repair_id: str) -> dict[str, Any] | None:
        """Return a legacy-shaped dict synthesized from portable Work, or None."""
        work = self.get_portable_work(repair_id)
        if work is None:
            return None
        meta = getattr(work, "metadata", {}) or {}
        return {
            "id": repair_id,
            "fingerprint": str(meta.get("legacy_fingerprint", "")),
            "payload_json": str(getattr(work, "description", "") or ""),
            "status": str(getattr(work, "status", "open")),
            "attempt": 1,
            "portable_work_id": str(getattr(work, "id", "")),
            "source": "portable",
        }

    def list_repairs_via_portable(self, limit: int = 50) -> list[dict[str, Any]]:
        works = self.list_portable_works()
        out: list[dict[str, Any]] = []
        for w in works[:limit]:
            rid = str(getattr(w, "metadata", {}).get("legacy_repair_id", "") or getattr(w, "id", ""))
            if not rid.startswith("work_legacy_"):
                rid = str(getattr(w, "id", ""))
            out.append(
                {
                    "id": rid.replace("work_legacy_", ""),
                    "portable_work_id": str(getattr(w, "id", "")),
                    "status": str(getattr(w, "status", "")),
                    "fingerprint": str(getattr(w, "metadata", {}).get("legacy_fingerprint", "")),
                    "source": "portable",
                }
            )
        return out

    def list_repairs_with_fallback(self, limit: int = 50) -> list[Any]:
        """Dual-read: prefer portable when enabled and non-empty, fallback to SQLite."""
        if self._portable_read_enabled and self._portable_store is not None:
            portable = self.list_repairs_via_portable(limit=limit)
            if portable:
                return portable
        return self.list_repairs(limit=limit)

    def get_repair_with_fallback(self, repair_id: str) -> Any | None:
        """Dual-read for single repair: portable first, then SQLite Row."""
        if self._portable_read_enabled and self._portable_store is not None:
            portable = self.get_repair_via_portable(repair_id)
            if portable is not None:
                return portable
        return self.get_repair(repair_id)

    def list_candidates_via_portable(self, status: str | None = None) -> list[Any]:
        if self._portable_store is None:
            return []
        try:
            return self._portable_store.list_knowledge(status)
        except Exception:
            return []

    def get_knowledge_for_candidate(self, candidate_id: str) -> Any | None:
        if self._portable_store is None:
            return None
        try:
            return self._portable_store.get_knowledge(candidate_id)
        except Exception:
            return None

    def close(self) -> None:
        with self._lock:
            self._connection.close()







