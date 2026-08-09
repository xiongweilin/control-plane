"""Git push wrapper with GitHub SSH port-443 fallback (batch2 item 14)."""

from __future__ import annotations

import re
from typing import Any

from .errors import classify_exec_error
from .tools import ToolError

SSH_NETWORK_MARKERS = (
    "ssh",
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "could not resolve host",
    "port 22",
    "network is unreachable",
    "no route to host",
    "remote end hung up",
)


def _looks_like_ssh_network_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in SSH_NETWORK_MARKERS)


def _remote_path_from_url(url: str) -> str:
    """Extract ``owner/repo`` from an ssh or https git remote URL."""
    match = re.match(r"(?:ssh://git@|git@)[^/:]+(?::\d+)?[:/](.+?)(?:\.git)?/?$", url.strip())
    if match:
        return match.group(1)
    match = re.match(r"https?://[^/]+/(.+?)(?:\.git)?/?$", url.strip())
    if match:
        return match.group(1)
    return ""


async def push_with_ssh_fallback(
    executor: Any,
    repo: str,
    *,
    remote: str = "origin",
    branch: str = "main",
    timeout: int = 120,
    fallback_enabled: bool = True,
    fallback_host: str = "ssh.github.com:443",
) -> tuple[bool, str]:
    """Push ``branch`` to ``remote``; on SSH network failure retry via port 443.

    Returns (pushed, detail). Raises :class:`ToolError` when both attempts fail.
    The fallback rewrites the remote URL for this push only and pins
    ``GIT_SSH_COMMAND`` to a bounded, non-interactive ssh invocation.
    """
    try:
        await executor.run(
            ["git", "-C", repo, "push", "-q", remote, branch],
            timeout=timeout,
        )
        return True, "pushed"
    except ToolError as exc:
        first_error = str(exc)
        if not (fallback_enabled and _looks_like_ssh_network_error(first_error)):
            raise

    remote_url = await executor.run(
        ["git", "-C", repo, "remote", "get-url", remote],
        timeout=timeout,
    )
    path = _remote_path_from_url(remote_url)
    if not path:
        raise ToolError(
            f"git push failed and cannot infer remote path from {remote_url!r}: {first_error}"
        )
    host, _, port = fallback_host.partition(":")
    port = port or "443"
    fallback_url = f"ssh://git@{host}:{port}/{path}.git"
    ssh_command = (
        f"ssh -o BatchMode=yes -o ConnectTimeout=15 "
        f"-o StrictHostKeyChecking=accept-new -p {port}"
    )
    try:
        await executor.run(
            ["git", "-C", repo, "push", "-q", fallback_url, branch],
            timeout=timeout,
            env={"GIT_SSH_COMMAND": ssh_command},
        )
        return True, f"pushed via {fallback_host}"
    except ToolError as exc:
        classified = classify_exec_error(str(exc))
        raise ToolError(
            f"git push failed on both port 22 and {fallback_host} "
            f"({classified.value}): {exc}"
        ) from exc
