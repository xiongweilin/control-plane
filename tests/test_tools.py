from __future__ import annotations

import pytest

from control_plane.tools import ToolError, resolve_repo, validate_url


def test_validate_url_origin() -> None:
    assert validate_url("https://metratio.com/feedback", ("https://metratio.com",)) == "https://metratio.com/feedback"
    with pytest.raises(ToolError):
        validate_url("https://evil.example.com/x", ("https://metratio.com",))


def test_resolve_repo_allowed_and_denied() -> None:
    allowed = ("/srv/stack", "/mnt/d/download/agent")
    assert resolve_repo("/srv/stack/dify", allowed) == "/srv/stack/dify"
    assert resolve_repo("D:\\download\\agent\\control-plane", allowed) == "/mnt/d/download/agent/control-plane"
    with pytest.raises(ToolError):
        resolve_repo("/etc/ssh", allowed)
