from __future__ import annotations

import json

from control_plane.audit import (
    inspect_session_fields,
    redact_args,
    redact_text,
    redact_value,
    truncate_bytes,
)


def test_redact_value_nested() -> None:
    data = {
        "ok": "visible",
        "api_key": "sk-secret",
        "Authorization": "Bearer xyz",
        "nested": {"password": "p", "url": "https://example.com"},
        "list": [{"token": "t"}, "plain"],
    }
    redacted = redact_value(data)
    assert redacted["ok"] == "visible"
    assert redacted["api_key"] == "***"
    assert redacted["Authorization"] == "***"
    assert redacted["nested"]["password"] == "***"  # noqa: S105 - redaction sentinel
    assert redacted["nested"]["url"] == "https://example.com"
    assert redacted["list"][0]["token"] == "***"  # noqa: S105 - redaction sentinel
    assert redacted["list"][1] == "plain"


def test_redact_args_forms() -> None:
    args = [
        "curl",
        "-H",
        "Authorization: Bearer secret123",
        "--api-key=abc",
        "--password",
        "hunter2",
        "--url=https://x",
        "plain",
    ]
    redacted = redact_args(args)
    assert "secret123" not in " ".join(redacted)
    assert "hunter2" not in " ".join(redacted)
    assert "--api-key=***" in redacted
    assert "--password" in redacted
    assert "https://x" in " ".join(redacted)
    assert "plain" in redacted


def test_redact_text_jsonl() -> None:
    text = (
        '{"role": "user", "content": "hi", "headers": {"authorization": "Bearer x"}}\n'
        '{"role": "assistant", "content": "hello"}\n'
        "not json line\n"
    )
    redacted = redact_text(text)
    lines = redacted.splitlines()
    assert "Bearer x" not in redacted
    assert json.loads(lines[0])["headers"]["authorization"] == "***"
    assert json.loads(lines[1])["content"] == "hello"
    assert lines[2] == "not json line"


def test_truncate_bytes() -> None:
    text = "a" * 10_000
    capped, truncated = truncate_bytes(text, 1_000)
    assert truncated is True
    assert len(capped.encode("utf-8")) <= 1_000
    small, truncated_small = truncate_bytes("tiny", 1_000)
    assert truncated_small is False
    assert small == "tiny"


def test_inspect_session_fields_names_only(tmp_path) -> None:
    session_dir = tmp_path / "agent-sessions"
    session_dir.mkdir()
    (session_dir / "repair-1.jsonl").write_text(
        '{"type": "item", "data": {"api_key": "sk-value", "ok": "fine"}}\n'
        '{"type": "item", "data": {"headers": {"authorization": "Bearer v"}}}\n',
        encoding="utf-8",
    )
    (session_dir / "repair-2.jsonl").write_text(
        '{"type": "item", "data": {"token": "t"}}\n',
        encoding="utf-8",
    )
    fields = inspect_session_fields(session_dir)
    assert "api_key" in fields
    assert "authorization" in fields
    assert "token" in fields
    assert "ok" not in fields
    assert "type" not in fields
    # values never surface
    assert "sk-value" not in fields


def test_inspect_session_fields_missing_dir(tmp_path) -> None:
    assert inspect_session_fields(tmp_path / "nope") == set()
