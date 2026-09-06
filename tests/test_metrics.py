from portable_runtime.core.models import Work
from portable_runtime.core.runtime import Runtime
from portable_runtime.records.knowledge import KnowledgeProjection
from prometheus_client import CollectorRegistry, generate_latest

from control_plane.environment import CheckObservation, EnvironmentSnapshot
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
    runtime.store.save_work(Work(kind="personal-incident-repair", title="open", status="open"))
    runtime.store.save_work(Work(kind="personal-incident-repair", title="ready", status="ready"))
    runtime.store.save_work(
        Work(kind="personal-incident-repair-blocked", title="blocked", status="waiting")
    )
    runtime.store.save_work(Work(kind="personal-command", title="unrelated"))
    runtime.store.save_knowledge_projection(KnowledgeProjection(title="candidate"))

    text = _scrape(runtime)

    assert 'control_plane_repairs_total{status="closed"} 0.0' in text
    assert 'control_plane_repairs_total{status="failed"} 1.0' in text
    assert 'control_plane_repairs_total{status="active"} 1.0' in text
    assert 'control_plane_repairs_total{status="waiting"} 3.0' in text
    assert "control_plane_repairs_active 1.0" in text
    assert "control_plane_repairs_recoverable 3.0" in text
    assert "control_plane_candidates 1.0" in text


def test_profile_metrics_expose_environment_state() -> None:
    runtime = Runtime(runtime_id="test")
    collector = ControlPlaneMetricsCollector(runtime)
    collector.set_environment_snapshot(
        EnvironmentSnapshot(
            checked_at=123.0,
            observations=(
                CheckObservation(
                    name="docker_build_cache",
                    status="problem",
                    severity="warning",
                    automation="codex-judgment",
                    detail="too large",
                    manual_action="manual",
                    metadata={"configured": True, "bytes": 2048},
                ),
            ),
        )
    )
    collector.record_readiness(
        True,
        {"providers": [{"provider_id": "codex-primary", "available": True}]},
    )
    collector.record_current_provider_health(
        {"providers": [{"provider_id": "codex-primary", "available": False}]}
    )
    registry = CollectorRegistry(auto_describe=False)
    registry.register(collector)
    text = generate_latest(registry).decode("utf-8")

    assert (
        'control_plane_environment_check{automation="codex-judgment",check="docker_build_cache",'
        'configured="true",severity="warning",status="problem"} 1.0'
        in text
    )
    assert "control_plane_docker_build_cache_bytes 2048.0" in text
    assert 'control_plane_ready_provider_mismatch{provider="codex-primary"} 1.0' in text
