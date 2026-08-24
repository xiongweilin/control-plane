from __future__ import annotations

from pathlib import Path

path = Path(__file__).with_name("c3_wire.py")
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    "    async def _apply_approval_decision(\\n",\n',
    '    "    async def _apply_approval_decision(",\n',
)
path.write_text(text, encoding="utf-8")
Path(__file__).unlink()
