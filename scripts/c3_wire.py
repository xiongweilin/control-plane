from __future__ import annotations

import subprocess
from pathlib import Path

SOURCE_COMMIT = "2ace6be1d76177abf8ad15b0e439089f23b1bad4"
source = subprocess.check_output(
    ["git", "show", f"{SOURCE_COMMIT}:scripts/c3_wire.py"],
    text=True,
    encoding="utf-8",
)
source = source.replace(
    '    "    async def _apply_approval_decision(\\n",\n',
    '    "    async def _apply_approval_decision(",\n',
)
exec(compile(source, "<c3-wire-structural>", "exec"), {"__name__": "__main__"})

shim = Path("scripts/sitecustomize.py")
if shim.exists():
    subprocess.run(["git", "rm", "scripts/sitecustomize.py"], check=True)
