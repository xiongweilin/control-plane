from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig
from control_plane.models import AlertResponse
from control_plane.storage import Store


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(
        ControlPlaneConfig(),
        api_key="secret",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        model_preflight_enabled=False,
    )


def test_health_and_auth(tmp_path) -> None:
    client = TestClient(create_app(_config(tmp_path)))
    assert client.get("/healthz").json() == {"status": "ok"}
    response = client.post(
        "/v1/alerts/alertmanager",
        json={
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU"},
                    "annotations": {},
                    "startsAt": "2026-08-06T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "fp-x",
                }
            ],
        },
    )
    assert response.status_code == 401


def test_ingest_and_status(tmp_path, monkeypatch) -> None:
    app = create_app(_config(tmp_path))
    app.state.service.ingest = AsyncMock(  # type: ignore[attr-defined]
        return_value=AlertResponse(accepted=1, deduplicated=0, cooldown=0, budget_limited=0, paused=0)
    )
    client = TestClient(app)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU", "project": "dify"},
                "annotations": {},
                "startsAt": "2026-08-06T00:00:00Z",
                "endsAt": None,
                "fingerprint": "fp-1",
            }
        ],
    }
    response = client.post(
        "/v1/alerts/alertmanager",
        json=payload,
        headers={"X-Control-Plane-Key": "secret"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    status = client.get("/status", headers={"X-Control-Plane-Key": "secret"})
    assert status.status_code == 200
    assert "recent_repairs" in status.json()


def test_alertmanager_accepts_bearer_and_rejects_query_secret(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    app.state.service.ingest = AsyncMock(  # type: ignore[attr-defined]
        return_value=AlertResponse(
            accepted=1,
            deduplicated=0,
            cooldown=0,
            budget_limited=0,
            paused=0,
        )
    )
    client = TestClient(app)
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "HighCPU"},
                "annotations": {},
                "startsAt": "2026-08-06T00:00:00Z",
                "endsAt": None,
                "fingerprint": "fp-bearer",
            }
        ],
    }
    accepted = client.post(
        "/v1/alerts/alertmanager",
        json=payload,
        headers={"Authorization": "Bearer secret"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] == 1

    rejected = client.post(
        "/v1/alerts/alertmanager?api_key=secret",
        json=payload,
    )
    assert rejected.status_code == 401


def test_candidate_promotion_requires_approval_record(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    store: Store = app.state.store
    store.create_repair("repair-1", "HighCPU|dify|*", "{}")
    store.set_repair_status("repair-1", "closed", result="verified", finished_at=2**31)
    store.create_candidate(
        "cand-1",
        "HighCPU|dify|*",
        "control-plane",
        '[{"tool":"restart_service"}]',
        "container_status",
        "candidate",
        2**31,
        "archive",
        "",
        "",
        "repair-1",
    )
    client = TestClient(app)
    headers = {"X-Control-Plane-Key": "secret"}
    assert client.post(
        "/v1/candidates/cand-1/promote",
        json={"decided_by": "feishu", "note": "ok"},
        headers=headers,
    ).json()["accepted"] is True
    assert store.get_candidate("cand-1")["status"] == "official"
    portable_store = app.state.portable_runtime.store
    assert portable_store.list_authorizations()
    assert portable_store.export_state()["decision"]


def test_pause_resume(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    store: Store = app.state.store
    client = TestClient(app)
    headers = {"X-Control-Plane-Key": "secret"}
    assert client.post("/v1/control/pause", json={"reason": "test"}, headers=headers).json()["accepted"] is True
    assert store.get_setting("paused") == "1"
    assert client.post("/v1/control/resume", json={"reason": "test"}, headers=headers).json()["accepted"] is True
    assert store.get_setting("paused") == "0"
