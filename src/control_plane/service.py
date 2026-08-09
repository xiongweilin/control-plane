from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import Counter

from .advisories import fetch_security_advisories
from .alerts import alert_fingerprint, fingerprint_pattern
from .approvals import ApprovalManager
from .budget import Budget
from .codex_runner import CodexRunner
from .config import ControlPlaneConfig
from .errors import ErrorClass, TimeoutKind, classify_exec_error, classify_verify_error
from .evidence import EvidenceRecord, write_evidence
from .gitpush import push_with_ssh_fallback
from .metrics import CONTROLLED_IGNORES, MODEL_CONNECTIVITY, MODEL_DRIFT
from .models import Alert, AlertmanagerPayload, AlertResponse
from .notify import Notifier
from .runtime import current_run_id
from .state_machine import TERMINAL_STATES, RepairState, require_transition
from .storage import Store
from .tools import (
    CommandExecutor,
    ToolContext,
    ToolError,
    ToolResult,
    _probe,
    resolve_repo,
    validate_url,
)
from .verifier import Verifier

logger = logging.getLogger(__name__)

HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

recovery_retry_failed = Counter(
    "control_plane_recovery_retry_failed_total",
    "Repairs that failed after a previously verified recovery of the same alert",
    ["pattern"],
)


class RepairRejectedError(RuntimeError):
    pass


