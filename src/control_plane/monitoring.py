from __future__ import annotations

from typing import Any

import httpx
from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig


class PersonalMonitoringProvider:
    """Read-only Prometheus alert verification for the personal deployment."""

    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config
        self._descriptor = ProviderDescriptor(
            id="personal-monitoring",
            name="Personal Prometheus Monitoring",
            version="1.0.0",
            capabilities=["monitor.alert.active"],
            priority=20,
            tags={"personal-profile", "read-only", "verification"},
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        if not self.config.prometheus_url:
            return ProviderHealth(
                provider_id=self.descriptor.id,
                available=False,
                detail="prometheus_url is not configured",
            )
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.config.prometheus_url.rstrip('/')}/-/ready"
                )
            return ProviderHealth(
                provider_id=self.descriptor.id,
                available=response.status_code == 200,
                detail=f"prometheus ready status={response.status_code}",
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(
                provider_id=self.descriptor.id,
                available=False,
                detail=str(exc)[:500],
            )

    async def invoke(
        self,
        request: CapabilityRequest,
        context: InvocationContext,
    ) -> CapabilityResult:
        del context
        if request.capability != "monitor.alert.active":
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="unavailable",
                message=f"unsupported capability {request.capability}",
            )
        labels = request.parameters.get("labels", {})
        expected = dict(labels) if isinstance(labels, dict) else {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.config.prometheus_url.rstrip('/')}/api/v1/alerts"
                )
                response.raise_for_status()
                payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="failed",
                message=f"unable to verify Prometheus alert state: {exc}",
                metadata={"active": None},
            )

        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        raw_alerts = data.get("alerts", []) if isinstance(data, dict) else []
        matches = 0
        for raw in raw_alerts if isinstance(raw_alerts, list) else []:
            if not isinstance(raw, dict):
                continue
            current = raw.get("labels", {})
            if not isinstance(current, dict):
                continue
            if all(
                str(current.get(key, "")) == str(value)
                for key, value in expected.items()
                if value
            ):
                state = str(raw.get("state", "firing"))
                if state in {"firing", "pending"}:
                    matches += 1
        active = matches > 0
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message="alert remains active" if active else "alert is no longer active",
            metadata={"active": active, "matches": matches, "labels": expected},
        )

    async def cancel(self, request_id: str) -> None:
        del request_id
