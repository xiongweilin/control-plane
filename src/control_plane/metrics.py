from __future__ import annotations

import datetime as dt

from prometheus_client import Counter
from prometheus_client.core import GaugeMetricFamily

from .runtime import current_run_id
from .state_machine import TERMINAL_STATES
from .storage import Store

# 鉴权失败计数（低噪声安全可观测；按 reason/endpoint 打标）
AUTH_FAILURES = Counter(
    "control_plane_auth_failures_total",
    "Authentication failures",
    ["reason", "endpoint"],
)


class ControlPlaneCollector:
    """Prometheus collector over the control-plane SQLite state."""

    def __init__(self, store: Store, budget_remaining) -> None:
        self._store = store
        self._budget_remaining = budget_remaining

    def collect(self):
        rows = self._store.list_repairs(limit=100_000)
        terminal = {s.value for s in TERMINAL_STATES}
        status_counts: dict[str, int] = {}
        active = 0
        for row in rows:
            status = row["status"]
            status_counts[status] = status_counts.get(status, 0) + 1
            if status not in terminal:
                active += 1

        repairs = GaugeMetricFamily(
            "control_plane_repairs_total",
            "Control-plane repairs by status",
            labels=["status"],
        )
        for status, count in sorted(status_counts.items()):
            repairs.add_metric([status], count)
        yield repairs

        yield GaugeMetricFamily(
            "control_plane_repairs_active",
            "Control-plane repairs currently in progress",
            value=active,
        )
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
                "Resolved Codex CLI version and path",
                labels=["version", "path"],
            )
            codex_info.add_metric([codex_version, codex_path], 1)
            yield codex_info
        yield GaugeMetricFamily(
            "control_plane_health_last_ready",
            "Unix timestamp of the last successful /ready database probe",
            value=int(self._store.get_setting("health:last_ready", "0") or 0),
        )