class VerificationTimeoutError(RuntimeError):
    """Raised when the deterministic verifier exceeds verify_timeout_seconds."""


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
        self.http = http or httpx.AsyncClient(timeout=30, limits=HTTP_LIMITS)
        self._owns_http = http is None
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._repair_tasks: dict[str, asyncio.Task[Any]] = {}
        self._fingerprint_locks: dict[str, asyncio.Lock] = {}
        self.run_id = config.run_id or current_run_id()
        if hasattr(self.executor, "attach_store"):
            self.executor.attach_store(store)
        if hasattr(agent, "attach_store"):
            agent.attach_store(store)

    @property
    def paused(self) -> bool:
        return self.config.paused or self.store.get_setting("paused", "0") == "1"

    async def ingest(self, payload: AlertmanagerPayload) -> AlertResponse:
        accepted = 0
        deduplicated = 0
        cooldown = 0
        budget_limited = 0
        paused = 0
        ignored = 0
        pending = 0
        for alert in payload.alerts:
            decision = await self._ingest_alert(alert)
            accepted += decision.get("accepted", 0)
            deduplicated += decision.get("deduplicated", 0)
            cooldown += decision.get("cooldown", 0)
            budget_limited += decision.get("budget_limited", 0)
            paused += decision.get("paused", 0)
            ignored += decision.get("ignored", 0)
            pending += decision.get("pending", 0)
        return AlertResponse(
            accepted=accepted,
            deduplicated=deduplicated,
            cooldown=cooldown,
            budget_limited=budget_limited,
            paused=paused,
            ignored=ignored,
            pending=pending,
        )

    async def _ingest_alert(self, alert: Alert) -> dict[str, int]:
        fingerprint = alert_fingerprint(alert)
        alertname = alert.labels.get("alertname", "unknown")
        instance = alert.labels.get("instance", "")
        project = alert.labels.get("project", "")
        container = alert.labels.get("container", "")
        starts_at = int(alert.starts_at.timestamp())

        if alert.status == "resolved":
            self.store.mark_alert_resolved(
                fingerprint,
                int(alert.ends_at.timestamp()) if alert.ends_at else None,
            )
            recovered, evidence = await self._verify_alert_recovery(alert)
            if recovered:
                self.store.set_setting(f"attempt_reset:{fingerprint}", str(int(time.time())))
                await self._cancel_in_progress_repairs(fingerprint)
                await self._notify(
                    "info",
                    f"告警恢复已验证：{alertname}",
                    f"{self._describe(alert)}\n恢复证据：{evidence}",
                )
            else:
                await self._notify(
                    "warning",
                    f"告警 resolved 但恢复未验证：{alertname}",
                    f"{self._describe(alert)}\n未重置自动修复次数；{evidence}",
                )
            return {"deduplicated": 1}

        known = self.store.get_alert(fingerprint)
        self.store.upsert_alert(fingerprint, alertname, instance, project, container, "firing", starts_at)
        self.store.set_setting(
            f"alert_payload:{fingerprint}",
            json.dumps(alert.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        )

        if self._is_noise_alert(alert):
            if known is None and self.config.notify_ignored_noise:
                await self._notify(
                    "info",
                    f"已忽略测试/噪音告警：{alertname}",
                    f"{self._describe(alert)}\n不触发自动修复（instance={instance or '-'}）。",
                )
            return {"ignored": 1}

        if self.paused:
            await self._notify("warning", "控制平面已暂停，告警未处理", f"{alertname}: {self._describe(alert)}")
            return {"paused": 1}

        existing = known
        if existing and self.store.get_repair_state_for_fingerprint(fingerprint) == "in_progress":
            return {"deduplicated": 1}

        now = int(time.time())
        latest = self._latest_finished_repair(fingerprint)
        if latest is not None:
            finished_at = int(latest["finished_at"] or 0)
            if finished_at and now - finished_at < self.config.cooldown_seconds:
                if self.config.notify_cooldown_skip and self._cooldown_notify_due(fingerprint):
                    remaining_min = max(1, (self.config.cooldown_seconds - (now - finished_at)) // 60)
                    await self._notify(
                        "info",
                        f"冷却中，暂不重复修复：{alertname}",
                        f"{self._describe(alert)}\n剩余约 {remaining_min} 分钟，重复告警将被自动跳过。",
                    )
                    self.store.set_setting(f"notified:cooldown:{fingerprint}", str(now))
                return {"cooldown": 1}
            if (latest["error_class"] or "") == ErrorClass.DETERMINISTIC.value:
                # Deterministic failures will reproduce identically; do not burn
                # another attempt automatically (batch2 item 10).
                if self._cooldown_notify_due(fingerprint):
                    await self._notify(
                        "critical",
                        f"确定性失败，不再自动重试：{alertname}",
                        f"{self._describe(alert)}\n"
                        f"上一次失败被判定为确定性（{latest['error_class']}），"
                        "自动重试已抑制；请人工介入。",
                    )
                    self.store.set_setting(f"notified:cooldown:{fingerprint}", str(now))
                return {"cooldown": 1}

        policy = self._alert_policy(fingerprint)
        if policy == "ignore":
            if self._policy_notify_due(fingerprint, "ignore", ttl=6 * 3600):
                await self._notify(
                    "info",
                    f"已忽略告警（策略）：{alertname}",
                    f"{self._describe(alert)}\n如需恢复自动修复：/cp policy {fingerprint} auto",
                )
                self.store.set_setting(f"notified:policy:{fingerprint}:ignore", str(now))
            return {"ignored": 1}
        if policy == "manual":
            pending_raw = self.store.get_setting(f"pending:{fingerprint}")
            if pending_raw and now - int(pending_raw or 0) < 600:
                return {"pending": 1}
            self.store.set_setting(f"pending:{fingerprint}", str(now))
            await self._notify(
                "warning",
                f"告警待决定（手动策略）：{alertname}",
                f"{self._describe(alert)}\n"
                f"选项：\n/cp run {fingerprint} 让模型执行\n"
                f"/cp ignore {fingerprint} 忽略\n"
                f"/cp policy {fingerprint} auto 恢复自动修复",
            )
            return {"pending": 1}

        attempt = 1
        if latest is not None:
            reset_raw = self.store.get_setting(f"attempt_reset:{fingerprint}")
            try:
                reset_at = int(reset_raw) if reset_raw else 0
            except ValueError:
                reset_at = 0
            # 告警已恢复过则重置尝试计数
            attempt = (
                1 if reset_at >= int(latest["finished_at"] or 0) else int(latest["attempt"]) + 1
            )
            if attempt > self.config.max_attempts:
                await self._notify(
                    "critical",
                    f"告警达到最大尝试次数：{alertname}",
                    f"{self._describe(alert)}\nfingerprint={fingerprint}\n已升级，不再自动重试。",
                )
                return {"cooldown": 1}

        return await self._start_repair(alert, attempt)

    async def _start_repair(self, alert: Alert, attempt: int) -> dict[str, int]:
        fingerprint = alert_fingerprint(alert)
        alertname = alert.labels.get("alertname", "unknown")
        if not self.budget.can_spend():
            await self._notify("warning", "Agent 调用预算已耗尽", self._describe(alert))
            return {"budget_limited": 1}

        repair_id = f"repair-{uuid.uuid4().hex[:12]}"
        payload_json = json.dumps(
            alert.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        )
        lock = self._fingerprint_locks.setdefault(fingerprint, asyncio.Lock())
        async with lock:
            # In-process mutual exclusion (second alert for the same fingerprint
            # while a repair is active is deduplicated).
            if self.store.get_repair_state_for_fingerprint(fingerprint) == "in_progress":
                return {"deduplicated": 1}
            self.store.create_repair(repair_id, fingerprint, payload_json, attempt)
            # Persistent lease: guards against a second control-plane instance
            # picking up the same fingerprint concurrently (batch2 item 10).
            if not self.store.acquire_lease(
                fingerprint,
                self.run_id,
                repair_id,
                self.config.lease_ttl_seconds,
            ):
                self.store.set_repair_status(
                    repair_id,
                    RepairState.INTERRUPTED.value,
                    error="lease held by another control-plane instance",
                    finished_at=int(time.time()),
                )
                return {"deduplicated": 1}
            self.store.set_setting(f"repair:{repair_id}:fingerprint", fingerprint)
            pattern = fingerprint_pattern(alert)
            known_candidate = self.store.find_candidate(pattern, ("candidate", "official"))
            hint = ""
            if known_candidate is not None:
                hint = f"\n已知模式：{pattern}（已支持 {known_candidate['times_supported']} 次）"
            await self._notify(
                "info",
                f"开始修复：{alertname}",
                f"repair_id={repair_id}\n{self._describe(alert)}\n"
                f"今日 Agent 预算剩余：{self.budget.remaining()}{hint}\n"
                f"选项：/cp policy {fingerprint} manual|ignore 可改为手动或忽略",
            )
            task = asyncio.create_task(self._run_repair(repair_id, fingerprint, alert))
            self._tasks.add(task)
            self._repair_tasks[fingerprint] = task
            task.add_done_callback(lambda _t: self._repair_tasks.pop(fingerprint, None))
            task.add_done_callback(self._tasks.discard)
            return {"accepted": 1}

    async def _cancel_in_progress_repairs(self, fingerprint: str) -> None:
        task = self._repair_tasks.get(fingerprint)
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def set_alert_policy(self, fingerprint: str, policy: str) -> str:
        if policy not in {"auto", "manual", "ignore"}:
            raise ValueError("policy must be auto|manual|ignore")
        self.store.set_setting(f"policy:{fingerprint}", policy)
        self.store.set_setting(f"pending:{fingerprint}", "")
        await self._notify(
            "info",
            "告警策略已更新",
            f"fingerprint={fingerprint}\n策略：{policy}",
        )
        return f"已设置 {fingerprint} 的策略为 {policy}"

    async def run_manual(self, fingerprint: str) -> str:
        raw = self.store.get_setting(f"alert_payload:{fingerprint}")
        if not raw:
            return f"没有可执行的告警数据：{fingerprint}"
        try:
            alert = Alert.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            return f"告警数据无效：{exc}"
        self.store.set_setting(f"pending:{fingerprint}", "")
        result = await self._start_repair(alert, 1)
        if result.get("accepted"):
            return f"已启动修复：{fingerprint}"
        return "未启动（预算不足或已忽略）"

    async def dispatch_task(self, prompt: str, repo: str = "", project: str = "") -> tuple[str, str]:
        prompt = prompt.strip()
        if not prompt:
            return "", "任务描述为空"
        if len(prompt) > 4_000:
            return "", "任务描述过长（最多 4000 字）"
        if not self.budget.can_spend():
            return "", f"Agent 调用预算已耗尽（今日剩余 0/{self.config.daily_agent_budget}）"
        target = await self._pick_task_repo(repo, project)
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        fingerprint = f"task:{task_id}"
        payload_json = json.dumps(
            {"kind": "task", "prompt": prompt, "repo": target},
            ensure_ascii=False,
        )
        self.store.create_repair(task_id, fingerprint, payload_json, 1)
        await self._notify(
            "info",
            "任务已接收",
            f"task_id={task_id}\n{prompt[:500]}\n目标：{target}\n今日预算剩余：{self.budget.remaining()}",
        )
        task = asyncio.create_task(self._run_task(task_id, target, prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task_id, f"任务已派发：{task_id}\n目标：{target}"

    async def _pick_task_repo(self, repo: str, project: str) -> str:
        if project:
            resolved = self._resolve_project(project)
            if resolved in self.config.allowed_auto_projects:
                candidate = self.config.project_dirs.get(
                    resolved, f"D:\\infrastructure\\compose\\{resolved}"
                )
                if await self._path_exists(candidate):
                    return candidate
        if repo:
            try:
                return resolve_repo(
                    repo,
                    self.config.allowed_repo_roots,
                    blocked=self.config.blocked_paths,
                )
            except ToolError:
                logger.debug("repo %s not allowed; falling back to first available root", repo)
                CONTROLLED_IGNORES.labels(site="repo_fallback").inc()
        for root in self.config.allowed_repo_roots:
            if await self._path_exists(root):
                return root
        return self.config.allowed_repo_roots[0]

    async def _run_task(self, task_id: str, repo: str, prompt: str) -> None:
        try:
            self.store.set_repair_status(task_id, RepairState.PROPOSING.value)
            await self._notify(
                "info",
                "任务 Agent 启动",
                f"task_id={task_id}\n目标: {repo}\n模型: {self.config.model}",
            )
            branch = f"task/{task_id}"
            task_prompt = (
                "你是个人平台的任务 Agent，运行在完整 Codex 工具环境中。\n"
                f"task_id: {task_id}\n工作目录: {repo}\n"
                "用户任务：\n"
                f"{prompt}\n\n"
                "硬约束：\n"
                "- 代码/配置修改必须从 main 创建分支并提交（git checkout -b <branch>；"
                "禁止 push、禁止 force push、禁止删除 main）。\n"
                "- 禁止不可逆操作：删除/清空数据卷或数据库（docker compose down -v、"
                "docker volume rm、DROP/TRUNCATE、删除持久化数据）、"
                "修改凭据/防火墙/sshd、停机或删除含持久化数据的容器。\n"
                "- 运维允许白名单 Compose 项目的 docker compose restart / up -d 与只读诊断；URL 探针与 PromQL 查询。\n"
                "- 完成后最后一条消息总结：做了什么、验证结果、是否创建分支与分支名。"
            )
            result = await self.agent.run_task(
                repair_id=task_id,
                repo=repo,
                prompt=task_prompt,
                run_id=self.run_id,
            )
            self.store.increment_agent_calls(task_id)
            self.budget.spend()
            if result.timed_out:
                raise RuntimeError("任务 Agent 超时")
            if result.exit_code != 0:
                raise RuntimeError(
                    f"任务 Agent 失败（exit {result.exit_code}）: {result.stderr_tail[-1_500:]}"
                )
            summary = (result.last_message or "（无摘要）")[:4_000]
            self.store.set_repair_status(
                task_id,
                RepairState.CLOSED.value,
                finished_at=int(time.time()),
                result=summary,
            )
            await self._notify(
                "info",
                "任务完成",
                f"task_id={task_id}\n{summary}\n回滚：切回 main 并删除分支 {branch}（如已创建）",
            )
        except Exception as exc:
            logger.exception("task failed: %s", task_id)
            self.store.set_repair_status(
                task_id,
                RepairState.FAILED.value,
                error=str(exc)[:2_000],
                finished_at=int(time.time()),
            )
            await self._notify(
                "critical",
                "任务失败",
                f"task_id={task_id}\n{exc}\n"
                f"下一步：查看会话摘要 data/agent-sessions/{task_id}-last.md",
            )

    def dismiss_candidate(self, candidate_id: str) -> str:
        if self.store.dismiss_candidate(candidate_id):
            return f"候选已归档：{candidate_id}"
        return f"候选不存在或不是 candidate 状态：{candidate_id}"

    async def run_digest(self) -> str:
        today = dt.date.today().isoformat()
        if self.store.get_setting("digest:last_date") == today:
            return "今日已整理过"
        candidates = self.store.list_candidates("candidate")[: self.config.digest_max_candidates]
        evidence_files = self._recent_files(self.config.evidence_dir, "*.json", 10)
        sessions = self._recent_files(self.config.data_dir / "agent-sessions", "*-last.md", 5)
        if not candidates and not evidence_files and not sessions:
            await self._notify("info", "每日沉淀整理", "今日无沉淀记录，定时整理任务已执行。")
            self.store.set_setting("digest:last_date", today)
            return "无沉淀记录"
        if not self.budget.can_spend():
            await self._notify("warning", "沉淀整理已跳过", "Agent 调用预算已耗尽。")
            return "预算不足，跳过整理"

        repo = await self._pick_task_repo("", "")
        lines = [
            "你是控制平面的沉淀整理 Agent。审阅以下候选经验、证据文件与会话摘要，输出整理建议。",
            f"今日日期：{today}",
            "硬约束：只输出 KEEP/DROP 行，id 必须来自下面的候选列表；不要发明 id；不要修改任何文件。",
            "",
            "现有候选：",
        ]
        for row in candidates:
            seq = (row["tool_sequence"] or "")[:300]
            lines.append(
                f"- {row['id']} | pattern={row['pattern']} | 支持次数={row['times_supported']} | seq={seq}"
            )
        lines.append("近期证据文件：")
        lines.extend(f"- {path}" for path in evidence_files)
        lines.append("近期会话摘要：")
        lines.extend(f"- {path}" for path in sessions)
        lines.append(
            "输出格式：每条候选一行 `KEEP <id>: <理由>` 或 `DROP <id>: <理由>`；"
            "最后一行给一句总体建议。"
        )

        task_id = f"digest-{today}"
        result = await self.agent.run_task(
            repair_id=task_id,
            repo=repo,
            prompt="\n".join(lines),
            run_id=self.run_id,
        )
        self.budget.spend()
        if result.timed_out or result.exit_code != 0:
            await self._notify(
                "warning",
                "沉淀整理失败",
                f"exit={result.exit_code}\n{result.stderr_tail[-500:]}",
            )
            return "整理失败"

        summary = result.last_message or ""
        candidate_ids = {row["id"] for row in candidates}
        kept: list[str] = []
        dropped: list[str] = []
        for line in summary.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2 or ":" not in parts[1]:
                continue
            action, rest = parts
            candidate_id = rest.split(":", 1)[0].strip()
            if candidate_id not in candidate_ids:
                continue
            if action.upper() == "KEEP" and candidate_id not in kept:
                kept.append(candidate_id)
            elif (
                action.upper() == "DROP"
                and candidate_id not in dropped
                and self.store.dismiss_candidate(candidate_id)
            ):
                dropped.append(candidate_id)

        kept_rows = [row for row in candidates if row["id"] in kept]
        if kept_rows:
            text = (
                f"今日沉淀整理：共 {len(candidates)} 条候选，归档 {len(dropped)} 条，保留 {len(kept)} 条。\n"
                + "\n".join(f"- {row['id']} | {row['pattern']}" for row in kept_rows)
                + "\n回复 /cp promote <id> 晋升，/cp dismiss <id> 归档。"
            )
        else:
            text = f"今日沉淀整理：共 {len(candidates)} 条候选，归档 {len(dropped)} 条，无保留项。"
        await self._notify("info", "每日沉淀整理结果", text)
        self.store.set_setting("digest:last_date", today)
        return text

    @staticmethod
    def _recent_files(directory: Path, pattern: str, limit: int) -> list[str]:
        if not directory.is_dir():
            return []
        return [
            str(path)
            for path in sorted(
                directory.glob(pattern),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:limit]
        ]

    async def digest_loop(self) -> None:
        if not self.config.digest_enabled:
            return
        while True:
            await self._sleep_until_digest_time()
            try:
                await self.run_digest()
            except Exception:
                logger.exception("daily digest failed")
            await asyncio.sleep(86_400)

    async def _sleep_until_digest_time(self) -> None:
        try:
            hour, minute = (int(part) for part in self.config.digest_time.split(":", 1))
        except (ValueError, AttributeError):
            hour, minute = 21, 30
        now = dt.datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

    async def run_env_scan(self) -> list[str]:
        """Daily environment scan. Returns a list of differences; empty means all healthy."""
        differences: list[str] = []

        # ---- local disks ----
        for drive in ("C:\\", "D:\\"):
            try:
                usage = shutil.disk_usage(drive)
                free_gb = usage.free / 1024**3
                if free_gb < self.config.scan_disk_free_gb_min:
                    differences.append(f"本地磁盘 {drive} 剩余仅 {free_gb:.1f}G")
            except OSError as exc:
                differences.append(f"本地磁盘 {drive} 检查失败：{exc}")

        # ---- local docker containers ----
        try:
            output = await self.executor.run(
                [
                    "docker",
                    "ps",
                    "--format",
                    "{{.Names}}\t{{.Status}}",
                ],
                timeout=30,
            )
            for line in output.splitlines():
                if not line.strip():
                    continue
                status = line.split("\t")[-1]
                if "unhealthy" in status or "restarting" in status:
                    differences.append(f"Docker 容器异常：{line.strip()}")
        except ToolError as exc:
            differences.append(f"Docker 状态检查失败：{exc}")

        # ---- prometheus ----
        try:
            response = await self.http.get(f"{self.config.prometheus_url}/-/ready", timeout=15)
            if response.status_code != 200:
                differences.append(f"Prometheus 未就绪（HTTP {response.status_code}）")
        except httpx.HTTPError as exc:
            differences.append(f"Prometheus 不可达：{exc}")
        try:
            response = await self.http.get(f"{self.config.prometheus_url}/api/v1/alerts", timeout=15)
            body = response.json()
            firing = [
                alert.get("labels", {}).get("alertname", "?")
                for alert in body.get("data", {}).get("alerts", [])
                if alert.get("state") == "firing"
            ]
            if firing:
                differences.append(f"Prometheus 有 firing 告警：{', '.join(sorted(set(firing))[:8])}")
        except (httpx.HTTPError, ValueError) as exc:
            differences.append(f"Prometheus 告警查询失败：{exc}")

        # ---- cloud via ssh metratio ----
        async def cloud(cmd: list[str], timeout: int = 30) -> str:
            return await self.executor.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "metratio", *cmd],
                timeout=timeout,
            )

        try:
            disk = await cloud(["df", "-h", "/"])
            last = [line for line in disk.splitlines() if line.strip()][-1]
            parts = last.split()
            if len(parts) >= 4:
                used_pct = parts[4].rstrip("%")
                try:
                    if int(used_pct) > 85:
                        differences.append(f"云端磁盘使用率 {used_pct}%：{last}")
                except ValueError:
                    logger.debug("cloud disk usage line unparsable: %r", last[:200])
                    CONTROLLED_IGNORES.labels(site="disk_usage_parse").inc()
        except ToolError as exc:
            differences.append(f"云端磁盘检查失败：{exc}")

        try:
            cert = await cloud(
                ["openssl", "x509", "-enddate", "-noout", "-in", "/srv/stack/nginx/ssl/fullchain.pem"]
            )
            match = re.search(r"notAfter=([A-Za-z]{3} [0-9]{1,2} [0-9:]{8} [0-9]{4})", cert)
            if match:
                expires = dt.datetime.strptime(match.group(1), "%b %d %H:%M:%S %Y").replace(tzinfo=dt.UTC)
                days = (expires - dt.datetime.now(dt.UTC)).days
                if days < self.config.scan_cert_days_warn:
                    differences.append(f"云端证书 {days} 天后到期")
        except (ToolError, ValueError) as exc:
            differences.append(f"云端证书检查失败：{exc}")

        try:
            relay = (await cloud(["systemctl", "is-active", "webhook-relay"])).strip()
            if relay != "active":
                differences.append(f"云端 webhook-relay 状态：{relay}")
        except ToolError as exc:
            differences.append(f"云端 relay 检查失败：{exc}")

        try:
            fw = await cloud(["sudo", "-n", "firewall-cmd", "--list-all"])
            if "100.64.0.0/10" not in fw or "22/tcp" in fw:
                differences.append("云端防火墙 SSH 规则异常（应仅允许 Tailscale 源）")
        except ToolError as exc:
            differences.append(f"云端防火墙检查失败：{exc}")

        try:
            nginx_status = await cloud(["docker", "ps", "--filter", "name=gateway-nginx", "--format", "{{.Status}}"])
            if not any(
                line.split("\t")[-1].startswith("Up")
                for line in nginx_status.splitlines()
                if line.strip()
            ):
                differences.append("云端 gateway-nginx 未运行")
        except ToolError as exc:
            differences.append(f"云端 gateway-nginx 检查失败：{exc}")

        try:
            updates = await cloud(["sudo", "-n", "dnf", "check-update", "-q", "--security"], timeout=120)
            count = len([line for line in updates.splitlines() if line.strip()])
            if count:
                differences.append(f"云端有 {count} 个安全更新待安装")
        except ToolError as exc:
            differences.append(f"云端安全更新检查失败：{exc}")

        if differences:
            await self._notify(
                "warning",
                "每日环境自检发现差异",
                "；\n".join(differences),
            )
        return differences

    async def scan_loop(self) -> None:
        if not self.config.scan_enabled:
            return
        while True:
            await self._sleep_until_time(self.config.scan_time, fallback=(6, 0))
            try:
                differences = await self.run_env_scan()
                logger.info("env scan finished with %s differences", len(differences))
            except Exception:
                logger.exception("daily env scan failed")
            await asyncio.sleep(86_400)

    async def _sleep_until_time(self, time_spec: str, fallback: tuple[int, int]) -> None:
        try:
            hour, minute = (int(part) for part in time_spec.split(":", 1))
        except (ValueError, AttributeError):
            hour, minute = fallback
        now = dt.datetime.now().astimezone()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

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
                needs_approval = proposal.get("code_changed") or any(
                    row["status"] == "needs_approval"
                    for row in self.store.list_actions(repair_id)
                )
                if needs_approval:
                    await self._await_approval(ctx, repair_id, "apply")
                await self._complete_repair(repair_id, fingerprint, alert, proposal)
            except asyncio.CancelledError:
                cancelled_row = self.store.get_repair(repair_id)
                cancel_kind = (
                    TimeoutKind.APPROVAL.value
                    if cancelled_row is not None
                    and cancelled_row["status"]
                    in {RepairState.NEEDS_APPROVAL.value, RepairState.RECOVERING.value}
                    else TimeoutKind.EXEC.value
                )
                self.store.set_repair_status(
                    repair_id,
                    RepairState.INTERRUPTED.value,
                    finished_at=int(time.time()),
                    timeout_kind=cancel_kind,
                )
                await self._notify("warning", "修复被中断", f"repair_id={repair_id}")
                raise
            except RepairRejectedError:
                return
            except Exception as exc:
                await self._record_failure(repair_id, fingerprint, exc)
            finally:
                await ctx.close()
                self.store.release_lease(fingerprint, self.run_id)

    async def _complete_repair(
        self,
        repair_id: str,
        fingerprint: str,
        alert: Alert,
        proposal: dict[str, Any],
    ) -> None:
        """Verify, settle candidate evidence and close a repaired alert."""
        try:
            report = await asyncio.wait_for(
                self._verify(self._tool_context(repair_id), repair_id, alert),
                timeout=self.config.verify_timeout_seconds,
            )
        except TimeoutError as exc:
            raise VerificationTimeoutError(
                f"Verification timed out after {self.config.verify_timeout_seconds}s "
                "[timeout_kind=verify]"
            ) from exc
        if not report.all_passed:
            raise RuntimeError(f"Verification failed:\n{report.summary}")
        self._transition(repair_id, RepairState.VERIFIED)
        await self._notify("info", "验证通过", f"repair_id={repair_id}\n{report.summary}")
        if self._alert_is_firing(fingerprint):
            await self._create_candidate(self._tool_context(repair_id), repair_id, fingerprint, alert)
        else:
            await self._notify(
                "info",
                "告警已恢复，跳过候选沉淀",
                f"repair_id={repair_id}\n告警在修复完成前已恢复，不沉淀候选经验。",
            )
        branch = proposal.get("branch")
        rollback = (
            f"\n回滚：切回 main 并删除分支 {branch}"
            if branch and proposal.get("code_changed")
            else ""
        )
        await self._notify(
            "info",
            f"修复完成：{alert.labels.get('alertname', 'unknown')}",
            f"repair_id={repair_id}\n{report.summary}{rollback}",
        )
        self._transition(repair_id, RepairState.CLOSED, finished_at=int(time.time()), result=report.summary)

    def _tool_context(self, repair_id: str) -> ToolContext:
        return ToolContext(
            self.config,
            self.store,
            repair_id,
            self.config.patch_dir,
            executor=self.executor,
            http=self.http,
        )

    async def _record_failure(
        self,
        repair_id: str,
        fingerprint: str,
        exc: Exception,
    ) -> None:
        """Record a failure with error class, timeout kind and dual evidence chain."""
        logger.exception("repair failed: %s", repair_id)
        row = self.store.get_repair(repair_id)
        error_text = str(exc)[:2_000]
        error_class = classify_exec_error(error_text)
        timeout_kind = self._extract_timeout_kind(error_text)
        if timeout_kind == TimeoutKind.VERIFY.value or error_text.startswith("Verification failed"):
            error_class = classify_verify_error(error_text)
        previous_error = str(row["error"] or "") if row is not None else ""
        previous_original = str(row["original_error"] or "") if row is not None else ""
        attempt = int(row["attempt"] or 1) if row is not None else 1
        if attempt > 1:
            original_error = previous_original or previous_error or error_text
            recovery_error = error_text
        else:
            original_error = previous_original or error_text
            recovery_error = ""
        self.store.set_repair_status(
            repair_id,
            self._failure_status_for(error_text).value,
            error=error_text,
            error_class=error_class.value,
            timeout_kind=timeout_kind,
            original_error=original_error,
            recovery_error=recovery_error,
            finished_at=int(time.time()),
        )
        write_evidence(
            self.config.evidence_dir,
            repair_id,
            EvidenceRecord(
                record_type="RepairFailed",
                scope=f"repair:{repair_id}",
                epistemic_status="failure",
                lifecycle_status="failed",
                run_id=self.run_id,
                source_refs=[f"fingerprint:{fingerprint}"],
                detail={
                    "error_class": error_class.value,
                    "timeout_kind": timeout_kind,
                    "original_error": original_error[:2_000],
                    "recovery_error": recovery_error[:2_000],
                },
            ),
        )
        try:
            reset_raw = self.store.get_setting(f"attempt_reset:{fingerprint}")
            reset_at = int(reset_raw) if reset_raw else 0
        except ValueError:
            reset_at = 0
        created_at = int(row["created_at"] or 0) if row is not None else 0
        if reset_at and created_at and reset_at <= created_at:
            recovery_retry_failed.labels(pattern=fingerprint_pattern(self._alert_from_payload(row))).inc()
        outcome_state = self._failure_status_for(error_text)
        outcome_title = "修复超时" if outcome_state is RepairState.TIMED_OUT else "修复失败"
        await self._notify(
            "critical",
            outcome_title,
            f"repair_id={repair_id}\n{exc}\n"
            f"状态：{outcome_state.value}；错误分类：{error_class.value}；"
            f"超时类型：{timeout_kind or 'none'}\n\n"
            f"下一步：查看会话摘要 data/agent-sessions/{repair_id}-last.md；"
            "如需人工介入可调用 /v1/control/pause 暂停控制平面。",
        )

    @staticmethod
    def _alert_from_payload(row: Any) -> Alert:
        try:
            return Alert.model_validate(json.loads(row["payload_json"]))
        except (json.JSONDecodeError, ValueError, TypeError):
            return Alert.model_validate(
                {
                    "status": "firing",
                    "labels": {"alertname": "unknown"},
                    "annotations": {},
                    "startsAt": "2026-01-01T00:00:00Z",
                    "endsAt": None,
                    "fingerprint": "unknown",
                }
            )

    @staticmethod
    def _extract_timeout_kind(message: str) -> str:
        match = re.search(r"timeout_kind=(\w+)", message)
        if match:
            return match.group(1)
        if "Command timed out" in message:
            return TimeoutKind.COMM.value
        if "Verification timed out" in message:
            return TimeoutKind.VERIFY.value
        return ""

    @staticmethod
    def _failure_status_for(error_text: str) -> RepairState:
        """Classify a repair-ending error into FAILED or TIMED_OUT.

        An exec timeout without a committed candidate is the only path that
        becomes TIMED_OUT (a terminal state, no recovery). A timeout that
        produced a committed candidate never reaches this classifier: it takes
        the NEEDS_APPROVAL path instead. Everything else stays FAILED.
        """
        if (
            "without a committed candidate" in error_text
            and RepairService._extract_timeout_kind(error_text) == TimeoutKind.EXEC.value
        ):
            return RepairState.TIMED_OUT
        return RepairState.FAILED

    async def _run_codex_agent(
        self,
        ctx: ToolContext,
        repair_id: str,
        alert: Alert,
    ) -> dict[str, Any]:
        self._transition(repair_id, RepairState.PROPOSING)
        repo_root = await self._pick_repo(alert)
        main_repo = repo_root
        branch = f"{self.config.codex_branch_prefix}{repair_id}"
        isolated: Path | None = None
        dirty = await self._check_dirty_workspaces(repo_root)
        if dirty:
            if self.config.dirty_worktree_policy == "reject":
                raise RuntimeError(
                    f"workspace dirty; refusing to run agent "
                    f"(dirty_worktree_policy=reject): {', '.join(dirty)}"
                )
            repo_root, isolated = await self._create_isolated_worktree(repo_root, branch)
            await self._notify(
                "warning",
                "工作目录存在未提交修改，已隔离 worktree",
                f"repair_id={repair_id}\ndirty={', '.join(dirty)}\n"
                f"agent 将在隔离 worktree {isolated} 中运行。",
            )
        await self._notify(
            "info",
            "Agent 启动",
            f"repair_id={repair_id}\n目标仓库: {repo_root}\n分支: {branch}\n模型: {self.config.model}",
        )
        workspaces = await self._capture_workspace_states(main_repo)
        prompt = self._build_agent_prompt(alert, repo_root, repair_id, branch)
        try:
            result = await self.agent.run_task(
                repair_id=repair_id,
                repo=repo_root,
                prompt=prompt,
                run_id=self.run_id,
            )
            self.store.increment_agent_calls(repair_id)
            self.budget.spend()

            changed_workspaces: list[tuple[dict[str, str], str]] = []
            if isolated is not None:
                changed, diff_stat = await self._code_changed(str(isolated), branch)
                if changed:
                    changed_workspaces.append(({"repo": main_repo}, diff_stat))
            else:
                for before in workspaces:
                    changed, diff_stat = await self._code_changed(before["repo"], branch)
                    if changed:
                        changed_workspaces.append((before, diff_stat))
            if len(changed_workspaces) > 1:
                targets = ", ".join(state["repo"] for state, _ in changed_workspaces)
                raise RuntimeError(f"Agent changed multiple repositories: {targets}")

            if changed_workspaces:
                before, diff_stat = changed_workspaces[0]
                actual_repo = before["repo"]
                summary = result.last_message or (
                    "Agent timed out after creating a committed candidate branch"
                    if result.timed_out
                    else "Agent created a committed candidate branch without a summary"
                )
                self._record_codex_action(
                    repair_id,
                    actual_repo,
                    before,
                    branch,
                    diff_stat,
                    summary,
                    changed=True,
                )
                return {
                    "code_changed": True,
                    "branch": branch,
                    "repo": actual_repo,
                    "summary": summary,
                    "timed_out": result.timed_out,
                }

            if result.timed_out:
                raise RuntimeError(
                    "Codex agent timed out without a committed candidate [timeout_kind=exec]"
                )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"Codex agent failed (exit {result.exit_code}): {result.stderr_tail}"
                )
            before = workspaces[0] if workspaces else {"git_head": "no-git", "repo": main_repo}
            self._record_codex_action(
                repair_id,
                main_repo,
                before,
                branch,
                "",
                result.last_message,
                changed=False,
            )
            return {
                "code_changed": False,
                "branch": branch,
                "repo": main_repo,
                "summary": result.last_message,
                "timed_out": False,
            }
        finally:
            if isolated is not None:
                await self._remove_isolated_worktree(main_repo, isolated)
            else:
                restore_errors = await self._restore_workspace_states(workspaces, branch)
                if restore_errors:
                    detail = "; ".join(restore_errors)
                    await self._notify(
                        "critical",
                        "Agent 工作目录恢复失败",
                        f"repair_id={repair_id}\n{detail}",
                    )
                    raise RuntimeError(f"Failed to restore Agent workspaces: {detail}")

    async def _check_dirty_workspaces(self, repo_root: str) -> list[str]:
        dirty: list[str] = []
        for state in await self._capture_workspace_states(repo_root):
            if state["is_git"] != "1":
                continue
            try:
                output = await self.executor.run(
                    ["git", "-C", state["repo"], "status", "--porcelain"],
                    timeout=30,
                )
            except ToolError:
                logger.debug("dirty check failed for %s; treating as clean", state["repo"])
                CONTROLLED_IGNORES.labels(site="dirty_check").inc()
                continue
            if output.strip():
                dirty.append(state["repo"])
        return dirty

    async def _create_isolated_worktree(self, repo: str, branch: str) -> tuple[str, Path]:
        worktree_dir = Path(tempfile.mkdtemp(prefix="cp-iso-"))
        await self.executor.run(
            ["git", "-C", repo, "worktree", "add", "--detach", str(worktree_dir), "main"],
            timeout=120,
        )
        return str(worktree_dir), worktree_dir

    async def _remove_isolated_worktree(self, repo: str, worktree_dir: Path) -> None:
        try:
            await self.executor.run(
                ["git", "-C", repo, "worktree", "remove", "--force", str(worktree_dir)],
                timeout=120,
            )
        except ToolError as exc:
            logger.warning("isolated worktree removal failed for %s: %s", worktree_dir, exc)

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
            "- 运维允许：对任何运行中的 Compose 项目（dify、feedback-analysis-agent、"
            "catalog-ops-automation、observability、feishu-dify-gateway）执行 "
            "docker compose restart / up -d、docker restart 与只读 docker 诊断；URL 探针与 PromQL 查询。",
            "- 禁止不可逆操作：删除/清空数据卷或数据库（docker compose down -v、docker volume rm、"
            "DROP/TRUNCATE、删除持久化数据）、docker compose down、git push --force 或删除 main/受保护分支、"
            "修改凭据/防火墙/sshd、停止或删除含持久化数据的容器（先验证备份再做）。",
            "- 诊断优先只读；无法确认的失败不要伪装成功；不制造无意义的新告警。",
            "- 执行 npm audit 时必须显式使用 --registry=https://registry.npmjs.org，"
            "不得用镜像站不支持 audit API 的结果判断漏洞状态。",
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
                return resolve_repo(
                    repo,
                    self.config.allowed_repo_roots,
                    blocked=self.config.blocked_paths,
                )
            except ToolError:
                logger.debug("repo %s not allowed; falling back to first available root", repo)
                CONTROLLED_IGNORES.labels(site="repo_fallback").inc()
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
            inside = await self.executor.run(
                ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"]
            )
        except ToolError:
            return {"repo": repo, "is_git": "0", "git_head": "no-git", "git_ref": ""}
        if inside.strip().lower() != "true":
            return {"repo": repo, "is_git": "0", "git_head": "no-git", "git_ref": ""}
        head = await self.executor.run(["git", "-C", repo, "rev-parse", "HEAD"])
        try:
            ref = await self.executor.run(
                ["git", "-C", repo, "symbolic-ref", "--quiet", "--short", "HEAD"]
            )
        except ToolError:
            ref = ""
        return {
            "repo": repo,
            "is_git": "1",
            "git_head": head.strip(),
            "git_ref": ref.strip(),
        }

    async def _capture_workspace_states(self, repo_root: str) -> list[dict[str, str]]:
        root = Path(repo_root)
        paths = [root]
        if not (root / ".git").exists() and root.is_dir():
            paths.extend(
                child
                for child in sorted(root.iterdir(), key=lambda item: item.name.lower())
                if child.is_dir() and (child / ".git").exists()
            )
        states: list[dict[str, str]] = []
        seen: set[str] = set()
        for path in paths[:50]:
            normalized = str(path.resolve())
            if normalized.lower() in seen:
                continue
            seen.add(normalized.lower())
            state = await self._capture_repo_state(normalized)
            if state["is_git"] == "1":
                states.append(state)
        return states

    async def _restore_workspace_states(
        self,
        states: list[dict[str, str]],
        candidate_branch: str,
    ) -> list[str]:
        errors: list[str] = []
        for before in states:
            repo = before["repo"]
            try:
                current = await self._capture_repo_state(repo)
                if current["git_head"] == before["git_head"] and current["git_ref"] == before["git_ref"]:
                    continue
                if current["git_ref"] != candidate_branch:
                    errors.append(
                        f"{repo}: current ref {current['git_ref'] or current['git_head']} "
                        "is neither the original ref nor the candidate branch"
                    )
                    continue
                # Never overwrite uncommitted user changes: abandon the restore
                # and record the error instead (batch2 item 8).
                try:
                    dirty = await self.executor.run(
                        ["git", "-C", repo, "status", "--porcelain"],
                        timeout=30,
                    )
                except ToolError as exc:
                    errors.append(f"{repo}: cannot check worktree cleanliness: {exc}")
                    continue
                if dirty.strip():
                    errors.append(
                        f"{repo}: workspace is dirty; restore abandoned to protect "
                        f"uncommitted changes: {dirty.strip()[:200]}"
                    )
                    continue
                if before["git_ref"]:
                    await self.executor.run(
                        ["git", "-C", repo, "switch", "--quiet", before["git_ref"]],
                        timeout=120,
                    )
                else:
                    await self.executor.run(
                        ["git", "-C", repo, "switch", "--quiet", "--detach", before["git_head"]],
                        timeout=120,
                    )
                restored = await self._capture_repo_state(repo)
                if (
                    restored["git_head"] != before["git_head"]
                    or restored["git_ref"] != before["git_ref"]
                ):
                    errors.append(f"{repo}: post-restore ref/head mismatch")
            except (OSError, ToolError) as exc:
                errors.append(f"{repo}: {exc}")
        return errors

    async def _code_changed(self, repo: str, branch: str) -> tuple[bool, str]:
        try:
            await self.executor.run(["git", "-C", repo, "rev-parse", "--verify", branch])
        except ToolError:
            return False, ""
        try:
            output = await self.executor.run(["git", "-C", repo, "diff", "--stat", f"main...{branch}"])
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
            after={
                **({"branch": branch} if changed else {}),
                "diff_stat": diff_stat,
                "summary": summary[:4_000],
            },
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

    async def _await_approval(
        self,
        ctx: ToolContext | None,
        repair_id: str,
        kind: str,
    ) -> None:
        self._transition(repair_id, RepairState.NEEDS_APPROVAL)
        await self.approvals.register(repair_id)
        review = await self._pending_review_summary(repair_id)
        write_evidence(
            self.config.evidence_dir,
            repair_id,
            EvidenceRecord(
                record_type="PendingReview",
                scope=f"repair:{repair_id}",
                epistemic_status="pending",
                lifecycle_status="needs_approval",
                run_id=self.run_id,
                source_refs=[f"repair:{repair_id}"],
                detail={"summary": review[:8_000]},
            ),
        )
        await self._notify(
            "warning",
            "待审批：控制平面修复",
            f"repair_id={repair_id}\n"
            f"{review}\n"
            f"请回复 /cp approve {repair_id} 或 /cp reject {repair_id}；\n"
            f"或调用 POST /v1/approvals/{repair_id}/decision"
            "（action=approve|reject|rollback，需 X-Control-Plane-Key）。",
        )
        decision = await self._wait_approval(repair_id)
        await self.approvals.remove(repair_id)
        await self._apply_approval_decision(repair_id, decision)

    async def _wait_approval(self, repair_id: str) -> str | None:
        """Wait for a human decision; escalate on approval timeout when configured."""
        if self.config.approval_timeout_seconds <= 0:
            return await self.approvals.wait(repair_id)
        try:
            return await asyncio.wait_for(
                self.approvals.wait(repair_id),
                timeout=self.config.approval_timeout_seconds,
            )
        except TimeoutError:
            self._transition(
                repair_id,
                RepairState.ESCALATED,
                finished_at=int(time.time()),
                timeout_kind=TimeoutKind.APPROVAL.value,
                error="approval timeout; escalated for manual intervention",
            )
            await self._notify(
                "critical",
                "审批超时，修复已升级",
                f"repair_id={repair_id}\n"
                f"等待人工审批超过 {self.config.approval_timeout_seconds}s，"
                "已标记 escalated。可用 /cp approve|reject|rollback 继续处理。",
            )
            raise RepairRejectedError() from None

    async def _apply_approval_decision(self, repair_id: str, decision: str | None) -> None:
        """Apply an approval decision; shared by the live flow and post-restart resume."""
        approval_row = self.store.get_approval("repair", repair_id, decision or "")
        decided_by = str(approval_row["decided_by"]) if approval_row else "unknown"
        note = str(approval_row["note"]) if approval_row else ""
        if decision == "approve":
            self._transition(repair_id, RepairState.APPLYING)
            repair_row = self.store.get_repair(repair_id)
            if repair_row is not None:
                self.store.renew_lease(
                    str(repair_row["fingerprint"]),
                    self.run_id,
                    self.config.lease_ttl_seconds,
                )
            await self._apply_code_candidates(None, repair_id)
        elif decision == "rollback":
            await self._rollback(None, repair_id)
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
        write_evidence(
            self.config.evidence_dir,
            repair_id,
            EvidenceRecord(
                record_type="ApprovalDecision",
                scope=f"repair:{repair_id}",
                epistemic_status="confirmed",
                lifecycle_status="decided",
                run_id=self.run_id,
                source_refs=[f"repair:{repair_id}"],
                detail={"decision": decision, "decided_by": decided_by, "note": note},
            ),
        )

    async def _pending_review_summary(self, repair_id: str) -> str:
        """Build the review summary (commit, diff stat, files, impact) for approval."""
        lines: list[str] = []
        for row in self.store.list_actions(repair_id):
            if row["tool"] != "codex_agent":
                continue
            repo = row["target"]
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            branch = after.get("branch", "")
            summary = (after.get("summary") or "").strip()
            if branch:
                lines.append(f"repo={repo}\nbranch={branch}")
                try:
                    commit = (
                        await self.executor.run(
                            ["git", "-C", repo, "rev-parse", "--short", branch],
                            timeout=30,
                        )
                    ).strip()
                    lines.append(f"commit={commit}")
                except ToolError:
                    logger.debug("review summary: rev-parse failed for %s", repo)
                    CONTROLLED_IGNORES.labels(site="review_summary_git").inc()
                diff_stat = ""
                try:
                    diff_stat = await self.executor.run(
                        ["git", "-C", repo, "diff", "--stat", f"main...{branch}"],
                        timeout=30,
                    )
                    if diff_stat.strip():
                        lines.append(f"diff stat:\n{diff_stat.strip()}")
                except ToolError:
                    logger.debug("review summary: diff stat failed for %s", repo)
                    CONTROLLED_IGNORES.labels(site="review_summary_git").inc()
                try:
                    files = await self.executor.run(
                        ["git", "-C", repo, "diff", "--name-only", f"main...{branch}"],
                        timeout=30,
                    )
                    names = [name for name in files.splitlines() if name.strip()][:20]
                    if names:
                        lines.append(f"涉及文件: {', '.join(names)}")
                except ToolError:
                    logger.debug("review summary: diff name-only failed for %s", repo)
                    CONTROLLED_IGNORES.labels(site="review_summary_git").inc()
                if branch and self._dependency_scope_detected(diff_stat, summary):
                    packages = await self._dependency_packages(repo, branch)
                    info = await asyncio.to_thread(fetch_security_advisories, packages)
                    if info.status == "ok":
                        lines.append(
                            f"安全公告: {len(info.advisories)} 条已知公告（{info.source}）；"
                            f"涉及包: {', '.join(packages[:6]) or '未知'}"
                        )
                    else:
                        lines.append(f"安全公告: 无法获取（{info.error}）")
            if summary:
                lines.append(f"说明: {summary[:300]}")
        return "\n".join(lines) or "（无候选摘要）"

    async def _apply_code_candidates(
        self,
        ctx: ToolContext | None,
        repair_id: str,
    ) -> None:
        for row in self.store.list_actions(repair_id):
            if row["tool"] != "codex_agent":
                continue
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            repo = row["target"]
            if not repo:
                continue
            branch = after.get("branch", f"{self.config.candidate_branch_prefix}{repair_id}")
            await self.executor.run(["git", "-C", repo, "checkout", "-q", "main"], timeout=120)
            try:
                await self.executor.run(["git", "-C", repo, "merge", "--ff-only", branch], timeout=120)
            except ToolError:
                await self.executor.run(["git", "-C", repo, "merge", "-q", "--no-edit", branch], timeout=120)
            pushed, detail = await push_with_ssh_fallback(
                self.executor,
                repo,
                remote="origin",
                branch="main",
                timeout=self.config.git_push_timeout_seconds,
                fallback_enabled=self.config.github_ssh_fallback,
                fallback_host=self.config.github_ssh_host_port,
            )
            await self._notify(
                "info",
                "代码候选已合并到 main",
                f"repair_id={repair_id}\nrepo={repo}\npush: {detail}",
            )

    async def resume_pending_approval(self, repair_id: str) -> None:
        """Resume a repair that was awaiting approval when the service restarted.

        Restores the in-process approval waiter so /cp approve|reject|rollback
        keeps working across restarts (batch2 item 7).
        """
        row = self.store.get_repair(repair_id)
        if row is None or row["status"] not in {
            RepairState.NEEDS_APPROVAL.value,
            RepairState.RECOVERING.value,
        }:
            return
        try:
            self._transition(repair_id, RepairState.RECOVERING)
            await self.approvals.register(repair_id)
            self._transition(repair_id, RepairState.NEEDS_APPROVAL)
            review = await self._pending_review_summary(repair_id)
            await self._notify(
                "warning",
                "待审批（重启后恢复）",
                f"repair_id={repair_id}\n{review}\n"
                f"请回复 /cp approve {repair_id} 或 /cp reject {repair_id}；"
                "重启后审批仍有效。",
            )
            decision = await self._wait_approval(repair_id)
            await self.approvals.remove(repair_id)
            if decision == "approve":
                await self._apply_approval_decision(repair_id, decision)
                await self._finish_resumed_repair(repair_id)
            else:
                await self._apply_approval_decision(repair_id, decision)
        except RepairRejectedError:
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("resume of pending approval failed: %s", repair_id)
            self.store.set_repair_status(
                repair_id,
                RepairState.FAILED.value,
                error=str(exc)[:2_000],
                finished_at=int(time.time()),
            )

    async def _finish_resumed_repair(self, repair_id: str) -> None:
        row = self.store.get_repair(repair_id)
        if row is None:
            return
        try:
            alert = Alert.model_validate(json.loads(row["payload_json"]))
        except (json.JSONDecodeError, ValueError):
            self.store.set_repair_status(
                repair_id,
                RepairState.CLOSED.value,
                finished_at=int(time.time()),
                result="approved and applied",
            )
            return
        fingerprint = row["fingerprint"]
        await self._complete_repair(repair_id, fingerprint, alert, {"code_changed": True})

    async def _git_repo_roots(self) -> list[str]:
        roots: list[str] = []
        for root in self.config.allowed_repo_roots:
            if not await self._path_exists(root):
                continue
            try:
                await self.executor.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"], timeout=30)
            except ToolError:
                logger.debug("repo root %s is not a git work tree; skipping", root)
                CONTROLLED_IGNORES.labels(site="repo_enum_git").inc()
                continue
            roots.append(root)
        return roots

    async def list_candidate_branches_for_cleanup(
        self,
        repos: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Dry-run enumeration of stale candidate branches (merged/rejected/expired).

        Read-only; never deletes anything (batch2 item 9).
        """
        prefix = self.config.candidate_branch_prefix
        retention_seconds = self.config.candidate_retention_days * 86_400
        now = int(time.time())
        results: list[dict[str, Any]] = []
        for repo in repos or await self._git_repo_roots():
            try:
                merged_output = await self.executor.run(
                    ["git", "-C", repo, "branch", "--merged", "main"],
                    timeout=60,
                )
                refs_output = await self.executor.run(
                    [
                        "git",
                        "-C",
                        repo,
                        "for-each-ref",
                        "--format=%(refname:short)%09%(committerdate:unix)",
                        f"refs/heads/{prefix}*",
                    ],
                    timeout=60,
                )
            except ToolError:
                logger.debug("candidate branch enumeration failed for %s", repo)
                CONTROLLED_IGNORES.labels(site="repo_enum_git").inc()
                continue
            merged = {line.strip().lstrip("* ").strip() for line in merged_output.splitlines() if line.strip()}
            for line in refs_output.splitlines():
                branch, separator, raw_ts = line.partition("\t")
                branch = branch.strip()
                if not branch.startswith(prefix):
                    continue
                reasons: list[str] = []
                if branch in merged:
                    reasons.append("merged")
                repair_id = branch[len(prefix) :]
                repair = self.store.get_repair(repair_id) if repair_id.startswith("repair-") else None
                if repair is not None and (
                    repair["status"] == "rolled_back" or repair["result"] == "rejected"
                ):
                    reasons.append("rejected")
                try:
                    age_seconds = now - int(raw_ts or 0)
                except ValueError:
                    age_seconds = 0
                age_days = round(age_seconds / 86_400, 1)
                if age_seconds > retention_seconds:
                    reasons.append("expired")
                if reasons:
                    results.append(
                        {
                            "repo": repo,
                            "branch": branch,
                            "repair_id": repair_id,
                            "reasons": reasons,
                            "age_days": age_days,
                        }
                    )
        return results

    async def cleanup_candidate_branches(
        self,
        repos: list[str] | None = None,
        *,
        apply: bool = False,
    ) -> list[dict[str, Any]]:
        """List stale candidate branches; delete only when ``apply=True`` is explicit."""
        branches = await self.list_candidate_branches_for_cleanup(repos)
        if not apply:
            return branches
        for entry in branches:
            try:
                await self.executor.run(
                    ["git", "-C", entry["repo"], "branch", "-D", entry["branch"]],
                    timeout=60,
                )
                entry["deleted"] = True
            except ToolError as exc:
                entry["deleted"] = False
                entry["error"] = str(exc)[:500]
        return branches

    def _dependency_scope_detected(self, diff_stat: str, summary: str) -> bool:
        material = f"{diff_stat} {summary}".lower()
        markers = (
            "requirements",
            "pyproject",
            "package.json",
            "package-lock",
            "pnpm-lock",
            "uv.lock",
            "poetry.lock",
            "npm audit",
            "pip",
            "依赖",
            "dependency",
            "dependencies",
        )
        return any(marker in material for marker in markers)

    async def _dependency_packages(self, repo: str, branch: str) -> list[str]:
        """Best-effort package extraction from a candidate branch (local git only)."""
        names: list[str] = []
        for manifest in ("requirements.txt", "package.json", "pyproject.toml"):
            try:
                content = await self.executor.run(
                    ["git", "-C", repo, "show", f"{branch}:{manifest}"],
                    timeout=30,
                )
            except ToolError:
                logger.debug("candidate branch cleanup failed for %s", repo)
                CONTROLLED_IGNORES.labels(site="repo_enum_git").inc()
                continue
            if manifest == "requirements.txt":
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith(("#", "-", "[", "git+")):
                        continue
                    name = re.split(r"[=<>~!]", line, maxsplit=1)[0].strip()
                    if name:
                        names.append(name)
            elif manifest == "package.json":
                try:
                    data = json.loads(content)
                    for section in ("dependencies", "devDependencies", "peerDependencies"):
                        names.extend(data.get(section, {}).keys())
                except json.JSONDecodeError:
                    logger.debug("package.json unparsable on %s:%s; skipping", repo, branch)
                    continue
            else:  # pyproject.toml
                for line in content.splitlines():
                    line = line.strip()
                    if "=" in line and "[" not in line:
                        candidate = line.split("=", 1)[0].strip()
                        if candidate:
                            names.append(candidate)
        return [name for name in dict.fromkeys(names) if name][:12]

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

        # 语义证据：按告警类型推导确定性检查
        alertname = alert.labels.get("alertname", "")
        instance = alert.labels.get("instance", "")
        if alertname == "ServiceDown" and instance.startswith(("http://", "https://")):
            try:
                validate_url(instance, self.config.allowed_url_origins)
                tool_results["probe_urls"].append(
                    {"url": instance, "expected": [200, 301, 302, 307, 401]}
                )
                if alert.labels.get("job") == "blackbox":
                    tool_results["promql"][f"probe_success:{instance}"] = {
                        "query": f'probe_success{{instance="{instance}"}}',
                        "expected": 1,
                    }
            except ToolError:
                logger.debug("probe construction skipped for %s", instance)
                CONTROLLED_IGNORES.labels(site="probe_build").inc()
        elif alertname == "PrometheusScrapeFailed" and ":" in instance:
            tool_results["promql"][f"up:{instance}"] = {
                "query": f'up{{instance="{instance}"}}',
                "expected": 1,
            }
        elif instance.startswith(("http://", "https://")):
            try:
                validate_url(instance, self.config.allowed_url_origins)
                tool_results["probe_urls"].append(instance)
            except ToolError:
                logger.debug("probe url not allowed: %s", instance)
                CONTROLLED_IGNORES.labels(site="probe_build").inc()

        project = alert.labels.get("project", "")
        resolved_project = self._resolve_project(project)
        if resolved_project in self.config.allowed_auto_projects:
            actions.append({"tool": "container_status", "target": resolved_project})
        elif not tool_results["probe_urls"] and not tool_results["promql"] and not tool_results["repos"]:
            # 无代码/探针证据时，用环境容器基线作为确定性证据
            for allowed in self.config.allowed_auto_projects:
                resolved = self._resolve_project(allowed)
                project_dir = self.config.project_dirs.get(allowed)
                if project_dir and await self._path_exists(project_dir):
                    actions.append({"tool": "container_status", "target": resolved})
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
                        "{{.Names}}\t{{.Status}}",
                    ]
                )
            except ToolError as exc:
                failures.append(f"{project}: {exc}")
                continue
            lines = [line for line in output.splitlines() if line.strip()]
            if not lines:
                failures.append(f"{project}: no running containers")
                continue
            for line in lines:
                status = line.split("\t")[-1]
                if not status.startswith("Up"):
                    failures.append(f"{project}: container not up ({status})")
                elif "unhealthy" in status or "restarting" in status:
                    failures.append(f"{project}: container unhealthy/restarting ({status})")
        if failures:
            return False, "; ".join(failures), "container_status"
        return True, "all target containers running", "container_status"

    async def _check_promql(
        self,
        query: str,
        expected: float | None = None,
    ) -> tuple[bool, str, str]:
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
        if expected is not None:
            for result in results:
                try:
                    value = float(result["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    return False, f"invalid sample for query: {query}", "promql"
                if abs(value - expected) > 1e-9:
                    return False, f"value {value} != expected {expected} ({query})", "promql"
        return True, "query returned results", "promql"

    async def _verify_alert_recovery(self, alert: Alert) -> tuple[bool, str]:
        """Verify recovery from current deterministic evidence, not Alertmanager status alone."""
        alertname = alert.labels.get("alertname", "")
        instance = alert.labels.get("instance", "")
        raw_project = alert.labels.get("project", "")
        resolved_project = self._resolve_project(raw_project)
        project = (
            resolved_project
            if resolved_project in self.config.allowed_auto_projects
            else raw_project
        )

        if alertname == "DevEnvironmentUnhealthy":
            ok, message, _ = await self._check_promql(
                "dev_environment_health == 1 and on() "
                "((time() - dev_maintenance_last_run_ts) < 25200)",
                expected=1,
            )
            return ok, message

        if alertname == "MaintenanceMetricsStale":
            ok, message, _ = await self._check_promql(
                "(time() - dev_maintenance_last_run_ts) < bool 25200",
                expected=1,
            )
            return ok, message

        if alertname == "ServiceDown" and instance.startswith(("http://", "https://")):
            try:
                validate_url(instance, self.config.allowed_url_origins)
            except ToolError as exc:
                return False, f"recovery probe rejected: {exc}"
            return await _probe(self.http, instance)

        if alertname == "PrometheusScrapeFailed" and instance:
            escaped = instance.replace("\\", "\\\\").replace('"', '\\"')
            ok, message, _ = await self._check_promql(
                f'up{{instance="{escaped}"}}',
                expected=1,
            )
            return ok, message

        if project in self.config.allowed_auto_projects:
            ok, message, _ = await self._check_containers([project])
            return ok, message

        return False, f"no deterministic recovery validator for {alertname or 'unknown'}"

    async def _check_logs(
        self,
        target: str,
        since_minutes: int = 10,
        patterns: tuple[str, ...] = ("Traceback", "panic:", "FATAL"),
    ) -> tuple[bool, str, str]:
        if ":" not in target:
            return True, "no log target", "logs"
        project, service = target.split(":", 1)
        try:
            output = await self.executor.run(
                [
                    "docker",
                    "logs",
                    "--since",
                    f"{since_minutes}m",
                    "--tail",
                    "200",
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
        for pattern in patterns:
            if pattern in output:
                return False, f"log contains {pattern}", "logs"
        return True, "no fatal patterns in recent logs", "logs"

    async def _check_git(self, repo: str, branch: str) -> tuple[bool, str, str]:
        try:
            await self.executor.run(["git", "-C", repo, "rev-parse", "--is-inside-work-tree"])
        except ToolError:
            return True, "not a git repository; skipping git diff", "git"
        try:
            diff = await self.executor.run(["git", "-C", repo, "diff", f"main...{branch}", "--stat"])
            dirty = await self.executor.run(["git", "-C", repo, "status", "--porcelain"])
        except ToolError as exc:
            return False, f"git check failed: {exc}", "git"
        allowed, message = Verifier.diff_allowed(repo, diff)
        if not allowed:
            return False, message, "git"
        if dirty.strip():
            return False, f"workspace is dirty after repair: {dirty.strip()[:200]}", "git"
        return True, message, "git"

    def _is_noise_alert(self, alert: Alert) -> bool:
        alertname = alert.labels.get("alertname", "")
        instance = alert.labels.get("instance", "")
        if alertname in self.config.test_alert_alertnames:
            return True
        return any(instance.startswith(prefix) for prefix in self.config.test_alert_instance_prefixes)

    def _cooldown_notify_due(self, fingerprint: str) -> bool:
        key = f"notified:cooldown:{fingerprint}"
        raw = self.store.get_setting(key)
        if not raw:
            return True
        try:
            last = int(raw)
        except ValueError:
            return True
        return int(time.time()) - last >= self.config.cooldown_seconds

    def _alert_policy(self, fingerprint: str) -> str:
        raw = self.store.get_setting(f"policy:{fingerprint}")
        return raw if raw in {"auto", "manual", "ignore"} else self.config.default_alert_policy

    def _alert_is_firing(self, fingerprint: str) -> bool:
        row = self.store.get_alert(fingerprint)
        return row is not None and row["status"] == "firing"

    def _policy_notify_due(self, fingerprint: str, suffix: str, ttl: int) -> bool:
        key = f"notified:policy:{fingerprint}:{suffix}"
        raw = self.store.get_setting(key)
        if not raw:
            return True
        try:
            last = int(raw)
        except ValueError:
            return True
        return int(time.time()) - last >= ttl

    async def _rollback(self, ctx: ToolContext | None, repair_id: str) -> None:
        try:
            repair_row = self.store.get_repair(repair_id)
            payload = json.loads(str(repair_row["payload_json"]) if repair_row is not None else "{}")
            project = payload.get("labels", {}).get("project", "")
        except (json.JSONDecodeError, TypeError):
            project = ""
        for row in reversed(self.store.list_actions(repair_id)):
            tool = row["tool"]
            if tool == "codex_agent":
                after = json.loads(row["after_json"]) if row["after_json"] else {}
                repo = row["target"]
                branch = after.get("branch", f"{self.config.candidate_branch_prefix}{repair_id}")
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
                await self._notify(
                    "info",
                    "已知模式再次复现",
                    f"pattern={pattern}\n已更新候选经验（第 {int(existing['times_supported']) + 1} 次支持）。",
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
        await self._notify(
            "info",
            "已沉淀候选经验",
            f"pattern={pattern}\ncandidate_id={candidate_id}\n试运行 {self.config.candidate_trial_days} 天。",
        )
        advisory_detail: dict[str, Any] = {}
        for row in actions:
            if row["tool"] != "codex_agent":
                continue
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            branch = after.get("branch", "")
            diff_stat = after.get("diff_stat", "")
            summary = after.get("summary", "")
            if branch and self._dependency_scope_detected(diff_stat, summary):
                packages = await self._dependency_packages(row["target"], branch)
                info = await asyncio.to_thread(fetch_security_advisories, packages)
                advisory_detail = {
                    "advisories": info.to_dict(),
                    "packages": packages,
                }
                break
        write_evidence(
            self.config.evidence_dir,
            repair_id,
            EvidenceRecord(
                record_type="Candidate",
                scope=pattern,
                epistemic_status="supported",
                lifecycle_status="candidate",
                run_id=self.run_id,
                source_refs=[f"repair:{repair_id}"],
                detail={
                    "candidate_id": candidate_id,
                    "tool_sequence": tool_sequence,
                    **advisory_detail,
                },
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

    async def _opencodex_models(self) -> list[str] | None:
        """Read-only model-list probe against the configured OpenCodex proxy."""
        from .opencodex import OpenCodexClient

        client = OpenCodexClient(
            self.config.opencodex_base_url,
            self.config.model,
            timeout_seconds=self.config.opencodex_timeout_seconds,
            api_key=self.config.opencodex_api_key,
        )
        try:
            return await client.list_models()
        finally:
            await client.close()

    async def check_model_sources(self) -> dict[str, Any]:
        """Minimal connectivity regression over the three model sources.

        Sources: (1) the Codex CLI itself, (2) the OpenCodex proxy endpoint,
        (3) the configured default model present in the proxy model list.
        Read-only; never modifies runtime state. Results are also published to
        the ``control_plane_model_connectivity`` gauge.
        """
        from .codex_runner import CodexCliUnavailableError

        cli: dict[str, Any] = {"ok": False, "path": "", "version": "", "error": ""}
        try:
            path, version = self.agent.cli_info()
            cli.update(ok=True, path=str(path), version=version)
        except CodexCliUnavailableError as exc:
            cli["error"] = str(exc)

        models = await self._opencodex_models()
        opencodex: dict[str, Any] = {
            "ok": models is not None,
            "model_count": len(models) if models is not None else 0,
            "error": "" if models is not None else "proxy unreachable or error response",
        }
        model: dict[str, Any] = {"ok": False, "configured": self.config.model, "error": "opencodex unreachable"}
        if models is not None:
            model["ok"] = self.config.model in models
            model["error"] = (
                ""
                if model["ok"]
                else "configured model missing from OpenCodex model list"
            )

        MODEL_CONNECTIVITY.labels(source="cli").set(1 if cli["ok"] else 0)
        MODEL_CONNECTIVITY.labels(source="opencodex").set(1 if opencodex["ok"] else 0)
        MODEL_CONNECTIVITY.labels(source="model").set(1 if model["ok"] else 0)
        return {"cli": cli, "opencodex": opencodex, "model": model}

    async def check_model_drift(self) -> dict[str, Any]:
        """Read-only drift check for the OpenCodex model directory.

        Compares the live model id set against the recorded baseline; a change
        updates the baseline and is reported (metric + notification), never
        blocking. Only the model id set is stored — never model content.
        """
        models = await self._opencodex_models()
        if models is None:
            return {"drifted": False, "reachable": False, "detail": "proxy unreachable"}
        current = sorted(models)
        baseline_raw = self.store.get_setting("models:baseline", "")
        if not baseline_raw:
            # No prior baseline: establish one without reporting drift (nothing
            # to compare against on the very first run).
            self.store.set_setting("models:baseline", ",".join(current))
            MODEL_DRIFT.set(0)
            return {"drifted": False, "reachable": True, "detail": "baseline established"}
        baseline = [m for m in baseline_raw.split(",") if m]
        drifted = baseline != current
        self.store.set_setting("models:baseline", ",".join(current))
        MODEL_DRIFT.set(1 if drifted else 0)
        if drifted:
            detail = {
                "added": sorted(set(current) - set(baseline)),
                "removed": sorted(set(baseline) - set(current)),
            }
            await self._notify(
                "warning",
                "OpenCodex 模型清单变化",
                f"模型目录漂移检测：新增 {len(detail['added'])} 个、移除 {len(detail['removed'])} 个。"
                f"新增: {', '.join(detail['added'][:8]) or '无'}；"
                f"移除: {', '.join(detail['removed'][:8]) or '无'}",
            )
            return {"drifted": True, "reachable": True, "detail": detail}
        return {"drifted": False, "reachable": True, "detail": "unchanged"}

    async def startup_model_preflight(self) -> dict[str, Any]:
        """Startup preflight for the configured default model (non-blocking).

        Reports connectivity/degradation via metrics and a notification so the
        control plane still serves when the model layer is down. Called once
        from the app lifespan when ``model_preflight_enabled`` is set.
        """
        sources = await self.check_model_sources()
        problems: list[str] = []
        if not sources["cli"]["ok"]:
            problems.append(f"codex CLI 不可用：{sources['cli']['error']}")
        if not sources["opencodex"]["ok"]:
            problems.append("OpenCodex 代理不可达（连通性预检失败）")
        elif not sources["model"]["ok"]:
            problems.append(
                f"默认模型 {sources['model']['configured']} 不在 OpenCodex 模型清单中"
            )
        if problems:
            await self._notify(
                "warning",
                "模型启动预检未通过",
                "\n".join(problems),
            )
        return {"ok": not problems, "sources": sources, "problems": problems}

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._owns_http:
            await self.http.aclose()
