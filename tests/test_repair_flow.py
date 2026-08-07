from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from control_plane.alerts import alert_fingerprint
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
        if "rev-parse --short HEAD" in joined:
            return "abc123"
        if "docker ps" in joined:
            return "Up 2 minutes\nUp 5 minutes"
        if "fix/control-plane-" in joined and "diff --stat main" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "diff main...fix/control-plane-" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "status --porcelain" in joined:
            return ""
        if "rev-parse --verify fix/control-plane-" in joined:
            if self.branch_exists:
                return "abc123\n"
            raise ToolError("branch missing")
        return ""


class FakeExecutorNoGit(FakeExecutor):
    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> str:
        if "rev-parse --is-inside-work-tree" in " ".join(args):
            raise ToolError("not a git repository")
        return await super().run(args, cwd=cwd, timeout=timeout, input_text=input_text)


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
    (tmp_path / "dify").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    return replace(
        ControlPlaneConfig(),
        api_key="test-key",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        allowed_auto_projects=("dify",),
        allowed_repo_roots=(str(tmp_path / "repos"),),
        project_dirs={"dify": str(tmp_path / "dify")},
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
async def test_noise_alert_ignored(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    payload = AlertmanagerPayload.model_validate(
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "AlertmanagerE2E", "instance": "am-e2e", "project": "dify"},
                    "annotations": {},
                    "startsAt": "2026-08-07T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "fp-noise-1",
                }
            ],
        }
    )
    response = await service.ingest(payload)
    assert response.ignored == 1
    assert response.accepted == 0
    assert executor.calls == []
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_smoke_instance_alert_ignored(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    payload = AlertmanagerPayload.model_validate(
        {
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighCPU", "instance": "smoke-node", "project": "dify"},
                    "annotations": {},
                    "startsAt": "2026-08-07T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "fp-noise-2",
                }
            ],
        }
    )
    response = await service.ingest(payload)
    assert response.ignored == 1
    assert response.accepted == 0
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_policy_ignore_skips_repair(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    await service.set_alert_policy(fingerprint, "ignore")
    response = await service.ingest(payload)
    assert response.ignored == 1
    assert response.accepted == 0
    assert executor.calls == []
    assert store.list_repairs() == []
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_policy_manual_pending_then_run(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    await service.set_alert_policy(fingerprint, "manual")
    response = await service.ingest(payload)
    assert response.pending == 1
    assert response.accepted == 0
    assert store.list_repairs() == []

    message = await service.run_manual(fingerprint)
    assert "已启动修复" in message
    for _ in range(200):
        rows = store.list_repairs()
        if rows and rows[0]["status"] in {"closed", "failed"}:
            break
        await asyncio.sleep(0.05)
    rows = store.list_repairs()
    assert rows and rows[0]["status"] == "closed"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_attempt_count_resets_after_resolution(tmp_path) -> None:
    config = replace(_config(tmp_path), max_attempts=1, cooldown_seconds=0)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])

    response = await service.ingest(payload)
    assert response.accepted == 1
    for _ in range(200):
        rows = store.list_repairs()
        if rows and rows[0]["status"] in {"closed", "failed"}:
            break
        await asyncio.sleep(0.05)

    # 第二次触发：attempt 2 > max_attempts=1 → 升级，不再创建修复
    response2 = await service.ingest(payload)
    assert response2.cooldown == 1
    assert response2.accepted == 0
    assert len(store.list_repairs()) == 1

    # 告警恢复 → 尝试计数重置
    resolved = AlertmanagerPayload.model_validate(
        {
            "status": "resolved",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {"alertname": "HighCPU", "instance": "node1", "project": "dify"},
                    "annotations": {"summary": "cpu high"},
                    "startsAt": "2026-08-06T00:00:00Z",
                    "endsAt": "2026-08-07T00:00:00Z",
                    "fingerprint": fingerprint,
                }
            ],
        }
    )
    await service.ingest(resolved)

    # 再次触发：计数已重置 → 重新接受
    response3 = await service.ingest(payload)
    assert response3.accepted == 1
    for _ in range(200):
        rows = store.list_repairs()
        if len(rows) == 2 and rows[0]["status"] in {"closed", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert len(store.list_repairs()) == 2
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_dispatch_task_runs_and_closes(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
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
    task_id, message = await service.dispatch_task("帮我看看磁盘使用")
    assert task_id.startswith("task-")
    assert "已派发" in message
    for _ in range(200):
        row = store.get_repair(task_id)
        if row and row["status"] in {"closed", "failed"}:
            break
        await asyncio.sleep(0.05)
    row = store.get_repair(task_id)
    assert row is not None
    assert row["status"] == "closed"
    assert row["result"]
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_git_check_skips_non_git_repo(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutorNoGit()
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
    ok, message, kind = await service._check_git(str(tmp_path / "norepo"), "fix/test")
    assert ok is True
    assert "skipping" in message
    assert kind == "git"
    await service.close()
    store.close()


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
    assert any("merge --ff-only" in " ".join(args) for args, _ in executor.calls)
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
