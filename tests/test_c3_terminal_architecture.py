from __future__ import annotations

import ast
from pathlib import Path


def test_case_terminal_writes_have_one_product_owner() -> None:
    root = Path("src/control_plane")
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"closure_authority.py", "storage.py", "state_machine.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name == "set_repair_resolution":
                violations.append(f"{path}:{node.lineno}: set_repair_resolution")
                continue
            if name not in {"set_repair_status", "_transition"}:
                continue
            values: list[str] = []
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values.append(arg.value.lower())
                elif isinstance(arg, ast.Attribute):
                    values.append(arg.attr.lower())
            for keyword in node.keywords:
                if keyword.arg == "status" and isinstance(keyword.value, ast.Constant):
                    values.append(str(keyword.value.value).lower())
            if any(value in {"closed", "rolled_back"} for value in values):
                violations.append(f"{path}:{node.lineno}: {name} -> {values}")
    assert violations == []


def test_verified_lifecycle_is_not_owned_by_closure_authority() -> None:
    service = Path("src/control_plane/service.py").read_text(encoding="utf-8-sig")
    assert "self._transition(repair_id, RepairState.VERIFIED)" in service
