from pathlib import Path

import pytest
from portable_runtime.core.capabilities import CapabilityRequest, InvocationContext

from control_plane.config import ControlPlaneConfig
from control_plane.personal_operations import PersonalOperationsProvider


@pytest.mark.asyncio
async def test_known_garbage_is_moved_to_reversible_quarantine(tmp_path: Path) -> None:
    source = tmp_path / "portable-runtime-worktrees"
    source.mkdir()
    (source / "generated.txt").write_text("generated", encoding="utf-8")
    quarantine = tmp_path / "quarantine"
    provider = PersonalOperationsProvider(
        ControlPlaneConfig(
            api_key="test-key",
            known_garbage_paths=(str(source),),
            garbage_quarantine_dir=str(quarantine),
            automatic_handling_enabled=True,
        )
    )

    result = await provider.invoke(
        CapabilityRequest(
            id="request:garbage",
            capability="maintenance.cleanup_known_garbage",
            effect_class="write-local",
        ),
        InvocationContext(runtime_id="test"),
    )

    assert result.status == "succeeded"
    assert not source.exists()
    assert sorted(path.name for path in quarantine.iterdir())
