"""Real process-level tests: git temp repos for branch restore, dirty worktree
and candidate cleanup; subprocess fakes for the agent (no codex, no network).

Covers batch2 items 8, 9 and 18.
"""

from __future__ import annotations

import time
from dataclasses import replace

import httpx
import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.dsh_runner import DshSessionResult
from control_plane.models import Alert
from control_plane.notify import Notifier
from control_plane.service import RepairService
from control_plane.storage import Store
from control_plane.tools import CommandExecutor, ToolContext, ToolError


class FakeAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def run_task(
        self,
        *,
        repair_id: str,
        repo: str,
        prompt: str,
        run_id: str = "",
    ) -> DshSessionResult:
        self.calls += 1
        return DshSessionResult(exit_code=0, last_message="ok")


async def _git_init(tmp_path) -> tuple[str, CommandExecutor, ControlPlaneConfig]:
    repo = tmp_path / "repo"
    repo.mkdir()
    executor = CommandExecutor(ControlPlaneConfig())

    async def run(args: list[str], cwd: str, env: dict | None = None) -> str:
        return await executor.run(args, cwd=cwd, env=env)

    await run(["git", "init", "-b", "main"], str(repo))
    await run(["git", "config", "user.name", "test"], str(repo))
    await run(["git", "config", "user.email", "test@example.com"], str(repo))
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    await run(["git", "add", "-A"], str(repo))
    await run(["git", "commit", "-m", "base"], str(repo))
    config = replace(
        ControlPlaneConfig(),
        api_key="test",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "cp.db",
        notification_enabled=False,
        allowed_repo_roots=(str(repo),),
        candidate_branch_prefix="fix/control-plane-",
        candidate_retention_days=14,
    )
    return str(repo), executor, config


def _service(config: ControlPlaneConfig, store: Store, executor: CommandExecutor) -> RepairService:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    return RepairService(
        config,
        store,
        Budget(store, 100, 8),
        FakeAgent(),
        ApprovalManager(),
        Notifier(config),
        executor=executor,
        http=http,
    )


