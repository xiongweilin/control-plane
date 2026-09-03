from __future__ import annotations

import json
from dataclasses import replace

import httpx

from control_plane.alerts import alert_fingerprint, fingerprint_from_labels
from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.models import Alert
from control_plane.notify import Notifier
from control_plane.service import RepairService
from control_plane.storage import Store


def _make_service(tmp_path) -> tuple[RepairService, Store]:
    cfg = replace(
        ControlPlaneConfig(),
        api_key="x",
        state_db=tmp_path / "cp.db",
        notification_enabled=False,
        prometheus_url="http://prometheus.local",
    )
    store = Store(cfg.state_db)
    service = RepairService(
        cfg,
        store,
        Budget(store, 10, 8),
        object(),  # agent unused by reconciliation
        ApprovalManager(),
        object(),  # notifier unused: _verify_alert_recovery fails before notify for unknown alerts
    )
    return service, store


def _alert(name: str, instance: str = "") -> Alert:
    return Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": name, "instance": instance},
            "annotations": {},
            "startsAt": "2026-08-14T00:00:00Z",
            "endsAt": None,
            "generatorURL": "",
            "fingerprint": "x",
        }
    )


def _seed_firing(store: Store, alert: Alert) -> str:
    fp = alert_fingerprint(alert)
    store.upsert_alert(
        fp,
        alert.labels.get("alertname", ""),
        alert.labels.get("instance", ""),
        alert.labels.get("project", ""),
        alert.labels.get("container", ""),
        "firing",
        int(alert.starts_at.timestamp()),
    )
    store.set_setting(
        f"alert_payload:{fp}",
        json.dumps(alert.model_dump(mode="json", by_alias=True), ensure_ascii=False),
    )
    return fp


def _prometheus_alerts(*alerts: tuple[str, str]) -> dict:
    return {
        "status": "success",
        "data": {
            "alerts": [
                {"labels": {"alertname": name, "instance": instance}, "state": "firing"}
                for name, instance in alerts
            ]
        },
    }


async def test_control_plane_stale_ready_recovery_requires_fresh_metric(tmp_path) -> None:
    service, store = _make_service(tmp_path)
    queries: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"job": "control-plane"}, "value": [0, "1"]}],
                },
            },
        )

    service.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    ok, message = await service._verify_alert_recovery(_alert("ControlPlaneStaleReady"))

    assert ok is True
    assert message == "query returned results"
    assert queries == ["(time() - control_plane_health_last_ready) < bool 3600"]
    await service.close()
    store.close()


async def test_control_plane_stale_ready_recovery_rejects_stale_metric(tmp_path) -> None:
    service, store = _make_service(tmp_path)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {"job": "control-plane"}, "value": [0, "0"]}],
                },
            },
        )

    service.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    ok, message = await service._verify_alert_recovery(_alert("ControlPlaneStaleReady"))

    assert ok is False
    assert "expected 1" in message
    await service.close()
    store.close()


async def test_control_plane_stale_ready_resolution_resets_attempts(tmp_path) -> None:
    cfg = replace(
        ControlPlaneConfig(),
        api_key="x",
        state_db=tmp_path / "cp.db",
        notification_enabled=False,
        prometheus_url="http://prometheus.local",
    )
    store = Store(cfg.state_db)
    alert = _alert("ControlPlaneStaleReady")
    fingerprint = _seed_firing(store, alert)
    service = RepairService(
        cfg,
        store,
        Budget(store, 10, 8),
        object(),
        ApprovalManager(),
        Notifier(cfg),
    )

    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {"job": "control-plane"}, "value": [0, "1"]}],
                    },
                },
            )
        )
    )
    await service._handle_resolved(alert, int(alert.starts_at.timestamp()))

    assert store.get_setting(f"attempt_reset:{fingerprint}", "") != ""
    await service.close()
    store.close()


async def test_reconcile_resolves_stale_firing_alert(tmp_path) -> None:
    service, store = _make_service(tmp_path)
    alert = _alert("DiskUsageGrowthFast")
    fp = _seed_firing(store, alert)
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_prometheus_alerts()))
    )
    await service.reconcile_alerts()
    row = store.get_alert(fp)
    assert row["status"] == "resolved"
    assert row["resolved_at"] is not None
    # Recovery is not verifiable for this alert name: no attempt reset.
    assert store.get_setting(f"attempt_reset:{fp}", "") == ""
    await service.close()


async def test_reconcile_keeps_active_alerts(tmp_path) -> None:
    service, store = _make_service(tmp_path)
    active = _alert("DockerVolumeGrowthFast")
    stale = _alert("DiskUsageGrowthFast")
    fp_active = _seed_firing(store, active)
    fp_stale = _seed_firing(store, stale)
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=_prometheus_alerts(("DockerVolumeGrowthFast", "")),
            )
        )
    )
    await service.reconcile_alerts()
    assert store.get_alert(fp_active)["status"] == "firing"
    assert store.get_alert(fp_stale)["status"] == "resolved"
    await service.close()


async def test_reconcile_missing_payload_marks_resolved(tmp_path) -> None:
    service, store = _make_service(tmp_path)
    alert = _alert("DockerVolumeGrowthFast")
    fp = alert_fingerprint(alert)
    store.upsert_alert(fp, "DockerVolumeGrowthFast", "", "", "", "firing", 1)
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_prometheus_alerts()))
    )
    await service.reconcile_alerts()
    assert store.get_alert(fp)["status"] == "resolved"
    await service.close()


async def test_reconcile_query_failure_is_safe(tmp_path) -> None:
    service, store = _make_service(tmp_path)
    fp = _seed_firing(store, _alert("DiskUsageGrowthFast"))
    service.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")))
    )
    await service.reconcile_alerts()
    assert store.get_alert(fp)["status"] == "firing"
    await service.close()


def test_fingerprint_from_labels_matches_alert_fingerprint() -> None:
    alert = _alert("ServiceDown", "https://example.com/index")
    assert fingerprint_from_labels(alert.labels) == alert_fingerprint(alert)
