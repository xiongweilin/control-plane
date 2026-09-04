from __future__ import annotations

import datetime as dt

from prometheus_client import Counter, Gauge
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .outward_semantics import INTEGRITY_KINDS, integrity_violation_flags
from .runtime import current_run_id
from .state_machine import QUIESCENT_STATES, RECOVERABLE_STATES
from .storage import Store

# 鉴权失败计数（低噪声安全可观测；按 reason/endpoint 打标）
AUTH_FAILURES = Counter(
    "control_plane_auth_failures_total",
    "Authentication failures",
    ["reason", "endpoint"],
)

# 出站飞书通知失败计数（低噪声可观测；按 reason 打标：script_missing / spawn_or_timeout）
NOTIFY_FAILURES = Counter(
    "control_plane_notify_failures_total",
    "Outbound Feishu notification failures (script missing / spawn / timeout)",
    ["reason"],
)

# 模型来源连通性（批次5-5，2026-08-11 起 source=gateway 探测本机 LiteLLM 网关）：1=可达，0=不可达
MODEL_CONNECTIVITY = Gauge(
    "control_plane_model_connectivity",
    "Model source reachability (1=ok, 0=unreachable)",
    ["source"],
)

# 模型清单漂移（批次5-5）：1=与基线不一致（基线来自本机模型网关模型清单）
MODEL_DRIFT = Gauge(
    "control_plane_model_drift",
    "1 when the model gateway list drifted from the recorded baseline",
)

# 受控忽略（批次5-6）：明确语义的 except...pass 按 site 计数，低噪声可观测
CONTROLLED_IGNORES = Counter(
    "control_plane_ignored_errors_total",
    "Controlled exception ignores by site",
    ["site"],
)

# Game-mode suppression is deliberately a bounded, allowlisted signal.  The
# alert fingerprint is kept in SQLite audit settings instead of a metric label
# so a noisy alert stream cannot create unbounded time series.
GAME_MODE_SUPPRESSED = Counter(
    "control_plane_game_mode_alerts_suppressed_total",
    "Alerts suppressed while the CS2 game-mode state was active",
    ["alertname", "phase"],
)


