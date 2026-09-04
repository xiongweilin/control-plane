from portable_runtime.core.models import Work
from portable_runtime.core.runtime import Runtime
from portable_runtime.records.knowledge import KnowledgeProjection
from prometheus_client import CollectorRegistry, generate_latest

from control_plane.metrics import ControlPlaneMetricsCollector


def _scrape(runtime: Runtime) -> str:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(ControlPlaneMetricsCollector(runtime))
    return generate_latest(registry).decode("utf-8")


def test_profile_metrics_emit_zero_series_when_no_repairs_exist() -> None:
    text = _scrape(Runtime(runtime_id="test"))

    assert 'control_plane_repairs_total{status="closed"} 0.0' in text
    assert 'control_plane_repairs_total{status="failed"} 0.0' in text
    assert "control_plane_repairs_active 0.0" in text
    assert "control_plane_candidates 0.0" in text
    assert "control_plane_recovery_retry_failed_total 0.0" in text


def test_profile_metrics_project_kernel_v2_state() -> None:
    runtime = Runtime(runtime_id="test")
    runtime.store.save_work(Work(kind="personal-incident-repair", title="failed", status="failed"))
    runtime.store.save_work(Work(kind="personal-incident-repair", title="active", status="running"))
    runtime.store.save_work(Work(kind="personal-command", title="unrelated"))
    runtime.store.save_knowledge_projection(KnowledgeProjection(title="candidate"))

    text = _scrape(runtime)

    assert 'control_plane_repairs_total{status="closed"} 0.0' in text
    assert 'control_plane_repairs_total{status="failed"} 1.0' in text
    assert 'control_plane_repairs_total{status="active"} 1.0' in text
    assert "control_plane_repairs_active 1.0" in text
    assert "control_plane_candidates 1.0" in text
