from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from portable_runtime.core.capabilities import CapabilityRequest


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
    obligation_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_checks(
        cls, *, repair_id: str, checks: list[CheckResult]
    ) -> VerificationReport:
        normalized_checks = list(checks)
        return cls(
            repair_id=repair_id,
            checks=normalized_checks,
            obligation_refs=obligation_refs_for_checks(normalized_checks),
        )

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def summary(self) -> str:
        return "\n".join(
            f"{check.name}: {'PASS' if check.passed else 'FAIL'} - {check.message}"
            for check in self.checks
        )


def obligation_ref_for_check(name: str) -> str:
    normalized = str(name).strip()
    if normalized == "container_status":
        return "verify.container"
    if normalized.startswith("probe:"):
        return "verify.http"
    if normalized.startswith("promql:"):
        return "verify.promql"
    if normalized.startswith("git:") or normalized == "git_diff_guard":
        return "verify.git_diff"
    if normalized.startswith("logs:"):
        return "verify.logs"
    if normalized.startswith("tests:"):
        return "verify.tests"
    return normalized or "deterministic-verification"


def obligation_refs_for_checks(checks: list[CheckResult]) -> list[str]:
    return list(dict.fromkeys(obligation_ref_for_check(check.name) for check in checks))


FORBIDDEN_DIFF_PATTERNS = re.compile(
    r"(verifier\.py|alert\.rules\.yml|prometheus\.yml|alertmanager\.yml|"
    r"permission|acl|rbac|firewall|control-plane\.toml|AGENTS\.md|control-plane/|"
    r"\.env|credentials|secrets?|id_rsa|\.pem\b|\.key\b|token|password)",
    re.IGNORECASE,
)

VerificationResult = tuple[bool, str] | tuple[bool, str, str]
VerificationCallable = Callable[..., Awaitable[VerificationResult]]


