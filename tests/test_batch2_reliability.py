from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY

from control_plane.advisories import AdvisoryInfo
from control_plane.alerts import alert_fingerprint
from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.dsh_runner import DshRunner
from control_plane.config import ControlPlaneConfig
from control_plane.models import AlertmanagerPayload
from control_plane.notify import Notifier
from control_plane.service import RepairRejectedError, RepairService
from control_plane.storage import Store
from control_plane.tools import CommandExecutor, ToolContext, ToolError, execute_tool


class FakeExecutor:
    def __init__(self, branch_exists: bool = False) -> None:
        self.branch_exists = branch_exists
        self.current_branch = "main"

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
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
        if "rev-parse --short fix/control-plane-" in joined:
            return "c0ffee"
        if "diff --name-only main...fix/control-plane-" in joined:
            return "a.txt\nb.py\n"
        if "diff --stat main...fix/control-plane-" in joined:
            return "a.txt | 1 +\nb.py | 3 +-\n"
        if "status --porcelain" in joined:
            return ""
        if "rev-parse --verify fix/control-plane-" in joined:
            if self.branch_exists:
                return "abc123"
            raise ToolError("branch missing")
        if " show " in joined and "requirements.txt" in joined:
            return "requests==2.31.0\nflask>=2.0\n"
        return ""


class FakeDshRunner:
    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
    ):
        from control_plane.dsh_runner import DshSessionResult

        return DshSessionResult(exit_code=0, last_message=f"fixed {repair_id}")


def _config(tmp_path) -> ControlPlaneConfig:
    (tmp_path / "dify").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repos").mkdir(parents=True, exist_ok=True)
    return replace(
        ControlPlaneConfig(),
        api_key="test-key",
        run_id="run-batch2",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
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
                    "fingerprint": "fp-batch2",
                }
            ],
        }
    )


def _make_service(
    config: ControlPlaneConfig,
    store: Store,
    executor: FakeExecutor | CommandExecutor,
) -> RepairService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    return RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeDshRunner(),
        ApprovalManager(),
        Notifier(config),
        executor=executor,
        http=http,
    )