@pytest.mark.asyncio
async def test_branch_restore_clean(tmp_path) -> None:
    repo, executor, config = await _git_init(tmp_path)
    store = Store(config.state_db)
    service = _service(config, store, executor)
    branch = "fix/control-plane-repair-t1"

    states = await service._capture_workspace_states(repo)
    await executor.run(["git", "-C", repo, "checkout", "-q", "-b", branch], timeout=60)
    (tmp_path / "repo" / "b.txt").write_text("x\n", encoding="utf-8")
    await executor.run(["git", "-C", repo, "add", "-A"], timeout=60)
    await executor.run(["git", "-C", repo, "commit", "-q", "-m", "candidate"], timeout=60)

    errors = await service._restore_workspace_states(states, branch)
    assert errors == []
    current = await executor.run(["git", "-C", repo, "symbolic-ref", "--short", "HEAD"], timeout=30)
    assert current.strip() == "main"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_restore_abandons_on_dirty_worktree(tmp_path) -> None:
    repo, executor, config = await _git_init(tmp_path)
    store = Store(config.state_db)
    service = _service(config, store, executor)
    branch = "fix/control-plane-repair-t2"

    states = await service._capture_workspace_states(repo)
    await executor.run(["git", "-C", repo, "checkout", "-q", "-b", branch], timeout=60)
    (tmp_path / "repo" / "b.txt").write_text("x\n", encoding="utf-8")
    await executor.run(["git", "-C", repo, "add", "-A"], timeout=60)
    await executor.run(["git", "-C", repo, "commit", "-q", "-m", "candidate"], timeout=60)
    # user uncommitted change appears after the agent finished
    (tmp_path / "repo" / "user-notes.txt").write_text("mine\n", encoding="utf-8")

    errors = await service._restore_workspace_states(states, branch)
    assert any("dirty" in error and "abandoned" in error for error in errors)
    current = await executor.run(["git", "-C", repo, "symbolic-ref", "--short", "HEAD"], timeout=30)
    assert current.strip() == branch  # restore refused; user files untouched
    assert (tmp_path / "repo" / "user-notes.txt").read_text(encoding="utf-8") == "mine\n"
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_dirty_worktree_policy_reject_blocks_agent(tmp_path) -> None:
    repo, executor, config = await _git_init(tmp_path)
    config = replace(config, dirty_worktree_policy="reject")
    store = Store(config.state_db)
    service = _service(config, store, executor)
    (tmp_path / "repo" / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    repair_id = "repair-reject-1"
    store.create_repair(repair_id, "fp-r", "{}", 1)
    store.set_repair_status(repair_id, "diagnosing")
    ctx = ToolContext(
        config,
        store,
        repair_id,
        config.patch_dir,
        executor=executor,
    )
    alert = Alert.model_validate(
        {
            "status": "firing",
            "labels": {"alertname": "HighCPU", "instance": "node1"},
            "annotations": {},
            "startsAt": "2026-08-06T00:00:00Z",
            "endsAt": None,
            "fingerprint": "fp-r",
        }
    )
    with pytest.raises(RuntimeError, match="refusing to run"):
        await service._run_dsh_agent(ctx, repair_id, alert)
    assert service.agent.calls == 0  # type: ignore[attr-defined]
    await ctx.close()
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_isolated_worktree_runs_agent_and_keeps_candidate(tmp_path) -> None:
    repo, executor, config = await _git_init(tmp_path)
    config = replace(config, dirty_worktree_policy="isolate")
    store = Store(config.state_db)
    service = _service(config, store, executor)
    (tmp_path / "repo" / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    branch = "fix/control-plane-repair-iso"

    worktree, worktree_path = await service._create_isolated_worktree(repo, branch)
    assert worktree_path.is_dir()
    await executor.run(["git", "-C", worktree, "checkout", "-q", "-b", branch], timeout=60)
    (worktree_path / "candidate.txt").write_text("c\n", encoding="utf-8")
    await executor.run(["git", "-C", worktree, "add", "-A"], timeout=60)
    await executor.run(["git", "-C", worktree, "commit", "-q", "-m", "iso candidate"], timeout=60)

    changed, diff_stat = await service._code_changed(worktree, branch)
    assert changed is True
    assert "candidate.txt" in diff_stat

    await service._remove_isolated_worktree(repo, worktree_path)
    assert not worktree_path.exists()
    # candidate branch still exists in the main repository
    head = await executor.run(["git", "-C", repo, "rev-parse", "--verify", branch], timeout=30)
    assert head.strip()
    # main repo stayed clean of the agent's work
    porcelain = await executor.run(["git", "-C", repo, "status", "--porcelain"], timeout=30)
    assert "candidate.txt" not in porcelain
    await service.close()
    store.close()


@pytest.mark.asyncio
async def test_candidate_cleanup_dry_run_and_apply(tmp_path) -> None:
    repo, executor, config = await _git_init(tmp_path)
    store = Store(config.state_db)
    service = _service(config, store, executor)

    async def make_branch(branch: str, file: str, committer_date: str | None = None) -> None:
        await executor.run(["git", "-C", repo, "checkout", "-q", "-b", branch], timeout=60)
        (tmp_path / "repo" / file).write_text("x\n", encoding="utf-8")
        await executor.run(["git", "-C", repo, "add", "-A"], timeout=60)
        env = {"GIT_COMMITTER_DATE": committer_date, "GIT_AUTHOR_DATE": committer_date} if committer_date else None
        await executor.run(["git", "-C", repo, "commit", "-q", "-m", f"candidate {branch}"], timeout=60, env=env)
        await executor.run(["git", "-C", repo, "checkout", "-q", "main"], timeout=60)

    # 1) merged into main
    await make_branch("fix/control-plane-repair-m1", "m1.txt")
    await executor.run(
        ["git", "-C", repo, "merge", "-q", "--no-ff", "-m", "merge m1", "fix/control-plane-repair-m1"],
        timeout=60,
    )
    # 2) rejected (repair closed with result=rejected)
    await make_branch("fix/control-plane-repair-r1", "r1.txt")
    store.create_repair("repair-r1", "fp-r1", "{}", 1)
    store.set_repair_status("repair-r1", "closed", result="rejected", finished_at=int(time.time()))
    # 3) expired (committed in 2020)
    await make_branch("fix/control-plane-repair-e1", "e1.txt", committer_date="2020-01-01T00:00:00")

    branches = await service.list_candidate_branches_for_cleanup([repo])
    by_branch = {entry["branch"]: entry for entry in branches}
    assert "merged" in by_branch["fix/control-plane-repair-m1"]["reasons"]
    assert "rejected" in by_branch["fix/control-plane-repair-r1"]["reasons"]
    assert "expired" in by_branch["fix/control-plane-repair-e1"]["reasons"]

    cleaned = await service.cleanup_candidate_branches([repo], apply=True)
    assert all(entry["deleted"] for entry in cleaned)
    for branch in ("fix/control-plane-repair-m1", "fix/control-plane-repair-r1", "fix/control-plane-repair-e1"):
        with pytest.raises(ToolError):
            await executor.run(["git", "-C", repo, "rev-parse", "--verify", branch], timeout=30)
    await service.close()
    store.close()
