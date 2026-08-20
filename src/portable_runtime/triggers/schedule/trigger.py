"""Schedule trigger replacing Windows Task Scheduler."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from .base import TriggerDescriptor, TriggerEmitter, TriggerEvent


class ScheduleTrigger:
    def __init__(  # noqa: E501
        self,
        trigger_id: str = "schedule",
        interval_seconds: float = 3600,
        kind: str = "maintenance-scan",
    ) -> None:
        self._descriptor = TriggerDescriptor(id=trigger_id, name="Schedule Trigger")
        self.interval_seconds = interval_seconds
        self.kind = kind
        self._task: asyncio.Task[None] | None = None
        self._emit: TriggerEmitter | None = None

    @property
    def descriptor(self) -> TriggerDescriptor:
        return self._descriptor

    async def start(self, emit: TriggerEmitter) -> None:
        self._emit = emit
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        self._emit = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            if self._emit is None:
                continue
            event = TriggerEvent(
                id=f"evt_schedule_{uuid.uuid4().hex[:8]}",
                source=self.descriptor.id,
                kind=self.kind,
                payload={"kind": self.kind},
                occurred_at=datetime.now(UTC),
            )
            await self._emit(event)

    async def emit_once(self) -> TriggerEvent:
        event = TriggerEvent(
            id=f"evt_schedule_{uuid.uuid4().hex[:8]}",
            source=self.descriptor.id,
            kind=self.kind,
            payload={"kind": self.kind},
            occurred_at=datetime.now(UTC),
        )
        if self._emit:
            await self._emit(event)
        return event
