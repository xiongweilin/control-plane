from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.codex_runner import CodexSessionResult
from control_plane.config import ControlPlaneConfig
from control_plane.models import AlertmanagerPayload
from control_plane.notify import Notifier
from control_plane.service import RepairService
from control_plane.storage import Store
from control_plane.tools import ToolError


class FakeExecutor:
    def __init__(self, branch_exists: bool = False) -> None:
        self.branch_exists = branch_exists
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> str:
        self.calls.append((args, cwd))
        joined = " ".join(args)
        if joined == "test -d /srv/stack/dify":
            return ""
        if "git rev-parse --short HEAD" in joined:
            return "abc123"
        if "docker ps" in joined:
            return "Up 2 minutes\nUp 5 minutes"
        if "fix/control-plane-" in joined and "git diff --stat main" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "git diff main...fix/control-plane-" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "git status --porcelain" in joined:
            return ""
        if "git rev-parse --verify fix/control-plane-" in joined:
            if self.branch_exists:
                return "abc123\n"
            raise ToolError("branch missing")
        return ""


class FakeCodexRunner:
    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.calls = 0

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
    ) -> CodexSessionResult:
        self.calls += 1
        return CodexSessionResult(
            exit_code=self.exit_code,
            last_message=f"fixed {repair_id}",
        )


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(
        ControlPlaneConfig(),
        api_key="test-key",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        allowed_auto_projects=("dify",),
        allowed_repo_roots=("/srv/stack",),
        max_agent_calls_per_repair=8,
        candidate_wip_limit=10,
    )


def _payload() -> AlertmanagerPayload:
    return AlertmanagerPayload.model_validate(
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "instance": "node1", "project": "dify"},
                    "annotations": {"summary": "cpu high"},
                    "startsAt": "2026-08-06T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "fp-1",
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_repair_flow_with_approval(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor(branch_exists=True)
    agent = FakeCodexRunner()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "success"}))
    http = httpx.AsyncClient(transport=transport)
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        agent,
        approvals,
        Notifier(config),
        executor=executor,
        http=http,
    )
    response = await service.ingest(_payload())
    assert response.accepted == 1

    repair_id = store.list_repairs()[0]["id"]
    for _ in range(100):
        row = store.get_repair(repair_id)
        if row["status"] == "needs_approval":
            break
        await asyncio.sleep(0.05)
    assert store.get_repair(repair_id)["status"] == "needs_approval"

    await approvals.decide(repair_id, "approve")
    for _ in range(200):
        row = store.get_repair(repair_id)
        if row["status"] == "closed":
            break
        await asyncio.sleep(0.05)
    assert store.get_repair(repair_id)["status"] == "closed"

    candidates = store.list_candidates("candidate")
    assert len(candidates) == 1
    assert candidates[0]["pattern"] == "HighCPU|dify|*"
    assert store.list_actions(repair_id)[0]["tool"] == "codex_agent"
    assert any("git merge" in " ".join(args) for args, _ in executor.calls)
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_reject_closes_repair_without_candidate(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor(branch_exists=True)
    agent = FakeCodexRunner()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        agent,
        approvals,
        Notifier(config),
        executor=executor,
        http=http,
    )
    await service.ingest(_payload())
    repair_id = store.list_repairs()[0]["id"]
    for _ in range(100):
        if store.get_repair(repair_id)["status"] == "needs_approval":
            break
        await asyncio.sleep(0.05)
    await approvals.decide(repair_id, "reject")
    for _ in range(200):
        if store.get_repair(repair_id)["status"] == "closed":
            break
        await asyncio.sleep(0.05)
    assert store.get_repair(repair_id)["status"] == "closed"
    assert store.get_repair(repair_id)["result"] == "rejected"
    assert not store.list_candidates("candidate")
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_operations_only_repair_closes_without_approval(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor(branch_exists=False)
    agent = FakeCodexRunner()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        agent,
        approvals,
        Notifier(config),
        executor=executor,
        http=http,
    )
    response = await service.ingest(_payload())
    assert response.accepted == 1
    repair_id = store.list_repairs()[0]["id"]
    for _ in range(200):
        if store.get_repair(repair_id)["status"] == "closed":
            break
        await asyncio.sleep(0.05)
    assert store.get_repair(repair_id)["status"] == "closed"
    assert len(store.list_candidates("candidate")) == 1
    await service.close()
    store.close()
