from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.portable_authority import PortableRuntimeAuthority
from control_plane.verifier import CheckResult, VerificationReport
from portable_runtime.core.registry import ProviderRegistry
from portable_runtime.core.runtime import Runtime
from portable_runtime.stores.memory import InMemoryStateStore


async def _prepared_authority(tmp_path: Path, repair_id: str) -> PortableRuntimeAuthority:
    runtime = Runtime(store=InMemoryStateStore(), registry=ProviderRegistry())

    async def resolve_version(repo: str) -> str:
        assert repo == str(tmp_path)
        return "abc123"

    authority = PortableRuntimeAuthority(runtime, version_resolver=resolve_version)
    await authority.prepare_code_edit(
        repair_id=repair_id,
        repo=str(tmp_path),
        prompt="exercise C4a verification ownership",
    )
    return authority


def test_factory_owns_known_projection_with_dedup_and_stable_order() -> None:
    report = VerificationReport.from_checks(
        repair_id="repair-c4a-factory",
        checks=[
            CheckResult("probe:a", True, "a"),
            CheckResult("probe:b", True, "b"),
            CheckResult("git_diff_guard", True, "diff"),
        ],
    )

    assert report.obligation_refs == ["verify.http", "verify.git_diff"]


def test_factory_preserves_custom_check_as_explicit_obligation() -> None:
    report = VerificationReport.from_checks(
        repair_id="repair-c4a-custom",
        checks=[CheckResult("custom.policy.check", True, "custom passed")],
    )

    assert report.obligation_refs == ["custom.policy.check"]


@pytest.mark.asyncio
async def test_adapter_rejects_missing_declared_projection(tmp_path: Path) -> None:
    authority = await _prepared_authority(tmp_path, "repair-c4a-missing")
    malformed = VerificationReport(
        repair_id="repair-c4a-missing",
        checks=[
            CheckResult("probe:a", True, "http", "evidence:http"),
            CheckResult("git_diff_guard", True, "diff", "evidence:diff"),
        ],
        obligation_refs=["verify.http"],
    )

    with pytest.raises(ValueError, match=r"projection mismatch: missing=verify\.git_diff"):
        authority.record_verification(
            "repair-c4a-missing",
            report=malformed,
            evidence_refs=["evidence:http", "evidence:diff"],
        )


@pytest.mark.asyncio
async def test_adapter_rejects_extra_claimed_projection(tmp_path: Path) -> None:
    authority = await _prepared_authority(tmp_path, "repair-c4a-extra")
    malformed = VerificationReport(
        repair_id="repair-c4a-extra",
        checks=[CheckResult("probe:a", True, "http", "evidence:http")],
        obligation_refs=["verify.http", "verify.logs"],
    )

    with pytest.raises(ValueError, match=r"projection mismatch: extra=verify\.logs"):
        authority.record_verification(
            "repair-c4a-extra",
            report=malformed,
            evidence_refs=["evidence:http"],
        )


@pytest.mark.asyncio
async def test_complete_projection_without_durable_evidence_still_fails_closed(tmp_path: Path) -> None:
    authority = await _prepared_authority(tmp_path, "repair-c4a-no-evidence")
    report = VerificationReport.from_checks(
        repair_id="repair-c4a-no-evidence",
        checks=[
            CheckResult("provider_status", True, "provider passed"),
            CheckResult("probe:https://example.invalid", True, "http passed"),
            CheckResult("git_diff_guard", True, "diff passed"),
        ],
    )

    with pytest.raises(ValueError, match="durable evidence references"):
        authority.record_verification("repair-c4a-no-evidence", report=report)


def test_first_owner_architecture_has_no_service_or_adapter_projection_fallback() -> None:
    service = Path("src/control_plane/service.py").read_text(encoding="utf-8-sig")
    portable = Path("src/control_plane/portable_authority.py").read_text(encoding="utf-8-sig")
    verifier = Path("src/control_plane/verifier.py").read_text(encoding="utf-8-sig")

    assert "def from_checks" in verifier
    assert "obligation_refs=obligation_refs_for_checks(normalized_checks)" in verifier
    assert "obligation_refs_for_checks" not in service
    assert "failure_report = VerificationReport.from_checks(" in service

    assert "derived_refs = list(dict.fromkeys(obligation_refs_for_checks(report.checks)))" in portable
    assert "if declared_refs != derived_refs:" in portable
    assert "declared_refs or derived_refs" not in portable
    assert "obligation_refs_for_checks([check])" not in portable
