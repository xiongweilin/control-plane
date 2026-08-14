from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from control_plane.alerts import alert_fingerprint
from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.dsh_runner import DshSessionResult
from control_plane.models import AlertmanagerPayload
from control_plane.notify import Notifier
from control_plane.service import RepairService
from control_plane.storage import Store
from control_plane.tools import ToolError


class FakeExecutor:
    """Mirror of the repair-flow fake: git + docker responses, records calls."""

    def __init__(self, branch_exists: bool = False) -> None:
        self.branch_exists = branch_exists
        self.current_branch = "main"
        self.calls: list[tuple[list[str], str | None]] = []

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append((args, cwd))
        joined = " ".join(args)
        if "rev-parse --is-inside-work-tree" in joined:
            return "true"
        if "rev-parse HEAD" in joined:
            return "abc123"
        if "symbolic-ref --quiet --short HEAD" in joined:
            return self.current_branch
        if " switch " in f" {joined} ":
            self.current_branch = args[-1]
            return ""
        if "docker ps" in joined:
            return "Up 2 minutes"
        if "diff --stat main...fix/control-plane-" in joined or (
            "fix/control-plane-" in joined and "diff --stat main" in joined
        ):
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "diff --name-only main...fix/control-plane-" in joined:
            return "a.txt\n" if self.branch_exists else ""
        if "rev-parse --short fix/control-plane-" in joined:
            return "c0ffee\n" if self.branch_exists else ""
        if "status --porcelain" in joined:
            return ""
        if "rev-parse --verify fix/control-plane-" in joined:
            if self.branch_exists:
                return "abc123\n"
            raise ToolError("branch missing")
        if "merge --ff-only" in joined:
            return ""
        if "merge -q" in joined:
            return ""
        if "push" in joined:
            return ""
        return ""


class FakeCodexRunner:
    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
    ) -> DshSessionResult:
        return DshSessionResult(exit_code=0, last_message=f"fixed {repair_id}")


def _config(tmp_path) -> ControlPlaneConfig:
    (tmp_path / "dify").mkdir(parents=True, exist_ok=True)
    return replace(
        ControlPlaneConfig(),
        api_key="test-key",
        run_id="run-restart-1",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        pid_file=tmp_path / "data" / "control-plane.pid",
        notification_enabled=False,
        model_preflight_enabled=False,
        allowed_auto_projects=("dify",),
        allowed_repo_roots=(str(tmp_path / "repos"),),
        project_dirs={"dify": str(tmp_path / "dify")},
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
                    "fingerprint": "fp-restart",
                }
            ],
        }
    )


def _make_service(config: ControlPlaneConfig, store: Store, executor: FakeExecutor) -> RepairService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    return RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeCodexRunner(),
        ApprovalManager(),
        Notifier(config),
        executor=executor,
        http=http,
    )


def _seed_pending_repair(store: Store, repair_id: str, branch_exists: bool = True) -> None:
    payload = json.dumps(_payload().alerts[0].model_dump(mode="json"), ensure_ascii=False)
    store.create_repair(repair_id, "fp-restart", payload, 1)
    store.set_repair_status(repair_id, "needs_approval")
    store.add_action(
        f"act-{repair_id}",
        repair_id,
        "dsh_agent",
        "C:\\tmp\\repos",
        "needs_approval",
        before={"repo": "C:\\tmp\\repos", "git_head": "abc", "git_ref": "main"},
        after={
            "branch": f"fix/control-plane-{repair_id}",
            "diff_stat": "a.txt | 1 +",
            "summary": "candidate from before restart",
        },
    )


