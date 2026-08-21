from __future__ import annotations

import json
from pathlib import Path

import pytest

from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext
from portable_runtime.core.process import ProcessResult, ProcessSpec
from portable_runtime.providers.codex.provider import (
    CodexProvider,
    sandbox_for_capability,
)
from control_plane.codex_runner import CodexSessionResult
from control_plane.service import _LegacyCodexRunnerAdapter
from portable_runtime.core.capability_contract import CapabilityContractRegistry


class _CaptureExecutor:
    def __init__(self) -> None:
        self.specs: list[ProcessSpec] = []

    async def run(self, spec: ProcessSpec) -> ProcessResult:
        self.specs.append(spec)
        return ProcessResult(
            exit_code=0,
            stdout=json.dumps({"status": "succeeded", "message": "ok"}),
            stderr="",
            timed_out=False,
        )


@pytest.mark.asyncio
async def test_codex_provider_uses_capability_sandbox(tmp_path: Path) -> None:
    executor = _CaptureExecutor()
    provider = CodexProvider(
        cli="codex-not-installed",
        executor=executor,
        working_directory=tmp_path,
    )

    for capability, expected in (
        ("reason.generate", "read-only"),
        ("code.read", "read-only"),
        ("git.diff", "read-only"),
        ("code.edit", "workspace-write"),
    ):
        result = await provider.invoke(
            CapabilityRequest(id=f"req-{capability}", capability=capability, instruction="inspect"),
            InvocationContext(runtime_id="test"),
        )
        assert result.status == "succeeded"
        assert result.metadata["sandbox"] == expected

    sandboxes = [spec.argv[spec.argv.index("--sandbox") + 1] for spec in executor.specs]
    assert sandboxes == ["read-only", "read-only", "read-only", "workspace-write"]
    assert "danger-full-access" not in sandboxes


def test_codex_sandbox_mapping_fails_closed() -> None:
    assert sandbox_for_capability("reason.generate") == "read-only"
    assert sandbox_for_capability("future.capability") == "read-only"

    with pytest.raises(ValueError, match="unsupported Codex sandbox"):
        CodexProvider(sandbox_by_capability={"reason.generate": "danger-full-access"})


@pytest.mark.parametrize("capability", ["reason.generate", "code.read", "unknown.capability"])
def test_codex_sandbox_rejects_widening_read_capabilities(capability: str) -> None:
    with pytest.raises(ValueError, match="would widen"):
        CodexProvider(sandbox_by_capability={capability: "workspace-write"})


def test_codex_sandbox_allows_only_tightening_write_capabilities() -> None:
    provider = CodexProvider(sandbox_by_capability={"code.test": "read-only"})

    assert provider.descriptor.metadata["sandbox_override"] == "tighten-only"
    assert provider.descriptor.metadata["sandbox_overrides"] == {"code.test": "read-only"}


def test_codex_capabilities_have_effect_contracts() -> None:
    registry = CapabilityContractRegistry()
    assert registry.resolve("code.read").minimum_impact_class == "read"
    assert registry.resolve("git.diff").authorization_requirement == "none"
    assert registry.resolve("code.test").minimum_impact_class == "write-local"
    assert registry.resolve("shell.exec").authorization_requirement == "required"


@pytest.mark.asyncio
async def test_legacy_adapter_passes_capability_sandbox() -> None:
    class Runner:
        def __init__(self) -> None:
            self.sandboxes: list[str] = []

        async def run_task(self, *, repair_id: str, repo: str, prompt: str, run_id: str = "", sandbox: str) -> CodexSessionResult:
            self.sandboxes.append(sandbox)
            return CodexSessionResult(exit_code=0, last_message="ok")

    runner = Runner()
    adapter = _LegacyCodexRunnerAdapter(runner)
    context = InvocationContext(runtime_id="test")
    await adapter.invoke(
        CapabilityRequest(id="req-read", capability="reason.generate", instruction="inspect"),
        context,
    )
    await adapter.invoke(
        CapabilityRequest(id="req-edit", capability="code.edit", instruction="edit"),
        context,
    )
    assert runner.sandboxes == ["read-only", "workspace-write"]
