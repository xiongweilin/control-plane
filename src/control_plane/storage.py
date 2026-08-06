from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Store:
    """SQLite persistence for alerts, repairs, actions, approvals, candidates and budget."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
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

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
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
        allowed = {"attempt", "agent_call_count", "result", "error", "finished_at"}
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

    def close(self) -> None:
        with self._lock:
            self._connection.close()
