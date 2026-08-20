"""Fail if provider/deployment names leak into provider-independent layers."""

from __future__ import annotations

import ast
from pathlib import Path

FORBIDDEN = {
    "codex",
    "openai",
    "anthropic",
    "claude",
    "feishu",
    "slack",
    "telegram",
    "prometheus",
    "alertmanager",
    "docker",
    "powershell",
    "kubernetes",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "src" / "portable_runtime"
    targets = [root / "core", root / "interfaces"]
    errors: list[str] = []
    for directory in targets:
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name.lower() for alias in node.names]
                    module = (node.module or "").lower() if isinstance(node, ast.ImportFrom) else ""
                    if any(any(word in name.split(".") for word in FORBIDDEN) for name in [*names, module]):
                        errors.append(f"{path}:{node.lineno}: forbidden provider import")
    if errors:
        print("\n".join(errors))
        return 1
    print("portable core import boundary passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
