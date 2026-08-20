# ruff: noqa: E501
from __future__ import annotations

import asyncio
import time
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
        self.current_branch = "main"
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
        if "rev-parse --is-inside-work-tree" in joined:
            return "true"
        if "rev-parse HEAD" in joined:
            return "abc123"
        if "symbolic-ref --quiet --short HEAD" in joined:
            return self.current_branch
        if " switch " in f" {joined} ":
            self.current_branch = args[-1]
            return ""
        if "docker" in joined and "ps" in joined:
            return "Up 2 minutes\nUp 5 minutes"
        if "fix/control-plane-" in joined and "diff" in joined:
            return "a.txt | 1 +\n" if self.branch_exists else ""
        if "status --porcelain" in joined:
            return ""
        if "rev-parse --verify fix/control-plane-" in joined:
            return "abc123\n"
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


class FakeExecutorUnhealthy(FakeExecutor):
    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> str:
        if "docker" in " ".join(args) and "ps" in " ".join(args):
            return "grafana\tUp 2 minutes (unhealthy)"
        return await super().run(args, cwd=cwd, timeout=timeout, input_text=input_text)


class FakeExecutorScan(FakeExecutor):
    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
    ) -> str:
        joined = " ".join(args)
        if joined.startswith("ssh "):
            if "df -h /" in joined:
                return "/dev/vda1  40G  9.6G  28G  26% /"
            if "openssl" in joined:
                return "notAfter=Oct 31 08:03:33 2026 GMT"
            if "systemctl is-active" in joined:
                return "active"
            if "firewall-cmd --list-all" in joined:
                return (
                    "ports: 443/tcp 80/tcp\n"
                    'rich rules:\n\trule family="ipv4" source address="100.64.0.0/10" '
                    'port port="22" protocol="tcp" accept'
                )
            if "docker" in joined and "ps" in joined:
                return "gateway-nginx\tUp 2 hours"
            if "dnf check-update" in joined:
                return ""
            return ""
        if "docker" in joined and "ps" in joined:
            return "grafana\tUp 2 minutes (healthy)\nprometheus\tUp 2 minutes (healthy)"
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
        run_id: str = "",
    ) -> CodexSessionResult:
        self.calls += 1
        return CodexSessionResult(
            exit_code=self.exit_code,
            last_message=f"fixed {repair_id}",
        )


class FakeCodexRunnerWithSummary(FakeCodexRunner):
    def __init__(self, summary: str = "KEEP cand-1: 有价值\nDROP cand-2: 无价值") -> None:
        super().__init__(exit_code=0)
        self.summary = summary

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
    ) -> CodexSessionResult:
        return CodexSessionResult(
            exit_code=0,
            last_message=self.summary,
            timed_out=False,
            stderr_tail="",
        )


