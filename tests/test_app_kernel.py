import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
)
from portable_runtime.core.models import Event
from portable_runtime.responsibility import ResponsibilityKernel

import control_plane.app as app_module
from control_plane.app import (
    ALERT_ESCALATED_EVENT,
    ALERT_QUEUED_EVENT,
    _split_controller_reply,
    create_app,
)
from control_plane.codex_boundary import ThreadIsolatedCodexProvider
from control_plane.config import ControlPlaneConfig
from control_plane.kernel_bridge import PersonalKernelBridge


def make_config(tmp_path: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig(
        api_key="test-key",
        state_db=tmp_path / "kernel.db",
        artifact_root=tmp_path / "artifacts",
        agent_session_dir=tmp_path / "sessions",
        codex_worktree_root=tmp_path / "worktrees",
        prometheus_url="",
        alertmanager_url="",
        notification_enabled=False,
        game_mode_enabled=False,
        allowed_repo_roots=(str(tmp_path),),
        project_dirs={"test": str(tmp_path)},
        allowed_auto_projects=("test",),
    )


def test_app_is_thin_agent_kernel_profile(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    runtime = app.state.kernel_runtime
    assert runtime.runtime_id == "personal-platform"
    provider_ids = {item.id for item in runtime.registry.list()}
    assert "codex-primary" in provider_ids
    assert "feishu-human" in provider_ids
    assert "personal-monitoring" in provider_ids
    assert "personal-operations" in provider_ids
    assert "environment-inspection" in provider_ids
    assert app.state.controller.runtime is runtime
    assert callable(app.state.controller.step)
    assert isinstance(app.state.kernel_bridge, PersonalKernelBridge)
    assert app.state.kernel_bridge.runtime is runtime
    assert isinstance(app.state.kernel_bridge.responsibilities, ResponsibilityKernel)


def test_personal_effect_rules_are_kernel_owned(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    registry = app.state.kernel_runtime.contract_registry
    expected = {
        "shell.exec": ("write-local", False, True, False, 1, 1),
        "notify.send": ("write-remote", False, False, False, 1, 1),
        "git.merge": ("write-local", True, True, True, 1, 1),
        "git.push": ("write-remote", True, True, True, 2, 2),
        "git.fast_forward": ("write-local", False, True, True, 1, 1),
        "git.push_exact_ref": ("write-remote", False, True, True, 2, 2),
        "git.discard_line_ending_changes": ("write-local", False, True, False, 1, 1),
        "chezmoi.apply": ("write-local", False, True, True, 1, 1),
        "git.rollback": ("write-local", True, True, True, 2, 2),
        "docker.restart": ("write-remote", True, True, False, 2, 2),
        "docker.compose.up": ("write-remote", False, False, False, 3, 3),
        "maintenance.cleanup_known_garbage": ("write-local", False, False, False, 1, 1),
    }
    for capability, values in expected.items():
        rule = registry.effect_rule(capability)
        assert (
            rule.impact_class,
            rule.authorization_required,
            rule.resource_required,
            rule.version_required,
            rule.blast_radius,
            rule.exposure,
        ) == values


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["diagnosis", "execution"])
async def test_codex_boundary_promotes_phase_timeout_to_request_deadline(phase: str) -> None:
    class RecordingProvider:
        def __init__(self) -> None:
            self.request = None

        async def invoke(self, request, context):
            del context
            self.request = request
            return CapabilityResult(
                request_id=request.id,
                provider_id="test",
                status="succeeded",
                message="ok",
            )

    provider = RecordingProvider()
    boundary = ThreadIsolatedCodexProvider(provider)  # type: ignore[arg-type]
    request = CapabilityRequest(
        id=f"request:{phase}",
        capability="reason.generate",
        parameters={"phase": phase, "timeout_seconds": 900.0},
    )

    result = await boundary.invoke(request, InvocationContext(runtime_id="test"))

    assert result.status == "succeeded"
    assert provider.request is not None
    assert provider.request.timeout_seconds == 900.0


def test_personal_task_and_waiting_command_surfaces_are_unchanged(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    paths = {route.path for route in app.routes}
    assert "/healthz" in paths
    assert "/live" in paths
    assert "/ready" in paths
    assert "/metrics" in paths
    assert "/status" in paths
    assert "/v1/runtime" in paths
    assert "/v1/tasks" in paths
    assert "/v1/controllers/{controller_id}/command" in paths
    assert "/v1/alerts/alertmanager" in paths
    assert "/v1/game-mode" in paths
    assert "/v1/sessions/inspect" in paths


def test_feishu_task_reply_can_address_waiting_controller() -> None:
    assert _split_controller_reply("controller_abc restart prometheus") == (
        "controller_abc",
        "restart prometheus",
    )
    assert _split_controller_reply("ordinary task") is None


def test_profile_models_remain_existing_luna_names(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.diagnosis_model == "gpt-5.6-luna"
    assert cfg.execution_model == "gpt-5.6-luna"


@pytest.mark.asyncio
async def test_ready_blocks_when_a_kernel_provider_is_unavailable(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))

    async def unhealthy_runtime() -> dict[str, object]:
        return {
            "runtime_id": "personal-platform",
            "providers": [
                {"provider_id": "codex-primary", "available": False},
                {"provider_id": "personal-monitoring", "available": True},
            ],
        }

    app.state.kernel_runtime.health = unhealthy_runtime  # type: ignore[method-assign]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["unavailable_providers"] == ["codex-primary"]


@pytest.mark.asyncio
async def test_ready_is_ok_when_checks_and_kernel_providers_are_healthy(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))

    async def healthy_runtime() -> dict[str, object]:
        return {
            "runtime_id": "personal-platform",
            "providers": [{"provider_id": "codex-primary", "available": True}],
        }

    app.state.kernel_runtime.health = healthy_runtime  # type: ignore[method-assign]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_treats_game_mode_monitoring_shutdown_as_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(
        make_config(tmp_path),
        prometheus_url="http://127.0.0.1:1",
        alertmanager_url="http://127.0.0.1:1",
        game_mode_enabled=True,
    )
    app = create_app(cfg)
    monkeypatch.setattr(
        app_module,
        "read_game_mode_state",
        lambda *args, **kwargs: SimpleNamespace(suppress_alerts=True),
    )

    async def game_mode_runtime() -> dict[str, object]:
        return {
            "runtime_id": "personal-platform",
            "providers": [
                {"provider_id": "codex-primary", "available": True},
                {"provider_id": "personal-monitoring", "available": False},
            ],
        }

    app.state.kernel_runtime.health = game_mode_runtime  # type: ignore[method-assign]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["expected_degraded"]["reason"] == "game_mode_active"
    assert body["checks"]["prometheus"]["expected_down"] is True
    assert body["checks"]["alertmanager"]["expected_down"] is True


@pytest.mark.asyncio
async def test_metrics_refreshes_read_only_environment_provider(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    app.state.environment_provider._probe_runner = lambda: {"docker_available": False}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
        await asyncio.sleep(1)
        response = await client.get("/metrics")

    assert response.status_code == 200
    text = response.text
    assert "control_plane_environment_last_check_timestamp_seconds" in text
    assert 'check="docker_exited_containers"' in text
    assert 'status="unknown"' in text


@pytest.mark.asyncio
async def test_alert_ingress_is_deduplicated_and_does_not_wait_for_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(make_config(tmp_path))

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        await asyncio.sleep(0.05)
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "stable-fingerprint",
                "labels": {"alertname": "PrometheusScrapeFailed", "job": "control-plane"},
                "annotations": {"summary": "probe failed"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )
        second = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert first.status_code == 200
    assert first.json()["queued"] == 1
    assert first.json()["deduplicated"] == 0
    assert second.status_code == 200
    assert second.json()["queued"] == 0
    assert second.json()["deduplicated"] == 1
    assert second.json()["controllers"] == first.json()["controllers"]

    queued = [
        event
        for event in app.state.kernel_runtime.store.list_events()
        if event.type == ALERT_QUEUED_EVENT
    ]
    assert len(queued) == 1
    assert "fail_safe" not in queued[0].payload
    assert "repair_deadline_at" not in queued[0].payload
    assert queued[0].payload["title"] == "Alert: PrometheusScrapeFailed"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_any_project_alert_enters_the_auto_repair_scope_without_name_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(replace(make_config(tmp_path), automatic_handling_enabled=True))

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "arbitrary-project-alert",
                "labels": {
                    "alertname": "ArbitraryOperationalAlert",
                    "project": "test",
                },
                "annotations": {"summary": "operational drift"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert response.status_code == 200
    queued = [
        event
        for event in app.state.kernel_runtime.store.list_events()
        if event.type == ALERT_QUEUED_EVENT
    ]
    assert len(queued) == 1
    assert queued[0].payload["project"] == "test"
    assert queued[0].payload["repo"] == str(tmp_path)
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_sync_alert_without_project_targets_only_the_exact_line_ending_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = replace(
        make_config(tmp_path),
        automatic_handling_enabled=True,
        allowed_auto_projects=("ratio",),
        project_dirs={"ratio": str(tmp_path)},
        line_ending_auto_discard_repos=(str(tmp_path),),
    )
    app = create_app(cfg)

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "ratio-line-ending-noise",
                "labels": {"alertname": "ControlPlaneSynchronizationDegraded"},
                "annotations": {"summary": "ratio worktree drift"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert response.status_code == 200
    queued = [
        event
        for event in app.state.kernel_runtime.store.list_events()
        if event.type == ALERT_QUEUED_EVENT
    ]
    assert len(queued) == 1
    assert queued[0].payload["project"] == "ratio"
    assert queued[0].payload["repo"] == str(tmp_path)
    assert queued[0].payload["maintenance_capability"] == "git.discard_line_ending_changes"
    assert queued[0].payload["maintenance_parameters"] == {
        "repo": str(tmp_path),
        "project": "ratio",
    }
    await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("notification_status", "expected_sent"),
    [("succeeded", True), ("failed", False)],
)
@pytest.mark.asyncio
async def test_first_round_irreversible_blocker_escalates_through_feishu(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    notification_status: str,
    expected_sent: bool,
) -> None:
    app = create_app(replace(make_config(tmp_path), notification_enabled=True))
    diagnosis_calls = []
    notification_calls = []

    async def fake_invoke(request):
        diagnosis_calls.append(request)
        assert request.capability == "reason.generate"
        return CapabilityResult(
            request_id=request.id,
            provider_id="test",
            status="succeeded",
            message="SAFETY_CLASS=IRREVERSIBLE\nowner decision required",
        )

    async def fake_run_capability(work_id, capability, **kwargs):
        notification_calls.append((work_id, capability, kwargs))
        assert capability == "notify.send"
        return CapabilityResult(
            request_id=f"request:notify:{len(notification_calls)}",
            provider_id="test",
            status=notification_status,
            message="sent",
        )

    monkeypatch.setattr(app.state.kernel_runtime, "invoke", fake_invoke)
    monkeypatch.setattr(app.state.kernel_runtime, "run_capability", fake_run_capability)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "irreversible-feishu-escalation",
                "labels": {"alertname": "IrreversibleRepairDetected"},
                "annotations": {"summary": "requires an owner decision"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert response.status_code == 200
    for _ in range(20):
        escalated = [
            event
            for event in app.state.kernel_runtime.store.list_events()
            if event.type == ALERT_ESCALATED_EVENT
        ]
        if escalated:
            break
        await asyncio.sleep(0.01)

    assert [request.capability for request in diagnosis_calls] == ["reason.generate"]
    assert [capability for _work_id, capability, _kwargs in notification_calls] == [
        "notify.send"
    ]
    assert len(escalated) == 1
    assert escalated[0].payload["reason"] == "irreversible"
    assert escalated[0].payload["notification_sent"] is expected_sent
    assert escalated[0].payload["diagnosis_attempts"] == 1
    assert escalated[0].payload["execution_attempts"] == 1
    assert diagnosis_calls[0].parameters["phase"] == "diagnosis"
    assert "首轮" in notification_calls[0][2]["instruction"]


@pytest.mark.asyncio
async def test_active_codex_timeout_alert_keeps_one_controller_after_a_real_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(make_config(tmp_path))

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "timeout-fingerprint",
                "labels": {"alertname": "UnexpectedListenerDetected", "job": "node"},
                "annotations": {"summary": "probe timed out"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )
        first_controller = first.json()["controllers"][0]
        await asyncio.sleep(0.05)
        app.state.kernel_runtime.store.append_event(
            Event(
                id="event_codex_timeout",
                type="ControllerCapabilityResultObserved",
                subject_ref=first_controller,
                payload={
                    "result": {
                        "status": "failed",
                        "error": {"type": "timeout", "message": "bounded timeout"},
                    }
                },
            )
        )
        second = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["queued"] == 0
    assert second.json()["deduplicated"] == 1
    assert second.json()["controllers"] == [first_controller]
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_restarted_diagnosis_failure_keeps_one_controller_after_a_real_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(make_config(tmp_path))

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "restarted-diagnosis-failure",
                "labels": {"alertname": "UnexpectedListenerDetected", "job": "node"},
                "annotations": {"summary": "restarted diagnosis failed"},
            }
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )
        first_controller = first.json()["controllers"][0]
        await asyncio.sleep(0.05)
        app.state.kernel_runtime.store.append_event(
            Event(
                id="event_restarted_diagnosis_failure",
                type="ControllerRevisionAssessed",
                subject_ref=first_controller,
                payload={
                    "revision": {"failure_class": "diagnosis-failure"},
                },
            )
        )
        second = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["queued"] == 0
    assert second.json()["deduplicated"] == 1
    assert second.json()["controllers"] == [first_controller]
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_game_mode_only_suppresses_explicit_docker_and_scrape_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(replace(make_config(tmp_path), game_mode_enabled=True))

    async def fake_drive(controller: object, controller_id: str, policy: object) -> object:
        del policy
        return controller.get(controller_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "drive_policy", fake_drive)
    monkeypatch.setattr(
        app_module,
        "read_game_mode_state",
        lambda *args, **kwargs: SimpleNamespace(suppress_alerts=True),
    )
    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "game-mode-scrape",
                "labels": {"alertname": "PrometheusScrapeFailed", "job": "node"},
                "annotations": {"summary": "scrape failed"},
            },
            {
                "status": "firing",
                "fingerprint": "game-mode-listener",
                "labels": {"alertname": "UnexpectedListenerDetected", "job": "node"},
                "annotations": {"summary": "listener detected"},
            },
        ]
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/alerts/alertmanager",
            json=payload,
            headers={"X-Control-Plane-Key": "test-key"},
        )

    assert response.status_code == 200
    assert response.json()["suppressed"] == 1
    assert response.json()["queued"] == 1
    await asyncio.sleep(0.05)
