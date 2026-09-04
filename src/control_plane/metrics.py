"""Prometheus metrics for the personal control-plane profile.

The control-plane profile used to expose metrics from a historical repair store.
Agent Kernel v2 owns the durable state now, so this module is deliberately a
read-only projection over the canonical Work, Knowledge, Event, and
Responsibility stores.  It keeps the existing dashboard metric names stable
while making their source and compatibility semantics explicit.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from time import time
from typing import Any

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

_REPAIR_KIND = "personal-incident-repair"
_REPAIR_STATUS_LABELS = ("active", "waiting", "blocked", "closed", "failed", "interrupted")
_CALL_EVENT_SUFFIX = "CapabilityResultObserved"
_RETRY_MODES = {"retry-idempotent", "retry-request"}
_FAILURE_STATUSES = {"failed", "failure", "error"}


def _repair_status(status: str) -> str:
    if status in {"open", "ready", "running"}:
        return "active"
    if status in _REPAIR_STATUS_LABELS:
        return status
    if status == "completed":
        return "closed"
    if status in {"cancelled", "archived"}:
        return "interrupted"
    return "blocked"


def _nested_value(payload: Any, *keys: str) -> Any:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if key in payload:
            return payload[key]
    for value in payload.values():
        nested = _nested_value(value, *keys)
        if nested is not None:
            return nested
    return None


class ControlPlaneMetricsCollector:
    """Collect current profile metrics from Agent Kernel's canonical stores."""

    def __init__(self, runtime: Any, responsibilities: Any | None = None) -> None:
        self.runtime = runtime
        self.responsibilities = responsibilities
        self._last_ready = 0.0

    def mark_ready(self) -> None:
        """Record a successful /ready probe without changing kernel state."""

        self._last_ready = time()

    def _works(self) -> tuple[list[Any], int]:
        try:
            return list(self.runtime.store.list_work()), 0
        except Exception:
            return [], 1

    def _candidates(self) -> tuple[int, int]:
        errors = 0
        try:
            projections = list(self.runtime.store.list_knowledge_projections("candidate"))
        except Exception:
            projections = []
            errors += 1
        try:
            # list_knowledge() is a compatibility view and may duplicate
            # projections, so use its raw legacy namespace here.
            legacy = list(self.runtime.store.list_raw_legacy_knowledge("candidate"))
        except Exception:
            legacy = []
            errors += 1
        return len(projections) + len(legacy), errors

    def _resource_pool(self) -> tuple[int, int, int]:
        """Return available api_calls, configured flag, and read errors."""

        if self.responsibilities is None:
            return 0, 0, 0
        try:
            pools = self.responsibilities.journal.list("ResourcePool")
            pool = next(
                (
                    value
                    for value in pools
                    if getattr(value, "pool_key", "") == "personal-control-plane"
                ),
                None,
            )
            if pool is None:
                return 0, 0, 0
            available = self.responsibilities.available_resources(pool.id, now=datetime.now(UTC))
            return max(0, int(available.api_calls)), 1, 0
        except Exception:
            return 0, 0, 1

    def _today_calls(self) -> tuple[int, int]:
        try:
            events = self.runtime.store.list_events()
        except Exception:
            return 0, 1
        today = datetime.now(UTC).date()
        count = 0
        for event in events:
            if not event.type.endswith(_CALL_EVENT_SUFFIX):
                continue
            created_at = event.created_at
            if created_at.astimezone(UTC).date() == today:
                count += 1
        return count, 0

    def _retry_failures(self) -> tuple[int, int]:
        try:
            events = self.runtime.store.list_events()
        except Exception:
            return 0, 1
        count = 0
        for event in events:
            if event.type not in {
                "RecoveryApplicationRecorded",
                "RecoveryApplicationObserved",
                "RecoveryDispositionRecorded",
            }:
                continue
            mode = _nested_value(event.payload, "recovery_mode", "mode")
            status = _nested_value(event.payload, "status", "outcome", "result_status")
            if str(mode) in _RETRY_MODES and str(status).lower() in _FAILURE_STATUSES:
                count += 1
        return count, 0

    def collect(self) -> Iterator[GaugeMetricFamily]:
        errors = 0
        works, work_errors = self._works()
        errors += work_errors

        repair_counts: Counter[str] = Counter()
        for work in works:
            if getattr(work, "kind", None) == _REPAIR_KIND:
                repair_counts[_repair_status(str(work.status))] += 1

        repairs = GaugeMetricFamily(
            "control_plane_repairs_total",
            "Current personal incident-repair Work count by v2 compatibility status",
            labels=["status"],
        )
        for label in _REPAIR_STATUS_LABELS:
            repairs.add_metric([label], repair_counts[label])
        yield repairs

        failed = GaugeMetricFamily(
            "control_plane_repairs_failed_total",
            "Current personal incident-repair Work count in the failed state",
            labels=["status"],
        )
        failed.add_metric(["failed"], repair_counts["failed"])
        yield failed

        active = repair_counts["active"]
        active_metric = GaugeMetricFamily(
            "control_plane_repairs_active",
            "Current personal incident-repair Work count in active states",
        )
        active_metric.add_metric([], active)
        yield active_metric
        recoverable_metric = GaugeMetricFamily(
            "control_plane_repairs_recoverable",
            "Current personal incident-repair Work count waiting or blocked",
        )
        recoverable_metric.add_metric([], repair_counts["waiting"] + repair_counts["blocked"])
        yield recoverable_metric

        candidates, candidate_errors = self._candidates()
        errors += candidate_errors
        candidates_metric = GaugeMetricFamily(
            "control_plane_candidates",
            "Current candidate knowledge count from canonical and raw legacy namespaces",
        )
        candidates_metric.add_metric([], candidates)
        yield candidates_metric

        available_api_calls, pool_configured, pool_errors = self._resource_pool()
        errors += pool_errors
        configured_metric = GaugeMetricFamily(
            "control_plane_resource_pool_configured",
            "Whether the personal-control-plane Responsibility resource pool exists",
            labels=["pool"],
        )
        configured_metric.add_metric(["personal-control-plane"], pool_configured)
        yield configured_metric
        available_metric = GaugeMetricFamily(
            "control_plane_resource_available",
            "Available resource units in the personal Responsibility resource pool",
            labels=["resource"],
        )
        available_metric.add_metric(["api_calls"], available_api_calls)
        yield available_metric
        # Compatibility alias for existing consumers.  In v2 this is resource
        # pool capacity, not a daily Agent budget.
        budget_metric = GaugeMetricFamily(
            "control_plane_budget_remaining",
            "Compatibility alias for available personal-control-plane api_calls",
        )
        budget_metric.add_metric([], available_api_calls)
        yield budget_metric

        calls_today, call_errors = self._today_calls()
        errors += call_errors
        calls_metric = GaugeMetricFamily(
            "control_plane_agent_calls_today",
            "Current-day capability-result events observed by the control-plane profile",
        )
        calls_metric.add_metric([], calls_today)
        yield calls_metric

        retry_failures, retry_errors = self._retry_failures()
        errors += retry_errors
        retry_metric = CounterMetricFamily(
            "control_plane_recovery_retry_failed_total",
            "Observed failed idempotent recovery applications",
        )
        retry_metric.add_metric([], retry_failures)
        yield retry_metric

        # This is updated only by a successful /ready request; zero means that
        # no readiness proof has been observed since this process started.
        ready_metric = GaugeMetricFamily(
            "control_plane_health_last_ready",
            "Unix timestamp of the last successful control-plane /ready probe",
        )
        ready_metric.add_metric([], self._last_ready)
        yield ready_metric

        errors_metric = GaugeMetricFamily(
            "control_plane_metrics_collection_errors",
            "Number of store reads that failed during this metrics collection",
        )
        errors_metric.add_metric([], errors)
        yield errors_metric


__all__ = ["ControlPlaneMetricsCollector"]
