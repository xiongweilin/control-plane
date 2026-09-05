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

from .environment import EnvironmentSnapshot

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

    def __init__(
        self,
        runtime: Any,
        responsibilities: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.responsibilities = responsibilities
        self._last_ready = 0.0
        self._last_ready_ok: bool | None = None
        self._last_ready_providers: dict[str, bool] = {}
        self._current_providers: dict[str, bool] = {}
        self._environment_snapshot: EnvironmentSnapshot | None = None

    def mark_ready(self) -> None:
        """Record a successful /ready probe without changing kernel state."""

        self._last_ready = time()

    def record_readiness(self, ready: bool, kernel: dict[str, Any]) -> None:
        """Keep the last readiness proof separate from current provider health."""

        self._last_ready_ok = ready
        providers = kernel.get("providers", []) if isinstance(kernel, dict) else []
        self._last_ready_providers = {
            str(item.get("provider_id")): bool(item.get("available"))
            for item in providers
            if isinstance(item, dict) and item.get("provider_id")
        }
        if ready:
            self.mark_ready()

    def record_current_provider_health(self, kernel: dict[str, Any]) -> None:
        providers = kernel.get("providers", []) if isinstance(kernel, dict) else []
        self._current_providers = {
            str(item.get("provider_id")): bool(item.get("available"))
            for item in providers
            if isinstance(item, dict) and item.get("provider_id")
        }

    def set_environment_snapshot(self, snapshot: EnvironmentSnapshot | None) -> None:
        self._environment_snapshot = snapshot

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

    def collect(self) -> Iterator[GaugeMetricFamily | CounterMetricFamily]:
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

        ready_metric = GaugeMetricFamily(
            "control_plane_ready_status",
            "Last observed control-plane readiness result",
        )
        ready_metric.add_metric([], 1 if self._last_ready_ok is True else 0)
        yield ready_metric

        mismatch_metric = GaugeMetricFamily(
            "control_plane_ready_provider_mismatch",
            "Current provider health differs from the last readiness proof",
            labels=["provider"],
        )
        provider_ids = sorted(set(self._last_ready_providers) | set(self._current_providers))
        for provider_id in provider_ids:
            mismatch = (
                int(
                    self._last_ready_providers[provider_id]
                    != self._current_providers[provider_id]
                )
                if provider_id in self._last_ready_providers
                and provider_id in self._current_providers
                else 0
            )
            mismatch_metric.add_metric([provider_id], mismatch)
        yield mismatch_metric

        snapshot = self._environment_snapshot
        environment_metric = GaugeMetricFamily(
            "control_plane_environment_check",
            "Read-only environment check state: 0=ok, 1=problem, 2=unknown",
            labels=["check", "status", "severity", "automation", "configured"],
        )
        environment_timestamp = GaugeMetricFamily(
            "control_plane_environment_last_check_timestamp_seconds",
            "Unix timestamp of the last environment inspection",
        )
        environment_errors = GaugeMetricFamily(
            "control_plane_environment_probe_errors",
            "Whether the last environment inspection failed at the probe boundary",
        )
        if snapshot is None:
            environment_timestamp.add_metric([], 0)
            environment_errors.add_metric([], 0)
        else:
            environment_timestamp.add_metric([], snapshot.checked_at)
            environment_errors.add_metric([], 1 if snapshot.probe_error else 0)
            state_values = {"ok": 0, "problem": 1, "unknown": 2}
            for item in snapshot.observations:
                environment_metric.add_metric(
                    [
                        item.name,
                        item.status,
                        item.severity,
                        item.automation,
                        str(bool(item.metadata.get("configured", True))).lower(),
                    ],
                    state_values.get(item.status, 2),
                )
        yield environment_metric
        yield environment_timestamp
        yield environment_errors

        lifecycle_metrics = (
            ("control_plane_recoverability_status", "recoverability"),
            ("control_plane_synchronization_status", "synchronization"),
            ("control_plane_automatic_handling_status", "automatic_handling"),
        )
        for metric_name, check_name in lifecycle_metrics:
            metric = GaugeMetricFamily(
                metric_name,
                f"Current {check_name} state: 1=ok, 0=problem, -1=unknown",
            )
            lifecycle_item = next(
                (
                    entry
                    for entry in (snapshot.observations if snapshot else ())
                    if entry.name == check_name
                ),
                None,
            )
            value = {"ok": 1, "problem": 0, "unknown": -1}.get(
                lifecycle_item.status if lifecycle_item else "unknown",
                -1,
            )
            metric.add_metric([], value)
            yield metric

        garbage_metric = GaugeMetricFamily(
            "control_plane_known_garbage_paths",
            "Number of configured known-garbage paths awaiting bounded cleanup",
        )
        garbage_item = next(
            (
                entry
                for entry in (snapshot.observations if snapshot else ())
                if entry.name == "known_garbage"
            ),
            None,
        )
        garbage_metric.add_metric(
            [], float((garbage_item.metadata if garbage_item else {}).get("count", 0) or 0)
        )
        yield garbage_metric

        if snapshot is not None:
            for metric_name, check_name, label_name in (
                (
                    "control_plane_docker_exited_containers",
                    "docker_exited_containers",
                    "exited_count",
                ),
                (
                    "control_plane_windows_recursive_scan_access_errors",
                    "windows_recursive_scan",
                    "access_errors",
                ),
            ):
                metric = GaugeMetricFamily(metric_name, f"Current value from {check_name}")
                env_item = next(
                    (entry for entry in snapshot.observations if entry.name == check_name),
                    None,
                )
                value = env_item.metadata.get(label_name, 0) if env_item is not None else 0
                metric.add_metric([], float(value or 0))
                yield metric
            cache_metric = GaugeMetricFamily(
                "control_plane_docker_build_cache_bytes",
                "Observed Docker build cache size in bytes",
            )
            cache_item = next(
                (entry for entry in snapshot.observations if entry.name == "docker_build_cache"),
                None,
            )
            cache_metric.add_metric(
                [], float((cache_item.metadata if cache_item else {}).get("bytes", 0) or 0)
            )
            yield cache_metric


__all__ = ["ControlPlaneMetricsCollector"]
