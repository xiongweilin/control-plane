from __future__ import annotations

import json
from pathlib import Path

from scripts import verify_portable_runtime_pin as verifier


PIN = "f20cb879f3221e5bbb4ebfc6b3ae916e60829199"


def _tree(root: Path, *, marker: str = "public") -> None:
    source = root / "src" / "portable_runtime"
    (source / "core").mkdir(parents=True)
    (source / "core" / "models.py").write_text(f"marker = {marker!r}\n", encoding="utf-8")
    (source / "providers" / "codex").mkdir(parents=True)
    (source / "providers" / "codex" / "provider.py").write_text(
        "provider = 'neutral'\n", encoding="utf-8"
    )


def _pin(root: Path, digest: str) -> None:
    (root / "portable-runtime-pin.json").write_text(
        json.dumps(
            {
                "repository": "ratiolin/portable-runtime",
                "commit": PIN,
                "scope": "src/portable_runtime",
                "tree_algorithm": "sha256-path-and-content-v1",
                "tree_sha256": digest,
                "line_endings": "lf",
            }
        ),
        encoding="utf-8",
    )


def test_vendor_pin_accepts_matching_public_tree(tmp_path: Path, monkeypatch) -> None:
    public = tmp_path / "public"
    private = tmp_path / "private"
    _tree(public)
    _tree(private)
    digest = verifier._tree_digest(verifier._files(public, "src/portable_runtime"))
    _pin(private, digest)
    monkeypatch.setattr(verifier, "_git_head", lambda _: PIN)

    actual, errors = verifier.verify(root=private, public_root=public)

    assert actual == digest
    assert errors == []


def test_vendor_pin_rejects_semantic_drift(tmp_path: Path, monkeypatch) -> None:
    public = tmp_path / "public"
    private = tmp_path / "private"
    _tree(public)
    _tree(private, marker="private drift")
    digest = verifier._tree_digest(verifier._files(public, "src/portable_runtime"))
    _pin(private, digest)
    monkeypatch.setattr(verifier, "_git_head", lambda _: PIN)

    _, errors = verifier.verify(root=private, public_root=public)

    assert any("core/models.py" in error for error in errors)
    assert any("tree digest" in error for error in errors)