@pytest.mark.asyncio
async def test_failure_records_dual_evidence_chain(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    store.create_repair("repair-x", "fp-x", "{}", attempt=2)
    store.set_repair_status("repair-x", "diagnosing", error="first failure")

    await service._record_failure(
        "repair-x",
        "fp-x",
        RuntimeError("Verification failed:\nvalue 0 != expected 1"),
    )
    row = store.get_repair("repair-x")
    assert row["status"] == "failed"
    assert row["error_class"] == "deterministic"
    assert row["original_error"] == "first failure"
    assert "value 0 != expected 1" in row["recovery_error"]
    assert row["error"] == "Verification failed:\nvalue 0 != expected 1"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_exec_timeout_kind_recorded(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    store.create_repair("repair-t", "fp-t", "{}", attempt=1)
    await service._record_failure(
        "repair-t",
        "fp-t",
        RuntimeError("Codex agent timed out without a committed candidate [timeout_kind=exec]"),
    )
    row = store.get_repair("repair-t")
    assert row["timeout_kind"] == "exec"
    assert row["error_class"] == "retryable"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_deterministic_failure_suppresses_auto_retry(tmp_path) -> None:
    config = replace(_config(tmp_path), max_attempts=3, cooldown_seconds=0)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    store.create_repair("repair-det", fingerprint, "{}", attempt=1)
    store.set_repair_status(
        "repair-det",
        "failed",
        error_class="deterministic",
        finished_at=int(time.time()) - 1,
        error="workspace dirty; refusing to run agent",
    )
    response = await service.ingest(payload)
    assert response.cooldown == 1
    assert response.accepted == 0
    assert len(store.list_repairs()) == 1
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_approval_timeout_escalates(tmp_path) -> None:
    config = replace(_config(tmp_path), approval_timeout_seconds=1)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    store.create_repair("repair-ap", "fp-ap", "{}", attempt=1)
    store.set_repair_status("repair-ap", "needs_approval")
    await service.approvals.register("repair-ap")
    with pytest.raises(RepairRejectedError):
        await service._wait_approval("repair-ap")
    row = store.get_repair("repair-ap")
    assert row["status"] == "escalated"
    assert row["timeout_kind"] == "approval"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_review_summary_includes_commit_and_files(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor(branch_exists=True))
    store.create_repair("repair-s", "fp-s", "{}", attempt=1)
    store.add_action(
        "act-s",
        "repair-s",
        "dsh_agent",
        str(tmp_path / "repos"),
        "needs_approval",
        before={"repo": str(tmp_path / "repos"), "git_ref": "main"},
        after={
            "branch": "fix/control-plane-repair-s",
            "diff_stat": "a.txt | 1 +",
            "summary": "candidate explanation",
        },
    )
    summary = await service._pending_review_summary("repair-s")
    assert "commit=c0ffee" in summary
    assert "a.txt" in summary
    assert "b.py" in summary
    assert "candidate explanation" in summary
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_command_executor_audits_redacted_args(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    executor = CommandExecutor(config)
    executor.attach_store(store)
    output = await executor.run(["git", "--version"], timeout=30)
    assert output
    rows = store.list_audit()
    assert len(rows) == 1
    assert rows[0]["kind"] == "command"
    assert rows[0]["exit_code"] == 0
    assert "git" in rows[0]["argv_json"]
    store.close()


@pytest.mark.asyncio
async def test_dsh_runner_audits_and_writes_run_header(tmp_path) -> None:
    config = replace(_config(tmp_path), dsh_cli=Path(sys.executable))
    store = Store(config.state_db)
    runner = DshRunner(config)
    runner.attach_store(store)
    result = await runner.run_task(
        repair_id="repair-runner",
        repo=str(tmp_path),
        prompt="hello",
        run_id="run-audit-1",
    )
    assert result.exit_code != 0  # python.exe is not dsh; exit non-zero is fine
    rows = store.list_audit("repair-runner")
    assert len(rows) == 1
    assert rows[0]["kind"] == "agent"
    assert rows[0]["run_id"] == "run-audit-1"
    session_file = config.agent_session_dir / "repair-runner.jsonl"
    assert session_file.is_file()
    header = session_file.read_text(encoding="utf-8").splitlines()[0]
    assert '"run_id": "run-audit-1"' in header
    store.close()


@pytest.mark.asyncio
async def test_dsh_runner_redacts_session_before_write(tmp_path, monkeypatch) -> None:
    config = replace(_config(tmp_path), dsh_cli=Path(sys.executable))
    store = Store(config.state_db)
    runner = DshRunner(config)
    runner.attach_store(store)

    class FakeProc:
        pid = 12345
        returncode = 0

        async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
            return b'{"role": "user", "data": {"api_key": "sk-live"}}\n', b""

    async def fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return FakeProc()

    monkeypatch.setattr("control_plane.dsh_runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    result = await runner.run_task(
        repair_id="repair-redact",
        repo=str(tmp_path),
        prompt="p",
        run_id="run-redact",
    )
    assert result.exit_code == 0
    content = (config.agent_session_dir / "repair-redact.jsonl").read_text(encoding="utf-8")
    assert '"api_key": "***"' in content
    assert "sk-live" not in content
    store.close()


@pytest.mark.asyncio
async def test_pending_summary_includes_dependency_advisories(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor(branch_exists=True))
    store.create_repair("repair-ad", "fp-ad", "{}", attempt=1)
    store.add_action(
        "act-ad",
        "repair-ad",
        "dsh_agent",
        str(tmp_path / "repos"),
        "needs_approval",
        before={"repo": str(tmp_path / "repos"), "git_ref": "main"},
        after={
            "branch": "fix/control-plane-repair-ad",
            "diff_stat": "requirements.txt | 2 +-",
            "summary": "upgrade dependencies for CVE",
        },
    )

    def fake_fetch(packages: list[str]) -> AdvisoryInfo:
        return AdvisoryInfo(
            status="ok",
            advisories=[
                {
                    "ghsa_id": "GHSA-x",
                    "summary": "vuln",
                    "severity": "high",
                    "package": "requests",
                }
            ],
            source="github-advisories-api",
        )

    monkeypatch.setattr("control_plane.service.fetch_security_advisories", fake_fetch)
    summary = await service._pending_review_summary("repair-ad")
    assert "安全公告: 1 条已知公告" in summary
    assert "requests" in summary
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_pending_summary_advisory_unavailable_degradation(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor(branch_exists=True))
    store.create_repair("repair-ad2", "fp-ad2", "{}", attempt=1)
    store.add_action(
        "act-ad2",
        "repair-ad2",
        "dsh_agent",
        str(tmp_path / "repos"),
        "needs_approval",
        before={"repo": str(tmp_path / "repos"), "git_ref": "main"},
        after={
            "branch": "fix/control-plane-repair-ad2",
            "diff_stat": "pyproject.toml | 2 +-",
            "summary": "dependency bump",
        },
    )

    def fake_fetch_unavailable(packages: list[str]) -> AdvisoryInfo:
        return AdvisoryInfo(
            status="unavailable",
            error="unable to fetch security advisories: timeout",
            source="github-advisories-api",
        )

    monkeypatch.setattr("control_plane.service.fetch_security_advisories", fake_fetch_unavailable)
    summary = await service._pending_review_summary("repair-ad2")
    assert "安全公告: 无法获取" in summary
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_side_effect_gate_requires_approval_flag(tmp_path) -> None:
    config = replace(_config(tmp_path), external_side_effects_require_approval=True)
    store = Store(config.state_db)
    executor = FakeExecutor()
    ctx = ToolContext(config, store, "repair-g", config.patch_dir, executor=executor)
    result = await execute_tool(ctx, "restart_service", {"project": "dify"})
    assert result.requires_approval is True
    await ctx.close()
    store.close()


@pytest.mark.asyncio
async def test_side_effect_gate_off_by_default(tmp_path) -> None:
    config = _config(tmp_path)  # flag defaults to False
    store = Store(config.state_db)
    executor = FakeExecutor()
    ctx = ToolContext(config, store, "repair-g2", config.patch_dir, executor=executor)
    result = await execute_tool(ctx, "restart_service", {"project": "dify"})
    assert result.requires_approval is False
    await ctx.close()
    store.close()


@pytest.mark.asyncio
async def test_recovery_retry_failed_metric(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    store.upsert_alert("fp-batch2", "HighCPU", "node1", "dify", "", "firing", int(time.time()))
    store.create_repair(
        "repair-m",
        fingerprint,
        json.dumps(payload.alerts[0].model_dump(mode="json"), ensure_ascii=False),
        attempt=1,
    )
    created_at = store.get_repair("repair-m")["created_at"]
    # verified recovery happened before this repair started
    store.set_setting(f"attempt_reset:{fingerprint}", str(created_at - 10))
    pattern = "HighCPU|dify|*"
    before = REGISTRY.get_sample_value(
        "control_plane_recovery_retry_failed_total",
        {"pattern": pattern},
    ) or 0.0
    await service._record_failure("repair-m", fingerprint, RuntimeError("boom"))
    after = REGISTRY.get_sample_value(
        "control_plane_recovery_retry_failed_total",
        {"pattern": pattern},
    ) or 0.0
    assert after == before + 1
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_ingest_requires_lease_and_dedupes_in_process(tmp_path) -> None:
    config = _config(tmp_path)
    store = Store(config.state_db)
    service = _make_service(config, store, FakeExecutor())
    payload = _payload()
    fingerprint = alert_fingerprint(payload.alerts[0])
    store.create_repair("repair-hold", fingerprint, "{}", attempt=1)
    store.set_repair_status("repair-hold", "queued")
    assert store.acquire_lease(fingerprint, "run-other", "repair-hold", 900) is True

    response = await service.ingest(payload)
    assert response.deduplicated == 1
    assert response.accepted == 0
    held = store.get_repair("repair-hold")
    assert held["status"] == "queued"
    await service.close()
    store.close()