@pytest.mark.asyncio
async def test_resume_pending_approval_reject_after_restart(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    store = Store(config.state_db)
    repair_id = "repair-aaaa0001"
    _seed_pending_repair(store, repair_id)

    executor = FakeExecutor(branch_exists=True)
    service = _make_service(config, store, executor)
    # simulate restart: a brand-new service instance resumes the pending approval
    resume_task = asyncio.create_task(service.resume_pending_approval(repair_id))
    await asyncio.sleep(0.2)
    assert store.get_repair(repair_id)["status"] == "needs_approval"
    await service.approvals.decide(repair_id, "reject")
    await asyncio.wait_for(resume_task, timeout=10)
    row = store.get_repair(repair_id)
    assert row["status"] == "closed"
    assert row["result"] == "rejected"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_resume_pending_approval_approve_finishes_repair(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    store = Store(config.state_db)
    repair_id = "repair-bbbb0002"
    _seed_pending_repair(store, repair_id)

    executor = FakeExecutor(branch_exists=True)
    service = _make_service(config, store, executor)
    resume_task = asyncio.create_task(service.resume_pending_approval(repair_id))
    await asyncio.sleep(0.2)
    store.add_approval(
        f"ap-{repair_id}",
        "repair",
        repair_id,
        "approve",
        "feishu",
        "ok",
    )
    await service.approvals.decide(repair_id, "approve")
    await asyncio.wait_for(resume_task, timeout=10)
    row = store.get_repair(repair_id)
    assert row["status"] == "closed"
    assert any("merge --ff-only" in " ".join(args) for args, _ in executor.calls)
    # approval decision persisted for audit
    assert store.get_approval("repair", repair_id, "approve") is not None
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_approval_resumed_via_api_after_restart(tmp_path) -> None:
    """End-to-end: seeded needs_approval repair survives app restart and is decided via API."""
    from fastapi.testclient import TestClient

    from control_plane.app import create_app

    config = _config(tmp_path)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    store = Store(config.state_db)
    repair_id = "repair-cccc0003"
    _seed_pending_repair(store, repair_id)
    store.close()

    app = create_app(config)
    with TestClient(app) as client:
        headers = {"X-Control-Plane-Key": "test-key"}
        # restart reconciliation keeps needs_approval and re-registers the waiter
        assert app.state.store.get_repair(repair_id)["status"] == "needs_approval"
        response = client.post(
            f"/v1/approvals/{repair_id}/decision",
            json={"action": "reject", "decided_by": "feishu", "note": "restart test"},
            headers=headers,
        )
        assert response.json()["accepted"] is True
        for _ in range(100):
            if app.state.store.get_repair(repair_id)["status"] == "closed":
                break
            time.sleep(0.05)
        assert app.state.store.get_repair(repair_id)["result"] == "rejected"
    app.state.store.close()


@pytest.mark.asyncio
async def test_stale_repairs_marked_interrupted_on_restart(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from control_plane.app import create_app

    config = _config(tmp_path)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    store = Store(config.state_db)
    store.create_repair("repair-stale-1", "fp-x", "{}", 1)
    store.set_repair_status("repair-stale-1", "proposing")
    store.close()

    app = create_app(config)
    with TestClient(app):
        assert app.state.store.get_repair("repair-stale-1")["status"] == "interrupted"
    app.state.store.close()


@pytest.mark.asyncio
async def test_lease_blocks_second_instance_start(tmp_path) -> None:
    config = _config(tmp_path)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    store = Store(config.state_db)
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    # another instance already holds the lease
    store.create_repair("repair-other", fingerprint, "{}", 1)
    store.set_repair_status("repair-other", "failed", finished_at=int(time.time()))
    assert store.acquire_lease(fingerprint, "run-other", "repair-other", 900) is True

    service = _make_service(config, store, FakeExecutor())
    result = await service._start_repair(payload.alerts[0], 1)
    assert result.get("deduplicated") == 1
    assert service.run_id != "run-other"
    # the lease holder's repair must be untouched; the blocked repair is interrupted
    blocked = [row for row in store.list_repairs() if row["id"] != "repair-other"]
    assert blocked and blocked[0]["status"] == "interrupted"
    assert "lease" in blocked[0]["error"]
    await service.close()
    store.close()
