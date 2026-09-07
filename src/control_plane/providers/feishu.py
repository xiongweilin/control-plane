"""Control-plane-owned Feishu capability providers.

The Agent Kernel owns provider contracts, routing, and the reality boundary.
Feishu is a personal-platform integration, so its concrete human and
notification providers belong to this profile instead of portable-runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)


class FeishuHumanProvider:
    """Implements the profile's human.review / human.approve capabilities."""

    def __init__(self, provider_id: str = "feishu-human") -> None:
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name="Feishu Human Provider",
            version="1.0.0",
            capabilities=["human.review", "human.approve"],
            tags={"human"},
            priority=5,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self.descriptor.id,
            available=True,
            detail="feishu human ready (stub)",
        )

    async def invoke(
        self, request: CapabilityRequest, context: InvocationContext
    ) -> CapabilityResult:
        del context
        if request.capability == "human.approve":
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="needs-input",
                message="approval required via Feishu or CLI",
            )
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=f"reviewed: {request.instruction}",
        )

    async def cancel(self, request_id: str) -> None:
        del request_id


class FeishuNotificationProvider:
    """Send profile notifications through the local Feishu script.

    A successful script exit proves only that the provider accepted the
    request. The legacy script has no Feishu message id or delivery receipt,
    so this provider never claims delivery confirmation.
    """

    def __init__(self, provider_id: str = "feishu-notify") -> None:
        self._descriptor = ProviderDescriptor(
            id=provider_id,
            name="Feishu Notification Provider",
            version="1.0.0",
            capabilities=["notify.send"],
            tags={"notify"},
            priority=5,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        script = Path.home() / ".local" / "bin" / "feishu-notify.ps1"
        available = script.is_file()
        return ProviderHealth(
            provider_id=self.descriptor.id,
            available=available,
            detail=(
                "notify provider registered; delivery is not confirmed"
                if available
                else f"notification script not found: {script}"
            ),
        )

    def _result(
        self,
        request: CapabilityRequest,
        *,
        status: str,
        provider_accepted: bool | None,
        message: str,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        result_metadata: dict[str, Any] = {
            "provider_accepted": provider_accepted,
            "delivery_confirmed": False,
            "delivery_confirmation": "not_available",
        }
        if metadata:
            result_metadata.update(metadata)
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status=status,
            message=message,
            error=error,
            metadata=result_metadata,
        )

    @staticmethod
    async def _terminate_process(process: Any) -> None:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()

    async def invoke(
        self, request: CapabilityRequest, context: InvocationContext
    ) -> CapabilityResult:
        del context
        script = Path.home() / ".local" / "bin" / "feishu-notify.ps1"
        if not script.is_file():
            return self._result(
                request,
                status="unavailable",
                provider_accepted=False,
                message="Feishu notification script is unavailable",
                error={
                    "code": "feishu_script_missing",
                    "type": "script_missing",
                    "message": f"notification script not found: {script}",
                },
                metadata={"failure_phase": "script_lookup"},
            )

        timeout_seconds = (
            request.timeout_seconds if request.timeout_seconds is not None else 10.0
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe",
                "-File",
                str(script),
                request.instruction or "",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            return self._result(
                request,
                status="failed",
                provider_accepted=False,
                message="Feishu notification script failed to start",
                error={
                    "code": "feishu_script_start_failed",
                    "type": "script_start_failed",
                    "message": str(exc)[:500] or type(exc).__name__,
                },
                metadata={"failure_phase": "script_start"},
            )

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except TimeoutError:
            await self._terminate_process(proc)
            return self._result(
                request,
                status="unknown",
                provider_accepted=None,
                message="Feishu notification script timed out; delivery is unknown",
                error={
                    "code": "feishu_script_timeout",
                    "type": "timeout",
                    "message": f"notification script timed out after {timeout_seconds:g}s",
                },
                metadata={
                    "failure_phase": "script_wait",
                    "timeout_seconds": timeout_seconds,
                },
            )
        except Exception as exc:
            await self._terminate_process(proc)
            return self._result(
                request,
                status="unknown",
                provider_accepted=None,
                message="Feishu notification script result could not be observed",
                error={
                    "code": "feishu_script_wait_failed",
                    "type": "script_wait_failed",
                    "message": str(exc)[:500] or type(exc).__name__,
                },
                metadata={"failure_phase": "script_wait"},
            )

        exit_code = proc.returncode
        if exit_code != 0:
            return self._result(
                request,
                status="failed",
                provider_accepted=False,
                message=f"Feishu notification script exited with code {exit_code}",
                error={
                    "code": "feishu_script_exit_nonzero",
                    "type": "script_exit",
                    "exit_code": exit_code,
                    "message": f"notification script exited with code {exit_code}",
                },
                metadata={"failure_phase": "script_exit", "exit_code": exit_code},
            )

        return self._result(
            request,
            status="succeeded",
            provider_accepted=True,
            message="Feishu notification provider accepted the request; delivery is not confirmed",
            metadata={"script_exit_code": 0},
        )

    async def cancel(self, request_id: str) -> None:
        del request_id
