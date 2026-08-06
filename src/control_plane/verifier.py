from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    message: str
    evidence_ref: str = ""


@dataclass(slots=True)
class VerificationReport:
    repair_id: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def summary(self) -> str:
        lines = [f"{check.name}: {'PASS' if check.passed else 'FAIL'} - {check.message}" for check in self.checks]
        return "\n".join(lines)


FORBIDDEN_DIFF_PATTERNS = re.compile(
    r"(verifier\.py|alert\.rules\.yml|prometheus\.yml|alertmanager\.yml|"
    r"permission|acl|rbac|firewall|control-plane\.toml|AGENTS\.md|control-plane/)",
    re.IGNORECASE,
)


class Verifier:
    """Deterministic checks; never accepts LLM self-reports as evidence."""

    def __init__(
        self,
        *,
        probe: Any,
        container_status: Any,
        promql: Any,
        logs: Any,
        git: Any,
    ) -> None:
        self._probe = probe
        self._container_status = container_status
        self._promql = promql
        self._logs = logs
        self._git = git

    async def verify_repair(
        self,
        *,
        repair_id: str,
        alert: dict[str, Any],
        actions: list[dict[str, Any]],
        tool_results: dict[str, Any],
    ) -> VerificationReport:
        checks: list[CheckResult] = []
        targets: list[str] = []
        for action in actions:
            if action.get("tool") in {
                "restart_service",
                "compose_up",
                "wait_health",
                "container_status",
            }:
                targets.append(str(action.get("target") or ""))

        if targets:
            ok, message, ref = await self._container_status(targets)
            checks.append(CheckResult("container_status", ok, message, ref))

        probe_urls = tool_results.get("probe_urls", [])
        for url in probe_urls:
            ok, message, ref = await self._probe(url)
            checks.append(CheckResult(f"probe:{url}", ok, message, ref))

        promql_checks = tool_results.get("promql", {})
        for label, query in promql_checks.items():
            ok, message, ref = await self._promql(query)
            checks.append(CheckResult(f"promql:{label}", ok, message, ref))

        repos = tool_results.get("repos", [])
        for repo, branch in repos:
            ok, message, ref = await self._git(repo, branch)
            checks.append(CheckResult(f"git:{repo}", ok, message, ref))

        error_log_targets = tool_results.get("error_log_targets", [])
        for target in error_log_targets:
            ok, message, ref = await self._logs(target)
            checks.append(CheckResult(f"logs:{target}", ok, message, ref))

        if not checks:
            checks.append(
                CheckResult("minimum_evidence", False, "No deterministic verification was configured")
            )
        return VerificationReport(repair_id=repair_id, checks=checks)

    @staticmethod
    def diff_allowed(repo: str, diff: str) -> tuple[bool, str]:
        if FORBIDDEN_DIFF_PATTERNS.search(diff):
            return False, "Diff touches verifier, alert rules, permissions or control-plane config"
        return True, "Diff is within allowed boundaries"
