from __future__ import annotations

import asyncio
import time

import httpx

from control_plane.alerts import fingerprint_pattern
from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.dsh_runner import DshRunner
from control_plane.models import Alert
from control_plane.notify import Notifier
from control_plane.service import RepairService, recovery_retry_failed, repairs_skipped_dirty
from control_plane.storage import Store


def _unknown_label() -> str:
    """The label the code uses when a repair row has no usable payload."""
    alert = Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "unknown"},
            "annotations": {},
            "startsAt": "2026-01-01T00:00:00Z",
            "endsAt": None,
            "fingerprint": "unknown",
        }
    )
    return fingerprint_pattern(alert)


def _counter(counter, pattern: str) -> float:
    from prometheus_client import REGISTRY

    # prometheus_client Counter 的注册名是 <base>_total
    return REGISTRY.get_sample_value(counter._name + "_total", {"pattern": pattern}) or 0.0


async def _record(service, store, repair_id, fingerprint, exc_text: str) -> None:
    await service._record_failure(repair_id, fingerprint, RuntimeError(exc_text))


def test_recovery_retry_failed_only_counts_real_failures(tmp_path) -> None:
    """Dirty-worktree guard and exec timeout must not increment recovery_retry_failed."""
    from tests.test_repair_flow import _config

    config = _config(tmp_path)
    store = Store(config.state_db)
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        DshRunner(config),
        ApprovalManager(),
        Notifier(config),
        executor=store,  # type: ignore[arg-type] - unused in _record_failure
        http=httpx.AsyncClient(),
    )
    fingerprint = "fp-recovery-classification"
    store.create_repair("r1", fingerprint, "{}", attempt=1)
    store.set_setting(f"attempt_reset:{fingerprint}", str(int(time.time())))

    label = _unknown_label()
    before_retry = _counter(recovery_retry_failed, label)
    before_skip = _counter(repairs_skipped_dirty, label)

    asyncio.run(
        _record(
            service,
            store,
            "r1",
            fingerprint,
            "workspace dirty; refusing to run agent (dirty_worktree_policy=reject): x",
        )
    )
    assert _counter(recovery_retry_failed, label) == before_retry
    assert _counter(repairs_skipped_dirty, label) == before_skip + 1

    asyncio.run(
        _record(
            service,
            store,
            "r1",
            fingerprint,
            "dsh agent timed out without a committed candidate [timeout_kind=exec]",
        )
    )
    assert _counter(recovery_retry_failed, label) == before_retry

    asyncio.run(_record(service, store, "r1", fingerprint, "Verification failed:\ncontainer_status: FAIL"))
    assert _counter(recovery_retry_failed, label) == before_retry + 1

    store.close()
