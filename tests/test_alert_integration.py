from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from control_plane.app import ALERT_FINISHED_EVENT, ALERT_QUEUED_EVENT, create_app
from control_plane.config import ControlPlaneConfig


def _config(tmp_path: Path) -> ControlPlaneConfig:
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
        allowed_auto_projects=(),
    )


class _RecordingCodexProvider:
    def __init__(self) -> None:
        self.phases: list[str] = []
        self._descriptor = ProviderDescriptor(
            id="integration-codex",
            name="Integration Codex Provider",
            version="test",
            capabilities=["reason.generate"],
            priority=100,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True)

    async def invoke(
        self, request: CapabilityRequest, context: InvocationContext
    ) -> CapabilityResult:
        del context
        phase = str(request.parameters.get("phase", ""))
        self.phases.append(phase)
        message = (
            "SAFETY_CLASS=REVERSIBLE\nbounded diagnosis"
            if phase == "diagnosis"
            else "bounded execution result"
        )
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=message,
        )

    async def cancel(self, request_id: str) -> None:
        del request_id


class _InactiveAlertProvider:
    def __init__(self) -> None:
        self._descriptor = ProviderDescriptor(
            id="integration-monitor",
            name="Integration Alert Monitor",
            version="test",
            capabilities=["monitor.alert.active"],
            priority=100,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True)

    async def invoke(
        self, request: CapabilityRequest, context: InvocationContext
    ) -> CapabilityResult:
        del context
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message="alert is no longer active",
            metadata={"active": False, "matches": 0},
        )

    async def cancel(self, request_id: str) -> None:
        del request_id


@pytest.mark.asyncio
async def test_alert_ingress_runs_controller_codex_result_and_finished_event(
    tmp_path: Path,
) -> None:
    app = create_app(_config(tmp_path))
    runtime = app.state.kernel_runtime
    codex = _RecordingCodexProvider()
    runtime.registry.unregister("codex-primary")
    runtime.registry.register(codex)
    runtime.registry.unregister("personal-monitoring")
    runtime.registry.register(_InactiveAlertProvider())

    payload = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "integration-alert-1",
                "labels": {
                    "alertname": "IntegrationAlert",
                    "job": "control-plane",
                    "instance": "node-test",
                },
                "annotations": {
                    "summary": "integration summary",
                    "description": "integration description",
                    "detail": "integration detail",
                },
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
    assert response.json()["queued"] == 1

    finished = []
    for _ in range(100):
        events = runtime.store.list_events()
        finished = [event for event in events if event.type == ALERT_FINISHED_EVENT]
        if finished:
            break
        await asyncio.sleep(0.01)

    events = runtime.store.list_events()
    assert any(event.type == ALERT_QUEUED_EVENT for event in events)
    assert any(
        event.type == "ControllerDecisionSelected"
        and event.payload.get("decision", {}).get("capability") == "reason.generate"
        and event.payload.get("decision", {}).get("parameters", {}).get("phase")
        == "diagnosis"
        for event in events
    )
    assert any(event.type == "ControllerCapabilityResultObserved" for event in events)
    assert finished, "alert worker did not persist a finished event"
    assert finished[-1].payload["status"] == "closed"
    assert codex.phases == ["diagnosis", "execution"]
