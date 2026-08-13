from __future__ import annotations

from pathlib import Path

from prometheus_client import REGISTRY

from control_plane.config import ControlPlaneConfig


def _counter_value(site: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "control_plane_ignored_errors_total",
            {"site": site},
        )
        or 0.0
    )


def test_audit_write_failure_counts_controlled_ignore(tmp_path) -> None:
    from control_plane.dsh_runner import DshRunner
    from control_plane.storage import Store

    store = Store(tmp_path / "cp.db")

    def failing_add_audit_entry(*args, **kwargs):
        raise RuntimeError("database locked")

    store.add_audit_entry = failing_add_audit_entry  # type: ignore[method-assign]
    runner = DshRunner(ControlPlaneConfig(dsh_cli=Path("dsh.cmd")))
    runner.attach_store(store)
    before = _counter_value("audit_write")
    runner._audit(
        "run-1",
        "repair-1",
        ["node", "dsh", "--profile", "headless"],
        0,
        0.0,
        truncated=False,
    )
    assert _counter_value("audit_write") == before + 1
    store.close()


def test_process_tree_kill_fallback_counts_controlled_ignore(monkeypatch) -> None:
    import control_plane.runtime as rt

    def failing_taskkill(*args, **kwargs):
        raise OSError("taskkill unavailable")

    monkeypatch.setattr("control_plane.runtime.subprocess.run", failing_taskkill)
    before = _counter_value("process_tree_kill")
    # Nonexistent pid: the os.kill fallback raises ProcessLookupError which is
    # suppressed, but the controlled-ignore counter must still increment.
    rt.terminate_process_tree(999_999_999)
    assert _counter_value("process_tree_kill") == before + 1


def test_docker_df_parse_failure_counts_controlled_ignore(tmp_path) -> None:
    from control_plane.storage import Store
    from control_plane.tools import ToolContext, _cleanup_docker_tool

    store = Store(tmp_path / "cp.db")
    config = ControlPlaneConfig()
    ctx = ToolContext(config, store, "repair-1", tmp_path, executor=None, http=None)

    class FakeExecutor:
        async def run(self, args, **kwargs):
            return "not-json"

    ctx.executor = FakeExecutor()
    before = _counter_value("docker_df_parse")

    import asyncio

    asyncio.run(_cleanup_docker_tool(ctx, {"mode": "builder", "dry_run": True}))
    assert _counter_value("docker_df_parse") == before + 1
    store.close()
