from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(slots=True)
class PendingDecision:
    event: asyncio.Event = asyncio.Event()
    action: str | None = None


class ApprovalManager:
    """In-process decision registry; one pending decision per reference."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingDecision] = {}
        self._lock = asyncio.Lock()

    async def register(self, ref_id: str) -> None:
        async with self._lock:
            self._pending[ref_id] = PendingDecision()

    async def decide(self, ref_id: str, action: str) -> bool:
        async with self._lock:
            pending = self._pending.get(ref_id)
            if pending is None:
                return False
            pending.action = action
            pending.event.set()
            return True

    async def wait(self, ref_id: str) -> str | None:
        async with self._lock:
            pending = self._pending.get(ref_id)
        if pending is None:
            return None
        await pending.event.wait()
        return pending.action

    async def remove(self, ref_id: str) -> None:
        async with self._lock:
            self._pending.pop(ref_id, None)