class ControlPlaneCollector:
    """Prometheus collector over the control-plane SQLite state."""

    def __init__(self, store: Store, budget_remaining) -> None:
        self._store = store
        self._budget_remaining = budget_remaining

    def collect(self):
        rows = self._store.list_repairs(limit=100_000)
        quiescent = {s.value for s in QUIESCENT_STATES}
        status_counts: dict[str, int] = {}
        resolution_counts: dict[str, int] = {}
        restoration_counts: dict[str, int] = {}
        integrity_counts = dict.fromkeys(INTEGRITY_KINDS, 0)
        active = 0
        recoverable: dict[str, int] = {}
        for row in rows:
            status = row["status"]
            status_counts[status] = status_counts.get(status, 0) + 1
            resolution = str(row["resolution_kind"] or "unresolved")
            restoration = str(row["restoration_status"] or "unverified")
            resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
            restoration_counts[restoration] = restoration_counts.get(restoration, 0) + 1
            for kind, violated in integrity_violation_flags(row).items():
                if violated:
                    integrity_counts[kind] += 1
            if status not in quiescent:
                active += 1
            elif status in {s.value for s in RECOVERABLE_STATES}:
                recoverable[status] = recoverable.get(status, 0) + 1

        repairs = GaugeMetricFamily(
            "control_plane_repairs_total",
            "Control-plane repairs by status",
            labels=["status"],
        )
        for status, count in sorted(status_counts.items()):
            repairs.add_metric([status], count)
        yield repairs

        # ``control_plane_repairs_total`` is a status snapshot (a gauge): a
        # repair can move between non-terminal states, so applying
        # ``increase()`` to it is semantically incorrect.  Expose the
        # terminal failed count separately as a monotonic counter for trend
        # alerts while retaining the snapshot metric for dashboards.
        failed_total_value = self._store.get_setting("metrics:repairs_failed_total", "")
        try:
            failed_count = max(
                int(failed_total_value or 0), status_counts.get("failed", 0)
            )
        except ValueError:
            failed_count = status_counts.get("failed", 0)
        # A one-time bootstrap also repairs old databases that predate the
        # persisted event counter; later status transitions update this value
        # transactionally in Store.set_repair_status.
        if not failed_total_value or str(failed_count) != failed_total_value:
            self._store.set_setting("metrics:repairs_failed_total", str(failed_count))
        failed_total = CounterMetricFamily(
            "control_plane_repairs_failed_total",
            "Cumulative number of repairs that ended in failed status",
            labels=["status"],
        )
        failed_total.add_metric(["failed"], failed_count)
        yield failed_total

        yield GaugeMetricFamily(
            "control_plane_repairs_active",
            "Control-plane repairs currently in progress",
            value=active,
        )
        recoverable_gauge = GaugeMetricFamily(
            "control_plane_repairs_recoverable",
            "Quiescent repairs that may resume (interrupted/recovering/needs_approval)",
            labels=["status"],
        )
        for status, count in sorted(recoverable.items()):
            recoverable_gauge.add_metric([status], count)
        yield recoverable_gauge

        by_resolution = GaugeMetricFamily(
            "control_plane_repairs_by_resolution",
            "Control-plane repair snapshot by persisted resolution kind",
            labels=["resolution_kind"],
        )
        for resolution, count in sorted(resolution_counts.items()):
            by_resolution.add_metric([resolution], count)
        yield by_resolution

        by_restoration = GaugeMetricFamily(
            "control_plane_repairs_by_restoration",
            "Control-plane repair snapshot by persisted restoration status",
            labels=["restoration_status"],
        )
        for restoration, count in sorted(restoration_counts.items()):
            by_restoration.add_metric([restoration], count)
        yield by_restoration

        semantic_integrity = GaugeMetricFamily(
            "control_plane_semantic_integrity_violations",
            "Current repair rows violating fixed outward semantic invariants",
            labels=["kind"],
        )
        for kind in INTEGRITY_KINDS:
            semantic_integrity.add_metric([kind], integrity_counts[kind])
        yield semantic_integrity

        yield GaugeMetricFamily(
            "control_plane_candidates",
            "Candidate playbooks",
            value=len(self._store.list_candidates("candidate")),
        )
        yield GaugeMetricFamily(
            "control_plane_playbooks",
            "Official playbooks",
            value=len(self._store.list_playbooks()),
        )
        yield GaugeMetricFamily(
            "control_plane_budget_remaining",
            "Daily agent budget remaining",
            value=self._budget_remaining(),
        )
        yield GaugeMetricFamily(
            "control_plane_agent_calls_today",
            "Agent calls spent today",
            value=self._store.budget_calls(dt.date.today().isoformat()),
        )
        run_id = current_run_id()
        run_info = GaugeMetricFamily(
            "control_plane_run_info",
            "Stable run identifier of the serving process",
            labels=["run_id"],
        )
        run_info.add_metric([run_id or "unknown"], 1)
        yield run_info
        codex_version = str(self._store.get_setting("codex:cli_version", "") or "")
        codex_path = str(self._store.get_setting("codex:cli_path", "") or "")
        if codex_version:
            codex_info = GaugeMetricFamily(
                "control_plane_codex_cli_info",
                "Resolved codex CLI version and path",
                labels=["version", "path"],
            )
            codex_info.add_metric([codex_version, codex_path], 1)
            yield codex_info
        yield GaugeMetricFamily(
            "control_plane_health_last_ready",
            "Unix timestamp of the last successful /ready database probe",
            value=int(self._store.get_setting("health:last_ready", "0") or 0),
        )
        yield GaugeMetricFamily(
            "control_plane_last_scan_ts",
            "Unix timestamp of the last successful daily environment scan",
            value=int(self._store.get_setting("scan:last_ts", "0") or 0),
        )
        yield GaugeMetricFamily(
            "control_plane_last_digest_ts",
            "Unix timestamp of the last successful daily digest",
            value=int(self._store.get_setting("digest:last_ts", "0") or 0),
        )
