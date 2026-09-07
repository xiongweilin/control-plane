from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext

import control_plane.providers.feishu as feishu_module
from control_plane.providers.feishu import FeishuNotificationProvider


def _request(*, timeout_seconds: float | None = None) -> CapabilityRequest:
    return CapabilityRequest(
        id="request:test-notification",
        capability="notify.send",
        instruction="alert text",
        timeout_seconds=timeout_seconds,
    )


def _context() -> InvocationContext:
    return InvocationContext(runtime_id="test")


def _script_path(tmp_path: Path) -> Path:
    script = tmp_path / ".local" / "bin" / "feishu-notify.ps1"
    script.parent.mkdir(parents=True)
    script.write_text("# test stub\n", encoding="utf-8")
    return script


@pytest.mark.asyncio
async def test_missing_script_is_explicit_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    result = await FeishuNotificationProvider().invoke(_request(), _context())

    assert result.status == "unavailable"
    assert result.metadata["provider_accepted"] is False
    assert result.metadata["delivery_confirmed"] is False
    assert result.metadata["failure_phase"] == "script_lookup"


@pytest.mark.asyncio
async def test_script_start_failure_is_not_provider_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_path(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    async def fail_start(*args, **kwargs):
        del args, kwargs
        raise OSError("powershell unavailable")

    monkeypatch.setattr(feishu_module.asyncio, "create_subprocess_exec", fail_start)
    result = await FeishuNotificationProvider().invoke(_request(), _context())

    assert result.status == "failed"
    assert result.metadata["provider_accepted"] is False
    assert result.metadata["failure_phase"] == "script_start"


class _FakeProcess:
    def __init__(self, returncode: int, *, delay: float = 0.0) -> None:
        self.returncode = returncode
        self.delay = delay
        self.killed = False

    async def wait(self) -> int:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode", [1, 7])
async def test_nonzero_script_exit_is_explicit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    _script_path(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    process = _FakeProcess(returncode)

    async def start(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(feishu_module.asyncio, "create_subprocess_exec", start)
    result = await FeishuNotificationProvider().invoke(_request(), _context())

    assert result.status == "failed"
    assert result.metadata["provider_accepted"] is False
    assert result.metadata["delivery_confirmed"] is False
    assert result.metadata["exit_code"] == returncode


@pytest.mark.asyncio
async def test_success_means_provider_accepted_but_not_delivery_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_path(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    process = _FakeProcess(0)

    async def start(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(feishu_module.asyncio, "create_subprocess_exec", start)
    result = await FeishuNotificationProvider().invoke(_request(), _context())

    assert result.status == "succeeded"
    assert result.metadata["provider_accepted"] is True
    assert result.metadata["delivery_confirmed"] is False
    assert result.metadata["delivery_confirmation"] == "not_available"


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_does_not_claim_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _script_path(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    process = _FakeProcess(0, delay=1.0)

    async def start(*args, **kwargs):
        del args, kwargs
        return process

    monkeypatch.setattr(feishu_module.asyncio, "create_subprocess_exec", start)
    result = await FeishuNotificationProvider().invoke(
        _request(timeout_seconds=0.001), _context()
    )

    assert result.status == "unknown"
    assert result.metadata["provider_accepted"] is None
    assert result.metadata["delivery_confirmed"] is False
    assert result.metadata["failure_phase"] == "script_wait"
    assert process.killed is True
