from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from control_plane.app import create_app
from control_plane.config import ControlPlaneConfig


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(
        ControlPlaneConfig(),
        api_key="secret",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        agent_session_dir=tmp_path / "data" / "agent-sessions",
        pid_file=tmp_path / "data" / "control-plane.pid",
        notification_enabled=False,
        prometheus_url="",
        alertmanager_url="",
    )


def test_live_and_ready(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        live = client.get("/live")
        assert live.status_code == 200
        body = live.json()
        assert body["status"] == "ok"
        assert "run_id" in body
        assert "pid" in body

        ready = client.get("/ready")
        assert ready.status_code == 200
        ready_body = ready.json()
        assert ready_body["status"] == "ok"
        assert ready_body["checks"]["database"]["ok"] is True
        assert ready_body["last_ready_at"] != "0"
        # each /ready proves DB writability by refreshing the timestamp
        assert app.state.store.get_setting("health:last_ready") == ready_body["last_ready_at"]


def test_ready_reports_degraded_when_prometheus_unreachable(tmp_path) -> None:
    config = replace(_config(tmp_path), prometheus_url="http://127.0.0.1:1")
    app = create_app(config)
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 503
        body = ready.json()
        assert body["status"] == "degraded"
        assert body["checks"]["prometheus"]["ok"] is False
        assert body["checks"]["database"]["ok"] is True


def test_sessions_inspect_lists_names_only(tmp_path) -> None:
    config = _config(tmp_path)
    session_dir = config.agent_session_dir
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "repair-x.jsonl").write_text(
        '{"data": {"api_key": "sk-live", "headers": {"authorization": "Bearer z"}}}\n',
        encoding="utf-8",
    )
    app = create_app(config)
    with TestClient(app) as client:
        response = client.get(
            "/v1/sessions/inspect",
            headers={"X-Control-Plane-Key": "secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "api_key" in body["sensitive_field_names"]
        assert "authorization" in body["sensitive_field_names"]
        assert body["count"] == 2
        assert "sk-live" not in response.text
        assert "Bearer z" not in response.text


def test_candidate_cleanup_dry_run_default(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        response = client.post(
            "/v1/candidates/cleanup",
            json={},
            headers={"X-Control-Plane-Key": "secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["dry_run"] is True
        assert body["branches"] == []


def test_new_endpoints_require_auth(tmp_path) -> None:
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        assert client.get("/v1/sessions/inspect").status_code == 401
        assert client.post("/v1/candidates/cleanup", json={}).status_code == 401


def test_pid_file_written_and_removed_on_startup_shutdown(tmp_path) -> None:
    config = _config(tmp_path)
    pid_file = config.pid_file
    app = create_app(config)
    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert pid_file.exists()
        pid = int(pid_file.read_text(encoding="ascii").strip())
        assert pid > 0
    assert not pid_file.exists()


def test_auth_failures_counter_increments_on_bad_key(tmp_path) -> None:
    from prometheus_client import REGISTRY


    config = _config(tmp_path)
    app = create_app(config)
    before = REGISTRY.get_sample_value(
        "control_plane_auth_failures_total",
        {"reason": "invalid_key", "endpoint": "/v1/tasks"},
    ) or 0.0
    with TestClient(app) as client:
        resp = client.post("/v1/tasks", json={}, headers={"x-control-plane-key": "wrong"})
        assert resp.status_code == 401
    after = REGISTRY.get_sample_value(
        "control_plane_auth_failures_total",
        {"reason": "invalid_key", "endpoint": "/v1/tasks"},
    ) or 0.0
    assert after == before + 1.0
