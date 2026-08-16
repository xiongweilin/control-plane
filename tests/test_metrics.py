from __future__ import annotations

from control_plane.metrics import ControlPlaneCollector
from control_plane.storage import Store


def test_collector_emits_metrics(tmp_path) -> None:
    store = Store(tmp_path / "cp.db")
    collector = ControlPlaneCollector(store, budget_remaining=lambda: 5)
    families = {f.name: f for f in collector.collect()}
    assert "control_plane_repairs_total" in families
    assert "control_plane_repairs_active" in families
    assert "control_plane_repairs_recoverable" in families
    assert "control_plane_candidates" in families
    assert "control_plane_playbooks" in families
    assert "control_plane_budget_remaining" in families
    assert "control_plane_agent_calls_today" in families
    assert "control_plane_run_info" in families
    assert "control_plane_health_last_ready" in families
    assert "control_plane_last_scan_ts" in families
    assert "control_plane_last_digest_ts" in families
    store.set_setting("health:last_ready", "1700000000")
    store.set_setting("scan:last_ts", "1700000001")
    store.set_setting("digest:last_ts", "1700000002")
    families = {f.name: f for f in collector.collect()}
    samples = list(families["control_plane_health_last_ready"].samples)
    assert samples[0].value == 1700000000
    assert list(families["control_plane_last_scan_ts"].samples)[0].value == 1700000001
    assert list(families["control_plane_last_digest_ts"].samples)[0].value == 1700000002
    store.close()


def test_collector_active_excludes_quiescent_states(tmp_path) -> None:
    from control_plane.state_machine import RepairState

    store = Store(tmp_path / "cp.db")
    store.create_repair("r-running", "fp-1", "{}")
    store.set_repair_status("r-running", RepairState.PROPOSING.value)
    store.create_repair("r-interrupted", "fp-2", "{}")
    store.set_repair_status("r-interrupted", RepairState.INTERRUPTED.value)
    store.create_repair("r-closed", "fp-3", "{}")
    store.set_repair_status("r-closed", RepairState.CLOSED.value)
    collector = ControlPlaneCollector(store, budget_remaining=lambda: 5)
    families = {f.name: f for f in collector.collect()}
    active = families["control_plane_repairs_active"]
    assert list(active.samples)[0].value == 1
    recoverable = families["control_plane_repairs_recoverable"]
    labels = {s.labels["status"] for s in recoverable.samples}
    assert labels == {RepairState.INTERRUPTED.value}
    store.close()
