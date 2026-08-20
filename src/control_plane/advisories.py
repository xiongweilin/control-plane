"""Security-advisory lookup for dependency-update candidates (batch2 item 16).

Uses only the standard library (urllib) by design; no new dependencies.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

GITHUB_ADVISORIES_URL = "https://api.github.com/advisories?ecosystem=pip&per_page=100"

PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(slots=True)
class AdvisoryInfo:
    status: str  # ok | unavailable
    advisories: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    error: str = ""
    scanned_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "advisories": self.advisories,
            "source": self.source,
            "error": self.error,
            "scanned_at": self.scanned_at,
        }


def _valid_package_names(packages: list[str]) -> list[str]:
    return [name for name in packages if PACKAGE_NAME_RE.fullmatch(name or "")]


def fetch_security_advisories(
    packages: list[str],
    *,
    timeout_seconds: int = 15,
    url: str = GITHUB_ADVISORIES_URL,
) -> AdvisoryInfo:
    """Query the GitHub Security Advisories API for the given pip packages.

    Degrades to status=unavailable with the error recorded on any network, HTTP
    or parsing failure so evidence can note the gap (no hard dependency on the
    API being reachable).
    """
    names = _valid_package_names(packages)
    if not names:
        return AdvisoryInfo(
            status="unavailable",
            error="no valid package names supplied",
            source="github-advisories-api",
            scanned_at=int(time.time()),
        )
    try:
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "control-plane"},
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return AdvisoryInfo(
            status="unavailable",
            error=f"unable to fetch security advisories: {exc}",
            source="github-advisories-api",
            scanned_at=int(time.time()),
        )
    if not isinstance(body, list):
        return AdvisoryInfo(
            status="unavailable",
            error="unexpected response shape from GitHub advisories API",
            source="github-advisories-api",
            scanned_at=int(time.time()),
        )
    wanted = {name.lower() for name in names}
    hits: list[dict[str, Any]] = []
    for advisory in body:
        if not isinstance(advisory, dict):
            continue
        affected = advisory.get("vulnerabilities") or []
        for vuln in affected:
            package = (vuln.get("package") or {}).get("name", "")
            if package.lower() in wanted:
                hits.append(
                    {
                        "ghsa_id": advisory.get("ghsa_id"),
                        "summary": advisory.get("summary"),
                        "severity": advisory.get("severity"),
                        "cvss": (advisory.get("cvss") or {}).get("score"),
                        "published_at": advisory.get("published_at"),
                        "package": package,
                        "patched_versions": (vuln.get("vulnerable_version_range") or ""),
                    }
                )
                break
    return AdvisoryInfo(
        status="ok",
        advisories=hits,
        source="github-advisories-api",
        scanned_at=int(time.time()),
    )
