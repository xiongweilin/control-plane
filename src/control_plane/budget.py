from __future__ import annotations

import datetime as dt

from .storage import Store


class Budget:
    def __init__(self, store: Store, daily_limit: int, per_repair_limit: int) -> None:
        self._store = store
        self.daily_limit = daily_limit
        self.per_repair_limit = per_repair_limit

    @staticmethod
    def _today() -> str:
        return dt.date.today().isoformat()

    def remaining(self, date: str | None = None) -> int:
        day = date or self._today()
        used = self._store.budget_calls(day)
        return max(0, self.daily_limit - used)

    def can_spend(self, date: str | None = None) -> bool:
        return self.remaining(date) > 0

    def spend(self, amount: int = 1, date: str | None = None) -> bool:
        day = date or self._today()
        used = self._store.budget_calls(day)
        if used + amount > self.daily_limit:
            return False
        self._store.add_budget_calls(day, amount)
        return True

    def can_start_repair(self, repair_agent_calls: int = 0) -> bool:
        return repair_agent_calls <= self.per_repair_limit and self.can_spend()
