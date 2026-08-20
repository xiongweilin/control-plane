from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

try:
    from portable_runtime.core.capabilities import CapabilityRequest
except Exception:
    CapabilityRequest = Any  # type: ignore[misc,assignment]
try:
    from portable_runtime.core.router import CapabilityService
except Exception:
    CapabilityService = Any  # type: ignore[misc,assignment]
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
    r"permission|acl|rbac|firewall|control-plane\.toml|AGENTS\.md|control-plane/|"
    r"\.env|credentials|secrets?|id_rsa|\.pem\b|\.key\b|token|password)",
    re.IGNORECASE,
)
class _DirectVerifier:
    def __init__(self, *, probe: Any, container_status: Any, promql: Any, logs: Any, git: Any) -> None:
        self._probe = probe
        self._container_status = container_status
        self._promql = promql
        self._logs = logs
        self._git = git
    async def verify_repair(self, *, repair_id: str, alert: dict[str, Any], actions: list[dict[str, Any]], tool_results: dict[str, Any]) -> VerificationReport:  # noqa: E501
        checks: list[CheckResult] = []
        targets: list[str] = []
        for action in actions:
            if action.get("tool") in {"restart_service","compose_up","wait_health","container_status"}:
                targets.append(str(action.get("target") or ""))
        if targets:
            ok, message, ref = await self._container_status(targets)
            checks.append(CheckResult("container_status", ok, message, ref))
        probe_urls = tool_results.get("probe_urls", [])
        for entry in probe_urls:
            if isinstance(entry, dict):
                url = str(entry["url"])
                kwargs: dict[str, Any] = {}
                if entry.get("expected"):
                    kwargs["expected"] = set(entry["expected"])
                if entry.get("body_contains"):
                    kwargs["body_contains"] = entry["body_contains"]
            else:
                url = str(entry)
                kwargs = {}
            ok, message, ref = await self._probe(url, **kwargs)
            checks.append(CheckResult(f"probe:{url}", ok, message, ref))
        promql_checks = tool_results.get("promql", {})
        for label, query in promql_checks.items():
            if isinstance(query, dict):
                promql_query = str(query["query"])
                expected = query.get("expected")
            else:
                promql_query = str(query)
                expected = None
            ok, message, ref = await self._promql(promql_query, expected)
            checks.append(CheckResult(f"promql:{label}", ok, message, ref))
        repos = tool_results.get("repos", [])
        for repo, branch in repos:
            ok, message, ref = await self._git(repo, branch)
            checks.append(CheckResult(f"git:{repo}", ok, message, ref))
        error_log_targets = tool_results.get("error_log_targets", [])
        for entry in error_log_targets:
            if isinstance(entry, dict):
                target = str(entry["target"])
                log_kwargs = {key: entry[key] for key in ("since_minutes","patterns") if key in entry}
            else:
                target = str(entry)
                log_kwargs = {}
            ok, message, ref = await self._logs(target, **log_kwargs)
            checks.append(CheckResult(f"logs:{target}", ok, message, ref))
        if not checks:
            checks.append(CheckResult("minimum_evidence", False, "No deterministic verification was configured"))
        return VerificationReport(repair_id=repair_id, checks=checks)
    @staticmethod
    def diff_allowed(repo: str, diff: str) -> tuple[bool, str]:
        if FORBIDDEN_DIFF_PATTERNS.search(diff):
            return False, "Diff touches verifier, alert rules, permissions or control-plane config"
        return True, "Diff is within allowed boundaries"
