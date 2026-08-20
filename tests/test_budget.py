from __future__ import annotations

from control_plane.budget import Budget
from control_plane.storage import Store


def test_budget_enforces_daily_limit(tmp_path) -> None:
    store = Store(tmp_path / "state.db")
    budget = Budget(store, daily_limit=2, per_repair_limit=5)
    assert budget.can_spend()
    assert budget.spend()
    assert budget.spend()
    assert not budget.can_spend()
    assert not budget.spend()
    assert budget.remaining() == 0
    store.close()


def test_budget_per_repair_limit(tmp_path) -> None:
    store = Store(tmp_path / "state.db")
    budget = Budget(store, daily_limit=100, per_repair_limit=3)
    assert budget.can_start_repair(3)
    assert not budget.can_start_repair(4)
    store.close()
