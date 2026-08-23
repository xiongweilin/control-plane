from __future__ import annotations

import pytest

from control_plane.gitpush import _looks_like_ssh_network_error, _remote_path_from_url, push_with_ssh_fallback
from control_plane.tools import ToolError


class FakePushExecutor:
    """Simulates a git remote: fail once on port-22, succeed on the 443 fallback."""

    def __init__(self, fail_times: int = 1, network_error: bool = True) -> None:
        self.fail_times = fail_times
        self.network_error = network_error
        self.calls: list[tuple[list[str], dict]] = []
        self.env_calls: list[dict | None] = []

    async def run(
        self,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout: int = 60,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        self.calls.append((args, cwd or ""))
        self.env_calls.append(env)
        joined = " ".join(args)
        if "remote get-url" in joined:
            return "git@github.com:xiongweilin/control-plane.git"
        if "push" in joined:
            if self.fail_times > 0:
                self.fail_times -= 1
                if self.network_error:
                    raise ToolError("ssh: connect to host github.com port 22: Connection timed out")
                raise ToolError("git: 'origin' does not appear to be a git repository")
            return ""
        return ""


@pytest.mark.asyncio
async def test_push_ok_first_attempt() -> None:
    executor = FakePushExecutor(fail_times=0)
    pushed, detail = await push_with_ssh_fallback(executor, "C:\\repo", timeout=60)
    assert pushed is True
    assert detail == "pushed"
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_push_falls_back_to_ssh_443() -> None:
    executor = FakePushExecutor(fail_times=1)
    pushed, detail = await push_with_ssh_fallback(
        executor,
        "C:\\repo",
        timeout=60,
        fallback_enabled=True,
        fallback_host="ssh.github.com:443",
    )
    assert pushed is True
    assert "ssh.github.com:443" in detail
    assert len(executor.calls) == 3  # push + get-url + fallback push
    fallback_call = executor.calls[-1][0]
    assert "ssh://git@ssh.github.com:443/xiongweilin/control-plane.git" in " ".join(fallback_call)
    env = executor.env_calls[-1]
    assert env is not None
    assert "GIT_SSH_COMMAND" in env
    assert "-p 443" in env["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


@pytest.mark.asyncio
async def test_push_both_fail_raises_with_classification() -> None:
    executor = FakePushExecutor(fail_times=2)
    with pytest.raises(ToolError) as excinfo:
        await push_with_ssh_fallback(executor, "C:\\repo", timeout=60)
    assert "port 22 and ssh.github.com:443" in str(excinfo.value)
    assert "retryable" in str(excinfo.value)


@pytest.mark.asyncio
async def test_push_non_network_error_no_fallback() -> None:
    executor = FakePushExecutor(fail_times=1, network_error=False)
    with pytest.raises(ToolError):
        await push_with_ssh_fallback(executor, "C:\\repo", timeout=60)
    assert len(executor.calls) == 1  # no fallback attempt


@pytest.mark.asyncio
async def test_push_fallback_disabled() -> None:
    executor = FakePushExecutor(fail_times=1)
    with pytest.raises(ToolError):
        await push_with_ssh_fallback(executor, "C:\\repo", timeout=60, fallback_enabled=False)
    assert len(executor.calls) == 1


def test_remote_path_parsing() -> None:
    assert _remote_path_from_url("git@github.com:xiongweilin/control-plane.git") == "xiongweilin/control-plane"
    assert _remote_path_from_url("https://github.com/xiongweilin/control-plane.git") == "xiongweilin/control-plane"
    assert _remote_path_from_url("https://github.com/xiongweilin/control-plane") == "xiongweilin/control-plane"
    assert _remote_path_from_url("not a url") == ""


def test_ssh_network_marker() -> None:
    assert _looks_like_ssh_network_error("ssh: connect to host github.com port 22: Connection timed out")
    assert not _looks_like_ssh_network_error("repository not found")
