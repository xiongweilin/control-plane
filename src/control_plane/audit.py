"""Command audit with secret redaction and agent-output caps (batch2 items 11 & 17)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SENSITIVE_KEY_RE = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|authorization|credential|"
    r"private[_-]?key|access[_-]?key|bearer|set-cookie|session[_-]?id|client[_-]?secret)",
    re.IGNORECASE,
)

REDACTED = "***"


def _key_is_sensitive(key: str) -> bool:
    return SENSITIVE_KEY_RE.search(key) is not None


def redact_value(value: Any) -> Any:
    """Recursively redact values under sensitive-looking dictionary keys."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _key_is_sensitive(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value


def redact_args(args: list[str]) -> list[str]:
    """Redact command-line arguments that carry secret values.

    Handles ``--key=value``, ``--key value`` and ``Header: Authorization: Bearer x``
    style arguments. Values themselves are never echoed.
    """
    redacted: list[str] = []
    pending_sensitive = False
    for arg in args:
        if pending_sensitive:
            redacted.append(REDACTED)
            pending_sensitive = False
            continue
        match = re.match(r"^(--?[^=]+)=(.*)$", arg)
        if match:
            key, value = match.group(1), match.group(2)
            if _key_is_sensitive(key) or ("authorization" in arg.lower() and value):
                redacted.append(f"{key}={REDACTED}")
            else:
                redacted.append(arg)
            continue
        if _key_is_sensitive(arg):
            redacted.append(arg)
            pending_sensitive = True
            continue
        lowered = arg.lower()
        if "authorization" in lowered and ("bearer" in lowered or ":" in arg):
            redacted.append(REDACTED)
            continue
        redacted.append(arg)
    if pending_sensitive:
        redacted.append(REDACTED)
    return redacted


def redact_text(text: str) -> str:
    """Redact JSON-document shaped text line by line (used for agent-sessions)."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)
            continue
        out.append(json.dumps(redact_value(parsed), ensure_ascii=False))
    return "\n".join(out)


def truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    """Truncate text to ``limit`` bytes; report whether truncation happened."""
    if len(text.encode("utf-8")) <= limit:
        return text, False
    return text.encode("utf-8")[:limit].decode("utf-8", errors="replace"), True


SENSITIVE_FIELD_RE = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|authorization|credential|"
    r"private[_-]?key|access[_-]?key|bearer|set-cookie|cookie)",
    re.IGNORECASE,
)


def inspect_session_fields(directory: Path) -> set[str]:
    """List field names that may carry sensitive values inside session JSONL files.

    Read-only: only field names are returned, never values.
    """
    found: set[str] = set()
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.jsonl")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    _collect_sensitive_keys(parsed, found)
        except OSError:
            continue
    return found


def _collect_sensitive_keys(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE_FIELD_RE.search(str(key)):
                found.add(str(key))
            _collect_sensitive_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_sensitive_keys(item, found)
