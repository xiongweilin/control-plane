from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .alerts import alert_fingerprint, fingerprint_pattern
from .approvals import ApprovalManager
from .budget import Budget
from .codex_runner import CodexRunner
from .config import ControlPlaneConfig
from .evidence import EvidenceRecord, write_evidence
from .models import Alert, AlertmanagerPayload, AlertResponse
from .notify import Notifier
from .state_machine import TERMINAL_STATES, RepairState, require_transition
from .storage import Store
from .tools import (
    CommandExecutor,
    ToolContext,
    ToolError,
    ToolResult,
    _probe,
    resolve_repo,
)
from .verifier import Verifier

logger = logging.getLogger(__name__)


class RepairRejectedError(RuntimeError):
    pass


class RepairService:
    def __init__(
        self,
        config: ControlPlaneConfig,
        store: Store,
        budget: Budget,
        agent: CodexRunner,
        approvals: ApprovalManager,
        notifier: Notifier,
        executor: CommandExecutor | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.budget = budget
        self.agent = agent
        self.approvals = approvals
        self.notifier = notifier
        self.executor = executor or CommandExecutor(config)
        self.http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def paused(self) -> bool:
        return self.config.paused or self.store.get_setting("paused", "0") == "1"

    async def ingest(self, payload: AlertmanagerPayload) -> AlertResponse:
        accepted = 0
        deduplicated = 0
        cooldown = 0
        budget_limited = 0
        paused = 0
        for alert in payload.alerts:
            decision = await self._ingest_alert(alert)
            accepted += decision.get("accepted", 0)
            deduplicated += decision.get("deduplicated", 0)
            cooldown += decision.get("cooldown", 0)
            budget_limited += decision.get("budget_limited", 0)
            paused += decision.get("paused", 0)
        return AlertResponse(
            accepted=accepted,
            deduplicated=deduplicated,
            cooldown=cooldown,
            budget_limited=budget_limited,
            paused=paused,
        )

    async def _ingest_alert(self, alert: Alert) -> dict[str, int]:
        fingerprint = alert_fingerprint(alert)
        alertname = alert.labels.get("alertname", "unknown")
        instance = alert.labels.get("instance", "")
        project = alert.labels.get("project", "")
        container = alert.labels.get("container", "")
        starts_at = int(alert.starts_at.timestamp())

        if alert.status == "resolved":
            self.store.mark_alert_resolved(fingerprint, int(alert.ends_at.timestamp()) if alert.ends_at else None)
            await self._notify("info", f"告警已恢复：{alertname}", self._describe(alert))
            return {"deduplicated": 1}

        self.store.upsert_alert(fingerprint, alertname, instance, project, container, "firing", starts_at)

        if self.paused:
            await self._notify("warning", "控制平面已暂停，告警未处理", f"{alertname}: {self._describe(alert)}")
            return {"paused": 1}

        existing = self.store.get_alert(fingerprint)
        if existing and self.store.get_repair_state_for_fingerprint(fingerprint) == "in_progress":
            return {"deduplicated": 1}

        now = int(time.time())
        latest = self._latest_finished_repair(fingerprint)
        if latest is not None:
            finished_at = int(latest["finished_at"] or 0)
            if finished_at and now - finished_at < self.config.cooldown_seconds:
                return {"cooldown": 1}

        attempt = 1
        if latest is not None:
            attempt = int(latest["attempt"]) + 1
            if attempt > self.config.max_attempts:
                await self._notify(
                    "critical",
                    f"告警达到最大尝试次数：{alertname}",
                    f"{self._describe(alert)}\n已升级，不再自动重试。",
                )
                return {"cooldown": 1}

        if not self.budget.can_spend():
            await self._notify("warning", "Agent 调用预算已耗尽", self._describe(alert))
            return {"budget_limited": 1}

        repair_id = f"repair-{uuid.uuid4().hex[:12]}"
        payload_json = json.dumps(alert.model_dump(mode="json"), ensure_ascii=False)
        self.store.create_repair(repair_id, fingerprint, payload_json, attempt)
        self.store.set_setting(f"repair:{repair_id}:fingerprint", fingerprint)
        await self._notify("info", f"开始修复：{alertname}", f"repair_id={repair_id}\n{self._describe(alert)}")
        task = asyncio.create_task(self._run_repair(repair_id, fingerprint, alert))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"accepted": 1}

    async def _run_repair(self, repair_id: str, fingerprint: str, alert: Alert) -> None:
        async with self._semaphore:
            ctx = ToolContext(
                self.config,
                self.store,
                repair_id,
                self.config.patch_dir,
                executor=self.executor,
                http=self.http,
            )
            try:
                self._transition(repair_id, RepairState.DIAGNOSING)
                proposal = await self._run_codex_agent(ctx, repair_id, alert)
                if proposal.get("code_changed"):
                    await self._await_approval(ctx, repair_id, "apply")
                report = await self._verify(ctx, repair_id, alert)
                if not report.all_passed:
                    raise RuntimeError(f"Verification failed:\n{report.summary}")
                self._transition(repair_id, RepairState.VERIFIED)
                await self._create_candidate(ctx, repair_id, fingerprint, alert)
                await self._notify(
                    "info",
                    f"修复完成：{alert.labels.get('alertname', 'unknown')}",
                    f"repair_id={repair_id}\n{report.summary}",
                )
                self._transition(repair_id, RepairState.CLOSED, finished_at=int(time.time()), result=report.summary)
            except asyncio.CancelledError:
                self.store.set_repair_status(repair_id, RepairState.INTERRUPTED.value, finished_at=int(time.time()))
                await self._notify("warning", "修复被中断", f"repair_id={repair_id}")
                raise
            except RepairRejectedError:
                return
            except Exception as exc:
                logger.exception("repair failed: %s", repair_id)
                self.store.set_repair_status(
                    repair_id,
                    RepairState.FAILED.value,
                    error=str(exc)[:2_000],
                    finished_at=int(time.time()),
                )
                await self._notify("critical", "修复失败", f"repair_id={repair_id}\n{exc}")
            finally:
                await ctx.close()

    async def _run_codex_agent(
        self,
        ctx: ToolContext,
        repair_id: str,
        alert: Alert,
    ) -> dict[str, Any]:
        self._transition(repair_id, RepairState.PROPOSING)
        repo = await self._pick_repo(alert)
        branch = f"{self.config.codex_branch_prefix}{repair_id}"
        before = await self._capture_repo_state(repo)
        prompt = self._build_agent_prompt(alert, repo, repair_id, branch)
        result = await self.agent.run_task(
            repair_id=repair_id,
            repo=repo,
            prompt=prompt,
        )
        self.store.increment_agent_calls(repair_id)
        self.budget.spend()
        if result.timed_out:
            raise RuntimeError("Codex agent timed out")
        changed, diff_stat = await self._code_changed(repo, branch)
        if result.exit_code != 0 and not changed:
            raise RuntimeError(
                f"Codex agent failed (exit {result.exit_code}): {result.stderr_tail}"
            )
        self._record_codex_action(
            repair_id,
            repo,
            before,
            branch,
            diff_stat,
            result.last_message,
            changed=changed,
        )
        return {"code_changed": changed, "branch": branch, "summary": result.last_message}

    def _build_agent_prompt(
        self,
        alert: Alert,
        repo: str,
        repair_id: str,
        branch: str,
    ) -> str:
        playbooks = self.store.list_playbooks()
        candidates = [
            row
            for row in self.store.list_candidates("candidate")
            if row["pattern"] == fingerprint_pattern(alert)
        ]
        lines = [
            "你是个人平台控制平面的修复 Agent，运行在完整 Codex 工具环境中。",
            f"repair_id: {repair_id}",
            f"工作目录: {repo}",
            f"当前告警: {json.dumps(alert.model_dump(mode='json'), ensure_ascii=False)}",
            "",
            "硬约束：",
            "- 代码或配置修改必须从 main 创建分支，并提交到该分支："
            f"git checkout -b {branch}；完成后 git add -A && git commit。禁止 push。",
            "- 禁止修改：验证器、告警规则（alert.rules.yml / prometheus.yml / alertmanager.yml）、"
            "权限、AGENTS.md、凭据、control-plane 自身代码与数据。",
            "- 运维只允许：白名单 compose 项目（dify、feedback-analysis-agent、catalog-ops-automation）"
            "的 docker compose restart / up -d、只读 docker 诊断、URL 探针与 PromQL 查询。",
            "- 诊断优先只读；无法确认的失败不要伪装成功；不制造无意义的新告警。",
            "- 候选经验与 official playbook 只作为推理参考，不自动取得执行权限。",
        ]
        if playbooks:
            lines.append("")
            lines.append("可参考的 official playbook：")
            lines.extend(f"- [{row['pattern']}] {row['tool_sequence']}" for row in playbooks[:5])
        if candidates:
            lines.append("")
            lines.append("可参考的候选经验（未晋升，不自动执行）：")
            lines.extend(f"- [{row['pattern']}] {row['tool_sequence']}" for row in candidates[:5])
        lines.extend(
            [
                "",
                "最后一条消息请总结：根因判断、执行的动作、验证结果、是否创建分支与分支名。",
            ]
        )
        return "\n".join(lines)

    async def _pick_repo(self, alert: Alert) -> str:
        project = alert.labels.get("project", "")
        resolved = self._resolve_project(project)
        if resolved in self.config.allowed_auto_projects:
            candidate = self.config.project_dirs.get(
                resolved, f"D:\\infrastructure\\compose\\{resolved}"
            )
            if await self._path_exists(candidate):
                return candidate
        repo = alert.labels.get("repo", "")
        if repo:
            try:
                return resolve_repo(repo, self.config.allowed_repo_roots)
            except ToolError:
                pass
        for root in self.config.allowed_repo_roots:
            if await self._path_exists(root):
                return root
        return self.config.allowed_repo_roots[0]

    def _resolve_project(self, project: str) -> str:
        if project == "dify":
            return "docker"
        return project

    async def _path_exists(self, path: str) -> bool:
        return Path(path).is_dir()

    async def _capture_repo_state(self, repo: str) -> dict[str, str]:
        try:
            head = await self.executor.run(
                ["bash", "-lc", f"cd {repo} && git rev-parse --short HEAD 2>/dev/null || echo no-git"]
            )
        except ToolError:
            head = "no-git"
        return {"git_head": head}

    async def _code_changed(self, repo: str, branch: str) -> tuple[bool, str]:
        try:
            output = await self.executor.run(
                [
                    "bash",
                    "-lc",
                    f"cd {repo} && git rev-parse --verify {branch} >/dev/null 2>&1 && "
                    f"git diff --stat main...{branch} 2>/dev/null || true",
                ]
            )
        except ToolError:
            return False, ""
        stat_lines = [line for line in output.splitlines() if "|" in line]
        return bool(stat_lines), output.strip()

    def _record_codex_action(
        self,
        repair_id: str,
        repo: str,
        before: dict[str, str],
        branch: str,
        diff_stat: str,
        summary: str,
        *,
        changed: bool,
    ) -> None:
        action_id = f"act-{uuid.uuid4().hex[:12]}"
        self.store.add_action(
            action_id,
            repair_id,
            "codex_agent",
            repo,
            "needs_approval" if changed else "ok",
            before=before,
            after={"branch": branch, "diff_stat": diff_stat, "summary": summary[:4_000]},
            output=summary[:10_000],
        )

    def _record_action(
        self,
        repair_id: str,
        tool: str,
        target: str,
        result: ToolResult,
    ) -> None:
        action_id = f"act-{uuid.uuid4().hex[:12]}"
        self.store.add_action(
            action_id,
            repair_id,
            tool,
            target[:500],
            "needs_approval" if result.requires_approval else result.status,
            before=result.before,
            after=result.after,
            output=result.output[:10_000],
        )

    async def _await_approval(self, ctx: ToolContext, repair_id: str, kind: str) -> None:
        self._transition(repair_id, RepairState.NEEDS_APPROVAL)
        await self.approvals.register(repair_id)
        await self._notify(
            "warning",
            "待审批：控制平面修复",
            f"repair_id={repair_id}\n请回复 /cp approve {repair_id} 或 /cp reject {repair_id}。",
        )
        decision = await self.approvals.wait(repair_id)
        await self.approvals.remove(repair_id)
        if decision == "approve":
            self._transition(repair_id, RepairState.APPLYING)
            await self._apply_code_candidates(ctx, repair_id)
        elif decision == "rollback":
            await self._rollback(ctx, repair_id)
            self.store.set_repair_status(repair_id, RepairState.ROLLED_BACK.value, finished_at=int(time.time()))
            await self._notify("warning", "修复已回滚", f"repair_id={repair_id}")
            raise RepairRejectedError()
        else:
            self.store.set_repair_status(
                repair_id,
                RepairState.CLOSED.value,
                finished_at=int(time.time()),
                result="rejected",
            )
            await self._notify("info", "修复已被拒绝", f"repair_id={repair_id}")
            raise RepairRejectedError()

    async def _apply_code_candidates(self, ctx: ToolContext, repair_id: str) -> None:
        for row in self.store.list_actions(repair_id):
            if row["tool"] != "codex_agent":
                continue
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            repo = row["target"]
            if not repo:
                continue
            branch = after.get("branch", f"{self.config.codex_branch_prefix}{repair_id}")
            script = (
                f"cd {repo} && "
                f"git checkout -q main && "
                f"git merge --ff-only {branch} 2>/dev/null || git merge -q --no-edit {branch} && "
                f"git push -q origin main"
            )
            await self.executor.run(["bash", "-lc", script], timeout=300)
            await self._notify("info", "代码候选已合并到 main", f"repair_id={repair_id}\nrepo={repo}")

    async def _verify(
        self,
        ctx: ToolContext,
        repair_id: str,
        alert: Alert,
    ):
        verifier = Verifier(
            probe=lambda url: _probe(self.http, url),
            container_status=self._check_containers,
            promql=self._check_promql,
            logs=self._check_logs,
            git=self._check_git,
        )
        actions = [
            {
                "tool": row["tool"],
                "target": row["target"],
                "before_json": row["before_json"],
                "after_json": row["after_json"],
            }
            for row in self.store.list_actions(repair_id)
        ]
        tool_results: dict[str, Any] = {
            "probe_urls": [],
            "promql": {},
            "repos": [],
            "error_log_targets": [],
        }
        project = alert.labels.get("project", "")
        resolved_project = self._resolve_project(project)
        if resolved_project in self.config.allowed_auto_projects:
            actions.append({"tool": "container_status", "target": resolved_project})
        for row in self.store.list_actions(repair_id):
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            if row["tool"] == "codex_agent" and after.get("branch"):
                tool_results["repos"].append((row["target"], after["branch"]))
        return await verifier.verify_repair(
            repair_id=repair_id,
            alert=alert.model_dump(mode="json"),
            actions=actions,
            tool_results=tool_results,
        )

    async def _check_containers(self, projects: list[str]) -> tuple[bool, str, str]:
        failures: list[str] = []
        for project in projects:
            try:
                output = await self.executor.run(
                    [
                        "docker",
                        "ps",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                        "--format",
                        "{{.Status}}",
                    ]
                )
            except ToolError as exc:
                failures.append(f"{project}: {exc}")
                continue
            if not any(line.startswith("Up") for line in output.splitlines()):
                failures.append(f"{project}: no running containers")
        if failures:
            return False, "; ".join(failures), "container_status"
        return True, "all target containers running", "container_status"

    async def _check_promql(self, query: str) -> tuple[bool, str, str]:
        try:
            response = await self.http.get(
                f"{self.config.prometheus_url}/api/v1/query",
                params={"query": query},
                timeout=20,
            )
            response.raise_for_status()
            body = response.json()
            results = body.get("data", {}).get("result", [])
        except (httpx.HTTPError, ValueError) as exc:
            return False, f"promql error: {exc}", "promql"
        if not results:
            return False, f"no result for query: {query}", "promql"
        return True, "query returned results", "promql"

    async def _check_logs(self, target: str) -> tuple[bool, str, str]:
        if ":" not in target:
            return True, "no log target", "logs"
        project, service = target.split(":", 1)
        try:
            output = await self.executor.run(
                [
                    "docker",
                    "logs",
                    "--tail",
                    "50",
                    "--format",
                    "{{.Name}}\t{{.Message}}",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    f"label=com.docker.compose.service={service}",
                ]
            )
        except ToolError as exc:
            return False, f"log fetch failed: {exc}", "logs"
        for pattern in ("Traceback", "panic:", "FATAL"):
            if pattern in output:
                return False, f"log contains {pattern}", "logs"
        return True, "no fatal patterns in recent logs", "logs"

    async def _check_git(self, repo: str, branch: str) -> tuple[bool, str, str]:
        try:
            diff = await self.executor.run(
                ["bash", "-lc", f"cd {repo} && git diff main...{branch} --stat 2>/dev/null || true"]
            )
            dirty = await self.executor.run(
                ["bash", "-lc", f"cd {repo} && git status --porcelain 2>/dev/null || true"]
            )
        except ToolError as exc:
            return False, f"git check failed: {exc}", "git"
        allowed, message = Verifier.diff_allowed(repo, diff)
        if not allowed:
            return False, message, "git"
        if dirty.strip():
            return False, f"workspace is dirty after repair: {dirty.strip()[:200]}", "git"
        return True, message, "git"

    async def _rollback(self, ctx: ToolContext, repair_id: str) -> None:
        try:
            payload = json.loads(self.store.get_repair(repair_id)["payload_json"])
            project = payload.get("labels", {}).get("project", "")
        except (json.JSONDecodeError, TypeError):
            project = ""
        for row in reversed(self.store.list_actions(repair_id)):
            tool = row["tool"]
            if tool == "codex_agent":
                after = json.loads(row["after_json"]) if row["after_json"] else {}
                repo = row["target"]
                branch = after.get("branch", f"{self.config.codex_branch_prefix}{repair_id}")
                try:
                    for git_args in (
                        ["git", "-C", repo, "checkout", "-q", "main"],
                        ["git", "-C", repo, "branch", "-D", branch],
                    ):
                        try:
                            await self.executor.run(git_args, timeout=120)
                        except ToolError:
                            logger.warning("candidate branch cleanup step failed for %s", row["id"])
                except ToolError:
                    logger.warning("candidate branch cleanup failed for %s", row["id"])
        resolved_project = self._resolve_project(project)
        if resolved_project in self.config.allowed_auto_projects:
            try:
                project_dir = self.config.project_dirs.get(
                    resolved_project, f"D:\\infrastructure\\compose\\{resolved_project}"
                )
                await self.executor.run(
                    ["docker", "compose", "restart"],
                    cwd=str(project_dir),
                    timeout=180,
                )
            except ToolError:
                logger.warning("rollback restart failed for project %s", project)

    async def _create_candidate(
        self,
        ctx: ToolContext,
        repair_id: str,
        fingerprint: str,
        alert: Alert,
    ) -> None:
        pattern = fingerprint_pattern(alert)
        actions = self.store.list_actions(repair_id)
        if not actions:
            return
        tool_sequence = json.dumps(
            [
                {
                    "tool": row["tool"],
                    "target": row["target"],
                    "branch": (
                        json.loads(row["after_json"]).get("branch")
                        if row["after_json"]
                        else None
                    ),
                }
                for row in actions
            ],
            ensure_ascii=False,
        )
        candidate_count = len(self.store.list_candidates("candidate"))
        if candidate_count >= self.config.candidate_wip_limit:
            await self._notify("warning", "候选经验达到 WIP 上限", f"pattern={pattern}，未创建候选。")
            return
        existing = self.store.find_candidate(pattern, ("candidate", "official"))
        if existing is not None:
            if existing["status"] == "candidate":
                self.store.update_candidate(
                    existing["id"],
                    times_supported=int(existing["times_supported"]) + 1,
                    tool_sequence=tool_sequence,
                )
            return
        candidate_id = f"cand-{uuid.uuid4().hex[:12]}"
        deadline = int(time.time()) + self.config.candidate_trial_days * 86_400
        self.store.create_candidate(
            candidate_id,
            pattern,
            "control-plane",
            tool_sequence,
            "container_status,probe,promql",
            "candidate",
            deadline,
            self.config.default_disposition,
            "",
            "environment change or same alert pattern after 90 days",
            repair_id,
        )
        write_evidence(
            self.config.evidence_dir,
            repair_id,
            EvidenceRecord(
                record_type="Candidate",
                scope=pattern,
                epistemic_status="supported",
                lifecycle_status="candidate",
                source_refs=[f"repair:{repair_id}"],
                detail={"candidate_id": candidate_id, "tool_sequence": tool_sequence},
            ),
        )

    def _transition(self, repair_id: str, target: RepairState, **fields: Any) -> None:
        row = self.store.get_repair(repair_id)
        if row is None:
            raise RuntimeError(f"Repair not found: {repair_id}")
        require_transition(RepairState(row["status"]), target)
        self.store.set_repair_status(repair_id, target.value, **fields)

    def _latest_finished_repair(self, fingerprint: str) -> Any | None:
        rows = [
            row
            for row in self.store.list_repairs(limit=200)
            if row["fingerprint"] == fingerprint and row["status"] in {s.value for s in TERMINAL_STATES}
        ]
        return max(rows, key=lambda row: int(row["finished_at"] or 0)) if rows else None

    def _describe(self, alert: Alert) -> str:
        summary = alert.annotations.get("summary", "")
        description = alert.annotations.get("description", "")
        parts = [f"alertname={alert.labels.get('alertname', 'unknown')}"]
        if summary:
            parts.append(summary)
        if description:
            parts.append(description)
        return "\n".join(parts)[:2_000]

    async def _notify(self, severity: str, title: str, text: str) -> None:
        await self.notifier.notify(severity, title, text)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._owns_http:
            await self.http.aclose()
