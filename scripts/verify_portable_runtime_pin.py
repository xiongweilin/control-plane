"""Verify the private vendored portable-runtime against its public pin.

The private profile owns deployment adapters, but ``src/portable_runtime`` is
not a second semantic authority.  This check makes that boundary executable:
the configured public commit must be checked out and every source file in the
declared scope must have the same normalized content as the vendored tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PIN_FILE = "portable-runtime-pin.json"


def _load_pin(root: Path) -> dict[str, Any]:
    path = root / PIN_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    for key in ("repository", "commit", "scope", "tree_algorithm", "tree_sha256", "line_endings"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise ValueError(f"{path} is missing non-empty string field {key!r}")
    commit = payload["commit"]
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError(f"{path} commit must be a lowercase 40-character SHA-1")
    if payload["tree_algorithm"] != "sha256-path-and-content-v1":
        raise ValueError(f"unsupported tree algorithm: {payload['tree_algorithm']!r}")
    if payload["line_endings"] != "lf":
        raise ValueError(f"unsupported line ending normalization: {payload['line_endings']!r}")
    return payload


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"cannot resolve git HEAD for {root}: {exc}") from exc
    return result.stdout.strip()


def _files(root: Path, scope: str) -> dict[str, bytes]:
    base = root / scope
    if not base.is_dir():
        raise ValueError(f"missing tree scope: {base}")
    files: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix()
        files[relative] = path.read_bytes().replace(b"\r\n", b"\n")
    if not files:
        raise ValueError(f"tree scope contains no source files: {base}")
    return files


def _tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative, content in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def verify(*, root: Path, public_root: Path) -> tuple[str, list[str]]:
    pin = _load_pin(root)
    expected_commit = pin["commit"]
    actual_commit = _git_head(public_root)
    errors: list[str] = []
    if actual_commit != expected_commit:
        errors.append(f"public checkout is {actual_commit}, expected pinned {expected_commit}")

    vendored = _files(root, pin["scope"])
    public = _files(public_root, pin["scope"])
    if vendored.keys() != public.keys():
        missing = sorted(public.keys() - vendored.keys())
        extra = sorted(vendored.keys() - public.keys())
        if missing:
            errors.append(f"vendored tree is missing {len(missing)} file(s): {', '.join(missing[:5])}")
        if extra:
            errors.append(f"vendored tree has {len(extra)} unexpected file(s): {', '.join(extra[:5])}")
    for relative in sorted(vendored.keys() & public.keys()):
        if vendored[relative] != public[relative]:
            errors.append(f"vendored file differs from public pin: {relative}")

    public_digest = _tree_digest(public)
    vendored_digest = _tree_digest(vendored)
    configured_digest = pin["tree_sha256"]
    if configured_digest and configured_digest != public_digest:
        errors.append(f"configured tree_sha256 {configured_digest} does not match public {public_digest}")
    if vendored_digest != public_digest:
        errors.append(f"vendored tree digest {vendored_digest} does not match public {public_digest}")
    return public_digest, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-root", type=Path, required=True, help="checked-out public portable-runtime root")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        digest, errors = verify(root=args.root.resolve(), public_root=args.public_root.resolve())
    except ValueError as exc:
        print(f"portable-runtime pin verification failed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("portable-runtime pin verification failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"portable-runtime pin verified: commit={_load_pin(args.root.resolve())['commit']} tree_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
