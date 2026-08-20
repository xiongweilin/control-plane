"""Trigger template — implements TriggerSource without importing any provider."""
from portable_runtime.core.models import new_id, utcnow
from portable_runtime.triggers.base import TriggerDescriptor, TriggerEmitter, TriggerEvent


class MyTrigger:
    """Copy this file to create a new trigger. Runtime discovers it via HTTP or code registration."""

    @property
    def descriptor(self) -> TriggerDescriptor:
        return TriggerDescriptor(id="my-trigger", name="My Trigger")

    async def start(self, emit: TriggerEmitter) -> None:
        self._emit = emit

    async def stop(self) -> None:
        pass

    async def emit_example(self, payload: dict) -> TriggerEvent:
        event = TriggerEvent(
            id=new_id("evt"),
            source=self.descriptor.id,
            kind="my-event",
            payload=payload,
            occurred_at=utcnow(),
        )
        await self._emit(event)
        return event
