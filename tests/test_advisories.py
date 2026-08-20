from __future__ import annotations

import json
import urllib.error

from control_plane.advisories import fetch_security_advisories


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_fetch_advisories_finds_matching_package(monkeypatch) -> None:
    payload = [
        {
            "ghsa_id": "GHSA-abc",
            "summary": "critical vuln",
            "severity": "high",
            "cvss": {"score": 8.5},
            "published_at": "2026-01-01T00:00:00Z",
            "vulnerabilities": [
                {"package": {"name": "requests"}, "vulnerable_version_range": "<2.32.0"}
            ],
        },
        {
            "ghsa_id": "GHSA-other",
            "summary": "unrelated",
            "vulnerabilities": [{"package": {"name": "flask"}}],
        },
    ]
    monkeypatch.setattr(
        "control_plane.advisories.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(payload),
    )
    info = fetch_security_advisories(["requests"], timeout_seconds=5)
    assert info.status == "ok"
    assert len(info.advisories) == 1
    assert info.advisories[0]["ghsa_id"] == "GHSA-abc"
    assert info.advisories[0]["package"] == "requests"


def test_fetch_advisories_unavailable_on_network_error(monkeypatch) -> None:
    def boom(request: object, timeout: int) -> FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("control_plane.advisories.urllib.request.urlopen", boom)
    info = fetch_security_advisories(["requests"], timeout_seconds=5)
    assert info.status == "unavailable"
    assert info.error
    assert info.advisories == []


def test_fetch_advisories_invalid_packages() -> None:
    info = fetch_security_advisories(["../../etc/passwd"])
    assert info.status == "unavailable"


def test_advisory_info_serializes() -> None:
    info = fetch_security_advisories([])
    dumped = info.to_dict()
    assert dumped["status"] == "unavailable"
    assert "error" in dumped
    assert "scanned_at" in dumped
