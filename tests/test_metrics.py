from __future__ import annotations

from control_plane.metrics import ControlPlaneCollector
from control_plane.storage import Store


def test_collector_emits_metrics(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    collector = ControlPlaneCollector(store, budget_remaining=lambda: 5)
    families = {f.name: f for f in collector.collect()}
    assert "control_plane_repairs_total" in families
    assert "control_plane_repairs_active" in families
    assert "control_plane_candidates" in families
    assert "control_plane_playbooks" in families
    assert "control_plane_budget_remaining" in families
    assert "control_plane_agent_calls_today" in families
    assert "control_plane_run_info" in families
    assert "control_plane_health_last_ready" in families
    store.set_setting("health:last_ready", "1700000000")
    families = {f.name: f for f in collector.collect()}
    samples = list(families["control_plane_health_last_ready"].samples)
    assert samples[0].value == 1700000000
    store.close()
