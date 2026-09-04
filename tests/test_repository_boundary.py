from pathlib import Path


def test_no_embedded_or_legacy_kernel_code() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_paths = [
        "src/portable_runtime",
        "portable-runtime-pin.json",
        "MIGRATION.md",
        "deployments/portable-local",
        "src/control_plane/service.py",
        "src/control_plane/storage.py",
        "src/control_plane/portable_authority.py",
        "src/control_plane/closure_authority.py",
        "src/control_plane/reconciliation.py",
        "src/control_plane/state_machine.py",
        "src/control_plane/verifier.py",
        "src/control_plane/codex_runner.py",
        "src/control_plane/budget.py",
        "src/control_plane/evidence.py",
        "src/control_plane/outward_semantics.py",
    ]
    leftovers = [path for path in forbidden_paths if (root / path).exists()]
    assert leftovers == []


def test_profile_source_has_no_legacy_bridge_tokens() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "control_plane"
    forbidden = (
        "attach_portable_store",
        "PortableRuntimeAuthority",
        "ClosureAuthority",
        "ReconciliationDescriptorStore",
        "legacy repair",
        "compatibility projection",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    for token in forbidden:
        assert token not in text