class Verifier:
    def __init__(
        self,
        *,
        probe: VerificationCallable | None = None,
        container_status: VerificationCallable | None = None,
        promql: VerificationCallable | None = None,
        logs: VerificationCallable | None = None,
        git: VerificationCallable | None = None,
        capability_service: Any | None = None,
    ) -> None:
        self._probe = probe
        self._container_status = container_status
        self._promql = promql
        self._logs = logs
        self._git = git
        self._capability_service = capability_service
        self._direct_enabled = all(
            check is not None
            for check in (probe, container_status, promql, logs, git)
        )

    async def _invoke_capability(
        self, capability: str, parameters: dict[str, Any]
    ) -> tuple[bool, str, str]:
        request = CapabilityRequest(
            id=f"verify-{capability}-{id(parameters)}",
            capability=capability,
            parameters=parameters,
        )
        service = self._capability_service
        assert service is not None
        result = await service.invoke(request)
        passed = result.status == "succeeded"
        message = (
            result.message
            or (result.error or {}).get("message", "")
            or str(result.metadata)[:2000]
        )
        evidence_ref = (
            result.metadata.get("evidence_ref", "")
            if isinstance(result.metadata, dict)
            else ""
        )
        if not evidence_ref and result.evidence_refs:
            evidence_ref = result.evidence_refs[0]
        return passed, message[:2000], evidence_ref

    async def _run_check(
        self,
        capability: str,
        capability_parameters: dict[str, Any],
        direct: VerificationCallable | None,
        *direct_args: Any,
        **direct_kwargs: Any,
    ) -> tuple[bool, str, str]:
        if self._capability_service is not None:
            return await self._invoke_capability(capability, capability_parameters)
        if self._direct_enabled and direct is not None:
            result = await direct(*direct_args, **direct_kwargs)
            return (*result, "") if len(result) == 2 else result
        return False, f"capability unavailable: {capability}", ""

    async def verify_repair(
        self,
        *,
        repair_id: str,
        alert: dict[str, Any],
        actions: list[dict[str, Any]],
        tool_results: dict[str, Any],
    ) -> VerificationReport:
        checks: list[CheckResult] = []
        targets = [
            str(action.get("target") or "")
            for action in actions
            if action.get("tool")
            in {"restart_service", "compose_up", "wait_health", "container_status"}
        ]
        if targets:
            ok, message, evidence_ref = await self._run_check(
                "verify.container",
                {"targets": targets},
                self._container_status,
                targets,
            )
            checks.append(
                CheckResult("container_status", ok, message, evidence_ref)
            )

        for entry in tool_results.get("probe_urls", []):
            if isinstance(entry, dict):
                url = str(entry["url"])
                direct_kwargs: dict[str, Any] = {}
                parameters: dict[str, Any] = {"url": url}
                if entry.get("expected"):
                    expected = entry["expected"]
                    direct_kwargs["expected"] = set(expected)
                    parameters["expected"] = (
                        list(expected) if isinstance(expected, set) else expected
                    )
                    parameters["expected_status"] = parameters["expected"]
                if entry.get("body_contains"):
                    direct_kwargs["body_contains"] = entry["body_contains"]
                    parameters["body_contains"] = entry["body_contains"]
                if entry.get("timeout_seconds"):
                    parameters["timeout_seconds"] = entry["timeout_seconds"]
            else:
                url = str(entry)
                direct_kwargs = {}
                parameters = {"url": url}
            ok, message, evidence_ref = await self._run_check(
                "verify.http",
                parameters,
                self._probe,
                url,
                **direct_kwargs,
            )
            checks.append(CheckResult(f"probe:{url}", ok, message, evidence_ref))

        for label, query in tool_results.get("promql", {}).items():
            if isinstance(query, dict):
                promql_query = str(query["query"])
                expected = query.get("expected")
            else:
                promql_query = str(query)
                expected = None
            parameters = {"query": promql_query}
            if expected is not None:
                parameters["expected"] = expected
            ok, message, evidence_ref = await self._run_check(
                "verify.promql",
                parameters,
                self._promql,
                promql_query,
                expected,
            )
            checks.append(
                CheckResult(f"promql:{label}", ok, message, evidence_ref)
            )

        for repo, branch in tool_results.get("repos", []):
            ok, message, evidence_ref = await self._run_check(
                "verify.git",
                {"repo": repo, "branch": branch},
                self._git,
                repo,
                branch,
            )
            checks.append(CheckResult(f"git:{repo}", ok, message, evidence_ref))

        for entry in tool_results.get("error_log_targets", []):
            if isinstance(entry, dict):
                target = str(entry["target"])
                direct_kwargs = {
                    key: entry[key]
                    for key in ("since_minutes", "patterns")
                    if key in entry
                }
                parameters = {"target": target, **direct_kwargs}
            else:
                target = str(entry)
                direct_kwargs = {}
                parameters = {"target": target}
            ok, message, evidence_ref = await self._run_check(
                "verify.logs",
                parameters,
                self._logs,
                target,
                **direct_kwargs,
            )
            checks.append(CheckResult(f"logs:{target}", ok, message, evidence_ref))

        if self._capability_service is not None:
            test_entries = tool_results.get("tests") or tool_results.get("test_commands") or []
            if isinstance(test_entries, dict):
                test_entries = [test_entries]
            if isinstance(test_entries, list):
                for entry in test_entries:
                    if isinstance(entry, dict):
                        parameters = dict(entry)
                    elif isinstance(entry, (list, tuple)):
                        parameters = {"command": list(entry)}
                    else:
                        parameters = {"command": [str(entry)]}
                    ok, message, evidence_ref = await self._invoke_capability(
                        "verify.tests", parameters
                    )
                    label = parameters.get("cwd", "") or parameters.get("command", "")
                    checks.append(
                        CheckResult(f"tests:{label}", ok, message, evidence_ref)
                    )

            diff_text = tool_results.get("diff") or tool_results.get("diff_stat") or ""
            if isinstance(diff_text, str) and diff_text.strip():
                ok, message = self.diff_allowed("", diff_text)
                _, capability_message, _ = await self._invoke_capability(
                    "verify.git_diff", {"diff": diff_text}
                )
                if not ok:
                    message = f"{message} (capability: {capability_message[:200]})"
                checks.append(CheckResult("git_diff_guard", ok, message, ""))

        if not checks:
            checks.append(
                CheckResult(
                    "minimum_evidence",
                    False,
                    "No deterministic verification was configured",
                )
            )
        return VerificationReport.from_checks(repair_id=repair_id, checks=checks)

    @staticmethod
    def diff_allowed(repo: str, diff: str) -> tuple[bool, str]:
        if FORBIDDEN_DIFF_PATTERNS.search(diff):
            return (
                False,
                "Diff touches verifier, alert rules, permissions or control-plane config",
            )
        return True, "Diff is within allowed boundaries"