class Verifier:
    def __init__(self, *, probe: Any | None = None, container_status: Any | None = None, promql: Any | None = None, logs: Any | None = None, git: Any | None = None, capability_service: Any | None = None) -> None:  # noqa: E501
        self._probe = probe
        self._container_status = container_status
        self._promql = promql
        self._logs = logs
        self._git = git
        self._capability_service = capability_service
        if all(x is not None for x in (probe, container_status, promql, logs, git)):
            self._direct = _DirectVerifier(probe=probe, container_status=container_status, promql=promql, logs=logs, git=git)  # noqa: E501
        else:
            self._direct = None  # type: ignore[assignment]  # noqa: E501
    async def _invoke_capability(self, capability: str, parameters: dict[str, Any]) -> tuple[bool, str, str]:
        if self._capability_service is None:
            return False, f"capability unavailable: {capability}", ""
        try:
            from portable_runtime.core.capabilities import CapabilityRequest as _Req
        except Exception:
            return False, "CapabilityRequest unavailable", ""
        req = _Req(id=f"verify-{capability}-" + str(id(parameters)), capability=capability, parameters=parameters)
        result = await self._capability_service.invoke(req)
        passed = result.status == "succeeded"
        msg = result.message or (result.error or {}).get("message", "") or str(result.metadata)[:2000]
        ref = result.metadata.get("evidence_ref", "") if isinstance(result.metadata, dict) else ""
        if not ref and result.evidence_refs:
            ref = result.evidence_refs[0]
        return passed, msg[:2000], ref
    async def verify_repair(self, *, repair_id: str, alert: dict[str, Any], actions: list[dict[str, Any]], tool_results: dict[str, Any]) -> VerificationReport:  # noqa: E501
        if self._capability_service is None:
            if self._direct is not None:
                return await self._direct.verify_repair(repair_id=repair_id, alert=alert, actions=actions, tool_results=tool_results)  # noqa: E501
            return VerificationReport(repair_id=repair_id, checks=[CheckResult("minimum_evidence", False, "No deterministic verification was configured")])  # noqa: E501
        checks: list[CheckResult] = []
        targets: list[str] = []
        for action in actions:
            if action.get("tool") in {"restart_service","compose_up","wait_health","container_status"}:
                targets.append(str(action.get("target") or ""))
        if targets:
            ok, msg, ref = await self._invoke_capability("verify.container", {"targets": targets})
            checks.append(CheckResult("container_status", ok, msg, ref))
        probe_urls = tool_results.get("probe_urls", [])
        for entry in probe_urls:
            if isinstance(entry, dict):
                url = str(entry["url"])
                params: dict[str, Any] = {"url": url}
                if entry.get("expected"):
                    exp = entry["expected"]
                    params["expected"] = list(exp) if isinstance(exp, set) else exp
                    params["expected_status"] = params["expected"]
                if entry.get("body_contains"):
                    params["body_contains"] = entry["body_contains"]
                if entry.get("timeout_seconds"):
                    params["timeout_seconds"] = entry["timeout_seconds"]
            else:
                url = str(entry)
                params = {"url": url}
            ok, msg, ref = await self._invoke_capability("verify.http", params)
            checks.append(CheckResult(f"probe:{params.get('url', url)}", ok, msg, ref))
        promql_checks = tool_results.get("promql", {})
        for label, query in promql_checks.items():
            if isinstance(query, dict):
                promql_query = str(query["query"])
                expected = query.get("expected")
            else:
                promql_query = str(query)
                expected = None
            params2: dict[str, Any] = {"query": promql_query}
            if expected is not None:
                params2["expected"] = expected
            ok, msg, ref = await self._invoke_capability("verify.promql", params2)
            checks.append(CheckResult(f"promql:{label}", ok, msg, ref))
        repos = tool_results.get("repos", [])
        for repo, branch in repos:
            ok, msg, ref = await self._invoke_capability("verify.git", {"repo": repo, "branch": branch})
            checks.append(CheckResult(f"git:{repo}", ok, msg, ref))
        error_log_targets = tool_results.get("error_log_targets", [])
        for entry in error_log_targets:
            if isinstance(entry, dict):
                target = str(entry["target"])
                params3: dict[str, Any] = {"target": target}
                if "since_minutes" in entry:
                    params3["since_minutes"] = entry["since_minutes"]
                if "patterns" in entry:
                    params3["patterns"] = entry["patterns"]
            else:
                target = str(entry)
                params3 = {"target": target}
            ok, msg, ref = await self._invoke_capability("verify.logs", params3)
            checks.append(CheckResult(f"logs:{target}", ok, msg, ref))
        test_entries = tool_results.get("tests") or tool_results.get("test_commands") or []
        if isinstance(test_entries, dict):
            test_entries = [test_entries]
        if isinstance(test_entries, list):
            for entry in test_entries:
                if isinstance(entry, dict):
                    params4 = dict(entry)
                elif isinstance(entry, (list, tuple)):
                    params4 = {"command": list(entry)}
                else:
                    params4 = {"command": [str(entry)]}
                ok, msg, ref = await self._invoke_capability("verify.tests", params4)
                checks.append(CheckResult(f"tests:{params4.get('cwd','') or params4.get('command','')}", ok, msg, ref))
        diff_text = tool_results.get("diff") or tool_results.get("diff_stat") or ""
        if isinstance(diff_text, str) and diff_text.strip():
            ok, msg = self.diff_allowed("", diff_text)
            try:
                cap_ok, cap_msg, cap_ref = await self._invoke_capability("verify.git_diff", {"diff": diff_text})
                if not ok:
                    msg = f"{msg} (capability: {cap_msg[:200]})"
            except Exception:  # noqa: S110
                pass
            checks.append(CheckResult("git_diff_guard", ok, msg, ""))
        if not checks:
            checks.append(CheckResult("minimum_evidence", False, "No deterministic verification was configured"))
        return VerificationReport(repair_id=repair_id, checks=checks)
    @staticmethod
    def diff_allowed(repo: str, diff: str) -> tuple[bool, str]:
        if FORBIDDEN_DIFF_PATTERNS.search(diff):
            return False, "Diff touches verifier, alert rules, permissions or control-plane config"
        return True, "Diff is within allowed boundaries"
LegacyVerifier = Verifier
