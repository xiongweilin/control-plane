from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

import uvicorn

from .config import ControlPlaneConfig
from .runtime import (
    acquire_single_instance,
    bootstrap,
    graceful_shutdown,
    with_run_id,
)


def _run_cli_cleanup(config: ControlPlaneConfig, apply: bool) -> int:
    from .approvals import ApprovalManager
    from .budget import Budget
    from .codex_runner import CodexRunner
    from .notify import Notifier
    from .service import RepairService
    from .storage import Store

    store = Store(config.state_db)
    service = RepairService(
        config,
        store,
        Budget(store, 0, 0),
        CodexRunner(config),
        ApprovalManager(),
        Notifier(config),
    )

    async def run() -> list[dict[str, Any]]:
        try:
            return await service.cleanup_candidate_branches(apply=apply)
        finally:
            await service.close()

    branches = asyncio.run(run())
    for entry in branches:
        print(
            f"{entry['repo']}: {entry['branch']} "
            f"age={entry['age_days']}d reasons={','.join(str(r) for r in entry['reasons'])} "
            f"deleted={entry.get('deleted', False)} "
            f"error={entry.get('error', '')}"
        )
    print(f"total={len(branches)} apply={apply}")
    store.close()
    # ``--apply`` is intentionally a fail-closed compatibility request: stale
    # branches are reported but never deleted without a separate owner-
    # authorized recovery workflow.  Surface that refusal to automation with
    # a non-zero exit rather than pretending the request was applied.
    if apply and any(not bool(entry.get("deleted")) for entry in branches):
        return 2
    return 0


def _run_cli_inspect(config: ControlPlaneConfig) -> int:
    from .audit import inspect_session_fields

    fields = inspect_session_fields(config.agent_session_dir)
    for name in sorted(fields):
        print(name)
    print(f"found={len(fields)} sensitive field names (values not shown)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="control-plane")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "cleanup-candidates", "inspect-sessions"],
        help="serve (default) | cleanup-candidates (dry-run unless --apply) | inspect-sessions",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="cleanup-candidates: request deletion (currently fail-closed; advisory only)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = ControlPlaneConfig.load()

    if args.command == "cleanup-candidates":
        raise SystemExit(_run_cli_cleanup(config, args.apply))
    if args.command == "inspect-sessions":
        raise SystemExit(_run_cli_inspect(config))

    run = bootstrap(config.pid_file)
    acquired, detail = acquire_single_instance(config.pid_file)
    if not acquired:
        logging.getLogger(__name__).error("refusing to start: %s", detail)
        raise SystemExit(1)
    config = with_run_id(config)
    logging.getLogger(__name__).info(
        "control plane run_id=%s pid=%s %s",
        run.run_id,
        run.pid,
        detail,
    )
    from .app import create_app

    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=args.host or config.host,
            port=args.port or config.port,
            log_level=args.log_level,
        )
    )
    try:
        server.run()
    finally:
        graceful_shutdown(config.pid_file)


if __name__ == "__main__":
    main()
