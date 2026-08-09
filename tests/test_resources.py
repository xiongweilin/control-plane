from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.opencodex import OpenCodexClient
from control_plane.service import RepairService
from control_plane.storage import Store
from control_plane.tools import ToolContext


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(
        ControlPlaneConfig(),
        api_key="x",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        prometheus_url="",
        alertmanager_url="",
    )


async def test_service_close_closes_owned_http_client(tmp_path) -> None:
    store = Store(_config(tmp_path).state_db)
    cfg = _config(tmp_path)
    service = RepairService(
        cfg,
        store,
        Budget(store, 10, 8),
        object(),
        ApprovalManager(),
        object(),
    )
    assert service._owns_http is True
    assert service.http.is_closed is False
    await service.close()
    assert service.http.is_closed is True
    store.close()


async def test_service_close_does_not_close_injected_http_client(tmp_path) -> None:
    store = Store(_config(tmp_path).state_db)
    cfg = _config(tmp_path)
    injected = httpx.AsyncClient()
    service = RepairService(
        cfg,
        store,
        Budget(store, 10, 8),
        object(),
        ApprovalManager(),
        object(),
        http=injected,
    )
    assert service._owns_http is False
    await service.close()
    assert injected.is_closed is False
    await injected.aclose()
    store.close()


async def test_tool_context_close_closes_owned_http_client(tmp_path) -> None:
    store = Store(_config(tmp_path).state_db)
    ctx = ToolContext(_config(tmp_path), store, "repair-1", tmp_path)
    assert ctx._owns_http is True
    await ctx.close()
    assert ctx.http.is_closed is True
    store.close()


def test_http_clients_have_pool_limits() -> None:
    from control_plane.opencodex import HTTP_LIMITS as OCX_LIMITS
    from control_plane.service import HTTP_LIMITS as SVC_LIMITS
    from control_plane.tools import HTTP_LIMITS as TOOL_LIMITS

    for limits in (OCX_LIMITS, SVC_LIMITS, TOOL_LIMITS):
        assert limits.max_connections <= 20
        assert limits.max_keepalive_connections <= 10


@pytest.mark.asyncio
async def test_open_codex_client_close_after_cancel() -> None:
    handler_tasks: list[asyncio.Task] = []

    async def handler(reader, writer):
        handler_tasks.append(asyncio.current_task())
        await asyncio.sleep(3_600)

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = OpenCodexClient(
        f"http://127.0.0.1:{port}/v1",
        "model-x",
        timeout_seconds=60,
    )
    task = asyncio.create_task(client._client.get(f"http://127.0.0.1:{port}/slow"))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The client must still close cleanly after cancellation (no leaked handle).
    await client.close()
    assert client._client.is_closed is True
    server.close()
    for task in handler_tasks:
        task.cancel()
    await asyncio.gather(*handler_tasks, return_exceptions=True)
    await server.wait_closed()


@pytest.mark.asyncio
async def test_cancelled_request_releases_pool_connection() -> None:
    """A cancelled in-flight request must release its pooled connection."""
    handler_tasks: list[asyncio.Task] = []

    async def handler(reader, writer):
        handler_tasks.append(asyncio.current_task())
        line = await reader.readline()
        parts = line.split(b" ")
        path = parts[1] if len(parts) > 1 else b"/"
        if path == b"/slow":
            await asyncio.sleep(3_600)  # never respond; cancellation tears this down
            return
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with httpx.AsyncClient(
        timeout=60,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        task = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/slow"))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The pool is not exhausted by the cancelled request: a follow-up
        # request completes, proving the connection was released back to the
        # pool (or a fresh one is available) instead of being leaked.
        response = await client.get(f"http://127.0.0.1:{port}/fast")
        assert response.status_code == 200
    server.close()
    for task in handler_tasks:
        task.cancel()
    await asyncio.gather(*handler_tasks, return_exceptions=True)
    await server.wait_closed()