class FakeTimedOutCodexRunner(FakeCodexRunner):
    def __init__(self, executor: FakeExecutor) -> None:
        super().__init__(exit_code=124)
        self.executor = executor

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
    ) -> CodexSessionResult:
        self.calls += 1
        self.executor.current_branch = f"fix/control-plane-{repair_id}"
        return CodexSessionResult(
            exit_code=124,
            last_message="",
            timed_out=True,
            stderr_tail="codex session timed out",
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
async def test_resolution_without_recovery_evidence_does_not_reset_attempts(tmp_path) -> None:
    config = replace(_config(tmp_path), max_attempts=1, cooldown_seconds=0)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutorUnhealthy()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeCodexRunner(),
        approvals,
        Notifier(config),
        executor=executor,
        http=http,
    )
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    store.create_repair("repair-existing", fingerprint, "{}", attempt=1)
    store.set_repair_status(
        "repair-existing",
        "failed",
        finished_at=int(time.time()) - 60,
        error="test",
    )

    resolved = AlertmanagerPayload.model_validate(
        {
            "status": "resolved",
            "alerts": [
                {
                    "status": "resolved",
                    "labels": {
                        "alertname": "HighCPU",
                        "instance": "node1",
                        "project": "dify",
                    },
                    "annotations": {"summary": "cpu high"},
                    "startsAt": "2026-08-06T00:00:00Z",
                    "endsAt": "2026-08-07T00:00:00Z",
                    "fingerprint": fingerprint,
                }
            ],
        }
    )
    await service.ingest(resolved)

    assert store.get_setting(f"attempt_reset:{fingerprint}") == ""
    response = await service.ingest(payload)
    assert response.accepted == 0
    assert response.cooldown == 1
    assert len(store.list_repairs()) == 1
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_dispatch_task_runs_and_closes(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
async def test_env_scan_all_healthy(tmp_path) -> None:
    config = replace(_config(tmp_path), scan_disk_free_gb_min=0.0, notification_enabled=False)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutorScan()
    agent = FakeCodexRunner()

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Prometheus Server is Ready.")
        return httpx.Response(200, json={"status": "success", "data": {"alerts": []}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
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
    differences = await service.run_env_scan()
    assert differences == []
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_alert_is_firing(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    now = int(time.time())
    store.upsert_alert("fp-a", "DevEnvironmentUnhealthy", "node-exporter:9100", "", "", "firing", now)
    store.upsert_alert("fp-b", "DevEnvironmentUnhealthy", "node-exporter:9100", "", "", "resolved", now)
    assert service._alert_is_firing("fp-a") is True
    assert service._alert_is_firing("fp-b") is False
    assert service._alert_is_firing("fp-none") is False
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_cancel_in_progress_repair(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    task = asyncio.create_task(asyncio.sleep(1_000))
    service._repair_tasks["fp-cancel"] = task
    await service._cancel_in_progress_repairs("fp-cancel")
    assert task.cancelled()
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_cancel_queued_repair_marks_interrupted_and_releases_lease(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeCodexRunner(),
        ApprovalManager(),
        Notifier(config),
        executor=FakeExecutor(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )
    service._semaphore = asyncio.Semaphore(0)
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])

    response = await service.ingest(payload)
    assert response.accepted == 1
    await asyncio.sleep(0)
    repair = store.list_repairs()[0]
    assert repair["status"] == "queued"

    await service._cancel_in_progress_repairs(fingerprint)

    repair = store.get_repair(repair["id"])
    assert repair is not None
    assert repair["status"] == "interrupted"
    assert repair["timeout_kind"] == "exec"
    assert store.get_lease_owner(fingerprint) is None
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_check_containers_fails_on_unhealthy(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutorUnhealthy()
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    ok, message, kind = await service._check_containers(["observability"])
    assert ok is False
    assert "unhealthy" in message
    assert kind == "container_status"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_resolve_project_maps_legacy_docker_alias(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
    service = RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeCodexRunner(),
        approvals,
        Notifier(config),
        executor=FakeExecutor(),
        http=http,
    )
    # "docker" 是 dify 的旧 Compose 项目名（-p docker 时代）；项目已改名 name: dify，
    # 容器 label 为 com.docker.compose.project=dify，按 label 的容器检查必须用实际项目名。
    assert service._resolve_project("docker") == "dify"
    assert service._resolve_project("dify") == "dify"
    assert service._resolve_project("observability") == "observability"
    assert service._resolve_project("") == ""
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_check_promql_expected_mismatch(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {}, "value": [0, "0"]}],
                    },
                },
            )
        )
    )
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
    ok, message, kind = await service._check_promql('up{instance="x"}', expected=1)
    assert ok is False
    assert "!= expected 1" in message
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_run_digest_no_records(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    message = await service.run_digest()
    assert message == "无沉淀记录"
    assert store.get_setting("digest:last_date") != ""
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_run_digest_drops_candidate(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor()
    agent = FakeCodexRunnerWithSummary("DROP cand-x: 无价值，重复且无证据\nKEEP cand-y: 有真实证据")
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    now = int(time.time())
    store.create_candidate(
        "cand-x", "ServiceDown|*|*", "control-plane", "[]", "probe", "candidate",
        now + 86400 * 90, "archive", "", "env change", "repair-1",
    )
    store.create_candidate(
        "cand-y", "HighCPU|*|*", "control-plane", "[]", "probe", "candidate",
        now + 86400 * 90, "archive", "", "env change", "repair-2",
    )
    message = await service.run_digest()
    assert "归档 1 条" in message
    assert "cand-y" in message
    statuses = {row["id"]: row["status"] for row in store.list_candidates()}
    assert statuses["cand-x"] == "archived"
    assert statuses["cand-y"] == "candidate"
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
async def test_timed_out_agent_candidate_waits_for_review_and_restores_branch(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor(branch_exists=True)
    agent = FakeTimedOutCodexRunner(executor)
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
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
        if store.get_repair(repair_id)["status"] == "needs_approval":
            break
        await asyncio.sleep(0.05)

    assert store.get_repair(repair_id)["status"] == "needs_approval"
    assert executor.current_branch == "main"
    action = store.list_actions(repair_id)[0]
    assert action["status"] == "needs_approval"
    assert f"fix/control-plane-{repair_id}" in action["after_json"]

    await approvals.decide(repair_id, "reject")
    for _ in range(100):
        if store.get_repair(repair_id)["status"] == "closed":
            break
        await asyncio.sleep(0.05)
    assert store.get_repair(repair_id)["result"] == "rejected"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_reject_closes_repair_without_candidate(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    approvals = ApprovalManager()
    executor = FakeExecutor(branch_exists=True)
    agent = FakeCodexRunner()
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
    def _mock(request):
        if "api/v1/query" in str(request.url):
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, "1"]}]}})
        return httpx.Response(200, json={})
    http = httpx.AsyncClient(transport=httpx.MockTransport(_mock))
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
