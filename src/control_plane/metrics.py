from __future__ import annotations

import datetime as dt

from prometheus_client.core import GaugeMetricFamily

from .state_machine import TERMINAL_STATES
from .storage import Store


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
