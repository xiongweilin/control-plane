from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from control_plane.codex_runner import CodexRunner
from control_plane.codex_boundary import CodexExecutionBoundaryAdapter
from control_plane.config import ControlPlaneConfig
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext
from portable_runtime.core.process import ProcessResult, ProcessSpec
from portable_runtime.providers.codex.provider import CodexProvider


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603, S607 - fixed test Git command
        [shutil.which("git.exe") or shutil.which("git") or "git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None and shutil.which("git.exe") is None, reason="Git required")
def test_workspace_write_uses_detached_candidate_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "control-plane test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")

    config = replace(
        ControlPlaneConfig(),
        codex_worktree_root=tmp_path / "worktrees",
        codex_isolate_worktree=True,
    )
    boundary = CodexRunner(config)._prepare_execution_boundary(str(repo), "workspace-write")
    try:
        assert boundary.worktree is not None
        assert boundary.cwd == boundary.worktree
        assert boundary.cwd != repo
        assert boundary.cwd.is_dir()
        _git(boundary.cwd, "switch", "-c", "fix/control-plane-test")
        (boundary.cwd / "README.md").write_text("candidate\n", encoding="utf-8")
        _git(boundary.cwd, "add", "README.md")
        _git(boundary.cwd, "commit", "-m", "candidate")
    finally:
        worktree = boundary.worktree
        boundary.cleanup()
    assert worktree is not None
    assert not worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "fix/control-plane-test")


def test_codex_child_environment_denies_docker_and_git_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "user-command")
    monkeypatch.setenv("SSH_AUTH_SOCK", "user-agent")
    monkeypatch.setenv("GITHUB_TOKEN", "user-token")
    config = replace(
        ControlPlaneConfig(),
        codex_worktree_root=tmp_path / "worktrees",
        codex_disable_docker=True,
        codex_disable_ssh_credentials=True,
    )
    boundary = CodexRunner(config)._prepare_execution_boundary(str(tmp_path), "read-only")
    try:
        assert "GITHUB_TOKEN" not in boundary.env
        assert boundary.env["SSH_AUTH_SOCK"] == ""
        assert "disabled" in boundary.env["DOCKER_HOST"]
        assert boundary.env["DOCKER_CONTEXT"] == "control-plane-codex-disabled"
        git_config = Path(boundary.env["GIT_CONFIG_GLOBAL"])
        assert git_config.is_file()
        content = git_config.read_text(encoding="utf-8")
        assert "helper =" in content
        assert "sshCommand" in content
        git_probe = subprocess.run(  # noqa: S603, S607 - fixed Git config probe
            [shutil.which("git.exe") or shutil.which("git") or "git", "config", "--global", "--get", "core.sshCommand"],
            capture_output=True,
            text=True,
            env=boundary.env,
            check=False,
        )
        assert git_probe.returncode == 0
        assert boundary.env["CONTROL_PLANE_CODEX_PHYSICAL_BOUNDARY"] == "1"
    finally:
        boundary.cleanup()
    assert not Path(boundary.env["GIT_CONFIG_GLOBAL"]).exists()


def test_workspace_write_refuses_non_git_source_directory(tmp_path: Path) -> None:
    config = replace(
        ControlPlaneConfig(),
        codex_worktree_root=tmp_path / "worktrees",
        codex_isolate_worktree=True,
    )
    with pytest.raises(RuntimeError, match="confirmed Git repository"):
        CodexRunner(config)._prepare_execution_boundary(str(tmp_path), "workspace-write")


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("git") is None and shutil.which("git.exe") is None, reason="Git required")
async def test_production_codex_provider_uses_private_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "control-plane test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")

    class CaptureExecutor:
        def __init__(self) -> None:
            self.spec: ProcessSpec | None = None

        async def run(self, spec: ProcessSpec) -> ProcessResult:
            self.spec = ProcessSpec(
                argv=list(spec.argv),
                cwd=spec.cwd,
                env=dict(spec.env or {}),
                timeout_seconds=spec.timeout_seconds,
            )
            return ProcessResult(exit_code=0, stdout="ok", stderr="")

    capture = CaptureExecutor()
    config = replace(
        ControlPlaneConfig(),
        codex_worktree_root=tmp_path / "worktrees",
        codex_isolate_worktree=True,
    )
    provider = CodexProvider(
        cli="codex-not-installed",
        executor=capture,
        working_directory=repo,
        execution_boundary=CodexExecutionBoundaryAdapter(config),
    )
    result = await provider.invoke(
        CapabilityRequest(id="req-private-boundary", capability="code.edit", instruction="edit"),
        InvocationContext(runtime_id="test"),
    )
    assert result.status == "succeeded"
    assert capture.spec is not None
    assert capture.spec.cwd != repo
    assert capture.spec.cwd is not None and not capture.spec.cwd.exists()
    assert capture.spec.env is not None
    assert "disabled" in capture.spec.env["DOCKER_HOST"]
    assert capture.spec.env["SSH_AUTH_SOCK"] == ""
    assert capture.spec.env["CONTROL_PLANE_CODEX_PHYSICAL_BOUNDARY"] == "1"


def test_portable_codex_provider_has_no_control_plane_imports() -> None:
    provider_path = Path(__file__).parents[1] / "src" / "portable_runtime" / "providers" / "codex" / "provider.py"
    tree = ast.parse(provider_path.read_text(encoding="utf-8"), filename=str(provider_path))
    imported_modules = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ]
    imported_names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert all(not module.startswith("control_plane") for module in imported_modules)
    assert all(not name.startswith("control_plane") for name in imported_names)
