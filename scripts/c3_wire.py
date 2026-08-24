from __future__ import annotations

from pathlib import Path


def ensure_replace(path: str, old: str, new: str, count: int = 1) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8-sig")
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:140]!r}")
    p.write_text(text.replace(old, new, count), encoding="utf-8")


# storage: two independent lineage columns and canonical success anti-downgrade.
ensure_replace(
    "src/control_plane/storage.py",
    "                    restoration_proof_refs_json TEXT NOT NULL DEFAULT '[]',\n                    resolution_updated_at INTEGER,",
    "                    restoration_proof_refs_json TEXT NOT NULL DEFAULT '[]',\n                    resolution_basis_refs_json TEXT NOT NULL DEFAULT '[]',\n                    resolution_updated_at INTEGER,",
)
ensure_replace(
    "src/control_plane/storage.py",
    "        self._ensure_column(\n            \"repairs\", \"restoration_proof_refs_json\", \"TEXT NOT NULL DEFAULT '[]'\"\n        )\n        self._ensure_column(\"repairs\", \"resolution_updated_at\", \"INTEGER\")",
    "        self._ensure_column(\n            \"repairs\", \"restoration_proof_refs_json\", \"TEXT NOT NULL DEFAULT '[]'\"\n        )\n        self._ensure_column(\n            \"repairs\", \"resolution_basis_refs_json\", \"TEXT NOT NULL DEFAULT '[]'\"\n        )\n        self._ensure_column(\"repairs\", \"resolution_updated_at\", \"INTEGER\")",
)
ensure_replace(
    "src/control_plane/storage.py",
    "                \"resolution_kind, restoration_proof_refs_json, resolution_updated_at, \"\n                \"created_at, updated_at\"\n                \") VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)\",",
    "                \"resolution_kind, restoration_proof_refs_json, resolution_basis_refs_json, \"\n                \"resolution_updated_at, created_at, updated_at\"\n                \") VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)\",",
)
ensure_replace(
    "src/control_plane/storage.py",
    "                    ResolutionKind.UNRESOLVED.value,\n                    \"[]\",\n                    None,\n                    now,",
    "                    ResolutionKind.UNRESOLVED.value,\n                    \"[]\",\n                    \"[]\",\n                    None,\n                    now,",
)
old_resolution = '''    def set_repair_resolution(
        self,
        repair_id: str,
        *,
        resolution_kind: ResolutionKind | str,
        restoration_status: RestorationStatus | str,
        proof_refs: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Persist the orthogonal resolution axes without changing RepairState.

        C2 intentionally establishes storage and structural invariants only. It does
        not decide who is authorized to assert restoration; C3 owns that question.
        """
        kind, restoration, refs = normalize_repair_resolution(
            resolution_kind, restoration_status, proof_refs
        )
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE repairs SET resolution_kind=?, restoration_status=?, "
                "restoration_proof_refs_json=?, resolution_updated_at=? WHERE id=?",
                (
                    kind.value,
                    restoration.value,
                    json.dumps(list(refs)),
                    int(time.time()),
                    repair_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown repair: {repair_id}")
'''
new_resolution = '''    def set_repair_resolution(
        self,
        repair_id: str,
        *,
        resolution_kind: ResolutionKind | str,
        restoration_status: RestorationStatus | str,
        proof_refs: tuple[str, ...] | list[str] = (),
        basis_refs: tuple[str, ...] | list[str] = (),
    ) -> None:
        """Persist orthogonal disposition/restoration lineage without lifecycle authority."""
        kind, restoration, proofs, bases = normalize_repair_resolution(
            resolution_kind, restoration_status, proof_refs, basis_refs
        )
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE repairs SET resolution_kind=?, restoration_status=?, "
                "restoration_proof_refs_json=?, resolution_basis_refs_json=?, "
                "resolution_updated_at=? WHERE id=?",
                (
                    kind.value,
                    restoration.value,
                    json.dumps(list(proofs)),
                    json.dumps(list(bases)),
                    int(time.time()),
                    repair_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown repair: {repair_id}")
'''
ensure_replace("src/control_plane/storage.py", old_resolution, new_resolution)
ensure_replace(
    "src/control_plane/storage.py",
    "        if work is None and run is None:\n            return True\n        work_status = {",
    "        if work is None and run is None:\n            return True\n        canonical_success = (\n            work is not None\n            and run is not None\n            and getattr(work, \"status\", \"\") == \"completed\"\n            and getattr(run, \"status\", \"\") == \"succeeded\"\n        )\n        if canonical_success:\n            if status not in {\"verified\", \"closed\"}:\n                raise ValueError(\n                    \"canonical successful terminal pair cannot be downgraded by legacy lifecycle\"\n                )\n            return True\n        work_status = {",
)

# portable human approval: rollback gets exact explicit grants; reject gets none.
ensure_replace(
    "src/control_plane/portable_authority.py",
    '        specs = list(operation_specs or []) if action == "approve" else []',
    '        specs = list(operation_specs or []) if action in {"approve", "rollback"} else []',
)
ensure_replace(
    "src/control_plane/portable_authority.py",
    '            or getattr(decision, "selected_option", None) != "approve"\n        ):',
    '            or getattr(decision, "selected_option", None) not in {"approve", "rollback"}\n        ):',
)

# service wiring.
ensure_replace(
    "src/control_plane/service.py",
    "from .budget import Budget\n",
    "from .budget import Budget\nfrom .closure_authority import ClosureAuthority, RollbackExecutionReceipt\n",
)
ensure_replace(
    "src/control_plane/service.py",
    "        self.portable_authority = None\n",
    "        self.portable_authority = None\n        self.closure_authority: ClosureAuthority | None = None\n",
)
ensure_replace(
    "src/control_plane/service.py",
    "            self.portable_authority = PortableRuntimeAuthority(\n                portable_runtime,\n                legacy_store=store,\n                version_resolver=self._resolve_git_version_for_authority,\n            )\n            self.capability_service = portable_runtime.capabilities",
    "            self.portable_authority = PortableRuntimeAuthority(\n                portable_runtime,\n                legacy_store=store,\n                version_resolver=self._resolve_git_version_for_authority,\n            )\n            self.closure_authority = ClosureAuthority(store, portable_runtime.store)\n            self.capability_service = portable_runtime.capabilities",
)

old_rollback_decision = '''            reconciliation_required = await self._rollback(None, repair_id)
            if reconciliation_required:
                message = (
                    "rollback did not complete; reconciliation or owner-authorized recovery is required "
                    "[reconciliation-required]"
                )
                if self.portable_authority is not None:
                    self.portable_authority.mark_reconciliation_required(repair_id, message)
                self.store.set_repair_status(
                    repair_id,
                    RepairState.RECOVERING.value,
                    error=message,
                    finished_at=int(time.time()),
                )
                await self._notify("warning", "回滚结果待确认", f"repair_id={repair_id}\\n{message}")
                raise RepairRejectedError()
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
'''
new_rollback_decision = '''            receipts = await self._rollback(None, repair_id)
            if not receipts or any(receipt.status != "succeeded" for receipt in receipts):
                message = (
                    "rollback did not complete; reconciliation or owner-authorized recovery is required "
                    "[reconciliation-required]"
                )
                if self.portable_authority is not None:
                    self.portable_authority.mark_reconciliation_required(repair_id, message)
                self.store.set_repair_status(
                    repair_id,
                    RepairState.RECOVERING.value,
                    error=message,
                    finished_at=int(time.time()),
                )
                await self._notify("warning", "回滚结果待确认", f"repair_id={repair_id}\\n{message}")
                raise RepairRejectedError()
            if self.closure_authority is None:
                raise RuntimeError("rollback disposition requires ClosureAuthority")
            self.closure_authority.record_rolled_back(repair_id, receipts)
            await self._notify("warning", "修复已回滚", f"repair_id={repair_id}")
            raise RepairRejectedError()
        else:
            await self._record_canonical_human_approval(
                repair_id,
                decided_by=decided_by,
                principal_ref=self.config.owner_principal,
                principal_source="control-plane-api-key",
                action="reject",
                note=note,
            )
            if self.closure_authority is None:
                raise RuntimeError("rejection disposition requires ClosureAuthority")
            self.closure_authority.close_rejected(repair_id)
            await self._notify("info", "修复已被拒绝", f"repair_id={repair_id}")
            raise RepairRejectedError()
'''
ensure_replace("src/control_plane/service.py", old_rollback_decision, new_rollback_decision)

old_complete_tail = '''        self._transition(repair_id, RepairState.VERIFIED)
        await self._notify("info", "验证通过", f"repair_id={repair_id}\\n{report.summary}")
        if self._alert_is_firing(fingerprint):
            await self._create_candidate(self._tool_context(repair_id), repair_id, fingerprint, alert)
        else:
            await self._notify(
                "info",
                "告警已恢复，跳过候选沉淀",
                f"repair_id={repair_id}\\n告警在修复完成前已恢复，不沉淀候选经验。",
            )
        branch = proposal.get("branch")
        rollback = (
            f"\\n回滚：切回 main 并删除分支 {branch}"
            if branch and proposal.get("code_changed")
            else ""
        )
        await self._notify(
            "info",
            f"修复完成：{alert.labels.get('alertname', 'unknown')}",
            f"repair_id={repair_id}\\n{report.summary}{rollback}",
        )
        self._transition(repair_id, RepairState.CLOSED, finished_at=int(time.time()), result=report.summary)
'''
new_complete_tail = '''        self._transition(repair_id, RepairState.VERIFIED)
        if self.closure_authority is None:
            raise RuntimeError("successful repair requires ClosureAuthority")
        self.closure_authority.close_restored(repair_id)

        branch = proposal.get("branch")
        rollback = (
            f"\\n回滚：切回 main 并删除分支 {branch}"
            if branch and proposal.get("code_changed")
            else ""
        )
        try:
            await self._notify("info", "验证通过", f"repair_id={repair_id}\\n{report.summary}")
            await self._notify(
                "info",
                f"修复完成：{alert.labels.get('alertname', 'unknown')}",
                f"repair_id={repair_id}\\n{report.summary}{rollback}",
            )
        except Exception:
            logger.exception("completion notification failed after authoritative closure: %s", repair_id)

        # Learning/sedimentation is ancillary work after authoritative closure.
        try:
            if self._alert_is_firing(fingerprint):
                await self._create_candidate(self._tool_context(repair_id), repair_id, fingerprint, alert)
            else:
                await self._notify(
                    "info",
                    "告警已恢复，跳过候选沉淀",
                    f"repair_id={repair_id}\\n告警在修复完成前已恢复，不沉淀候选经验。",
                )
        except Exception:
            logger.exception("candidate sedimentation failed after authoritative closure: %s", repair_id)
'''
ensure_replace("src/control_plane/service.py", old_complete_tail, new_complete_tail)

old_task_close = '''            if self._task_result_already_verified(repair_id):
                task_result = self._canonical_verification_summary(repair_id)
                self.store.set_repair_status(
                    repair_id,
                    RepairState.CLOSED.value,
                    finished_at=int(time.time()),
                    result=task_result or "task result verified",
                )
            else:
'''
new_task_close = '''            if self._task_result_already_verified(repair_id):
                if self.closure_authority is None:
                    raise RuntimeError("verified task recovery requires ClosureAuthority")
                self.closure_authority.close_restored(repair_id)
            else:
'''
ensure_replace("src/control_plane/service.py", old_task_close, new_task_close)

old_rollback = '''    async def _rollback(self, ctx: ToolContext | None, repair_id: str) -> bool:
        reconciliation_required = False
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
                    version = (await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)).strip()
                    result = await self._invoke_personal_operation(
                        repair_id=repair_id,
                        capability="git.rollback",
                        resource_ref=f"repo:{Path(repo).resolve()}",
                        parameters={"repo": repo, "branch": branch},
                        effect_class="write-local",
                        subject_version_refs=[f"git:{version}"],
                        instruction=f"rollback candidate branch {branch}",
                    )
                    if result.status != "succeeded":
                        # A failed or unknown rollback is never evidence that
                        # the candidate branch was removed.  Keep the repair
                        # recoverable instead of promoting the legacy row to
                        # ROLLED_BACK on a refused/blocked mutation.
                        reconciliation_required = True
                        logger.warning("portable candidate rollback failed for %s: %s", row["id"], result.message)
                except ToolError:
                    reconciliation_required = True
                    logger.warning("candidate branch cleanup failed for %s", row["id"])
        resolved_project = self._resolve_project(project)
        if resolved_project in self.config.allowed_auto_projects:
            try:
                project_dir = self.config.project_dirs.get(
                    resolved_project, f"D:\\infrastructure\\compose\\{resolved_project}"
                )
                result = await self._invoke_personal_operation(
                    repair_id=repair_id,
                    capability="docker.restart",
                    resource_ref=f"compose:{resolved_project}",
                    parameters={"project": resolved_project, "project_dir": str(project_dir)},
                    effect_class="write-remote",
                    subject_version_refs=[f"repair:{repair_id}"],
                    instruction=f"restart allowlisted compose project {resolved_project}",
                )
                if result.status != "succeeded":
                    reconciliation_required = True
                    logger.warning("portable rollback restart failed for project %s: %s", project, result.message)
            except (ToolError, RuntimeError):
                reconciliation_required = True
                logger.warning("rollback restart failed for project %s", project)
        return reconciliation_required
'''
new_rollback = '''    async def _rollback(
        self, ctx: ToolContext | None, repair_id: str
    ) -> list[RollbackExecutionReceipt]:
        receipts: list[RollbackExecutionReceipt] = []
        try:
            repair_row = self.store.get_repair(repair_id)
            payload = json.loads(str(repair_row["payload_json"]) if repair_row is not None else "{}")
            project = payload.get("labels", {}).get("project", "")
        except (json.JSONDecodeError, TypeError):
            project = ""
        for row in reversed(self.store.list_actions(repair_id)):
            if row["tool"] != "codex_agent":
                continue
            after = json.loads(row["after_json"]) if row["after_json"] else {}
            repo = row["target"]
            branch = after.get("branch", f"{self.config.candidate_branch_prefix}{repair_id}")
            try:
                version = (
                    await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)
                ).strip()
                result = await self._invoke_personal_operation(
                    repair_id=repair_id,
                    capability="git.rollback",
                    resource_ref=f"repo:{Path(repo).resolve()}",
                    parameters={"repo": repo, "branch": branch},
                    effect_class="write-local",
                    subject_version_refs=[f"git:{version}"],
                    instruction=f"rollback candidate branch {branch}",
                )
                receipts.append(
                    RollbackExecutionReceipt(
                        capability="git.rollback",
                        request_id=result.request_id,
                        provider_id=result.provider_id,
                        status=result.status,
                    )
                )
                if result.status != "succeeded":
                    logger.warning("portable candidate rollback failed for %s: %s", row["id"], result.message)
            except ToolError:
                receipts.append(RollbackExecutionReceipt("git.rollback", "", "", "failed"))
                logger.warning("candidate branch cleanup failed for %s", row["id"])
        resolved_project = self._resolve_project(project)
        if resolved_project in self.config.allowed_auto_projects:
            try:
                project_dir = self.config.project_dirs.get(
                    resolved_project, f"D:\\infrastructure\\compose\\{resolved_project}"
                )
                result = await self._invoke_personal_operation(
                    repair_id=repair_id,
                    capability="docker.restart",
                    resource_ref=f"compose:{resolved_project}",
                    parameters={"project": resolved_project, "project_dir": str(project_dir)},
                    effect_class="write-remote",
                    subject_version_refs=[f"repair:{repair_id}"],
                    instruction=f"restart allowlisted compose project {resolved_project}",
                )
                receipts.append(
                    RollbackExecutionReceipt(
                        capability="docker.restart",
                        request_id=result.request_id,
                        provider_id=result.provider_id,
                        status=result.status,
                    )
                )
                if result.status != "succeeded":
                    logger.warning("portable rollback restart failed for project %s: %s", project, result.message)
            except (ToolError, RuntimeError):
                receipts.append(RollbackExecutionReceipt("docker.restart", "", "", "failed"))
                logger.warning("rollback restart failed for project %s", project)
        return receipts
'''
ensure_replace("src/control_plane/service.py", old_rollback, new_rollback)

# startup projection reconciliation before stale lifecycle interruption.
ensure_replace(
    "src/control_plane/app.py",
    "from .codex_runner import CodexRunner\n",
    "from .codex_runner import CodexRunner\nfrom .closure_authority import ClosureAuthorityError\n",
)
ensure_replace(
    "src/control_plane/app.py",
    '''        now = int(time.time())
        keep_pending = {RepairState.NEEDS_APPROVAL.value, RepairState.RECOVERING.value}
        for row in store.list_repairs_with_fallback(limit=1_000):
            if row["status"] in keep_pending:
                continue''',
    '''        projection_conflicts: set[str] = set()
        if service.closure_authority is not None:
            for row in store.list_repairs_with_fallback(limit=1_000):
                repair_id = str(row["id"])
                try:
                    if service.closure_authority.reconcile_restored_projection(repair_id):
                        logger.info("reconciled canonical terminal projection for %s", repair_id)
                except ClosureAuthorityError:
                    projection_conflicts.add(repair_id)
                    logger.critical(
                        "canonical/legacy closure integrity conflict for %s; preserving row for review",
                        repair_id,
                        exc_info=True,
                    )

        now = int(time.time())
        keep_pending = {RepairState.NEEDS_APPROVAL.value, RepairState.RECOVERING.value}
        for row in store.list_repairs_with_fallback(limit=1_000):
            if str(row["id"]) in projection_conflicts:
                continue
            if row["status"] in keep_pending:
                continue''',
)

# C2 resolution tests: every non-unresolved disposition has an explicit basis.
p = Path("tests/test_repair_resolution.py")
lines = p.read_text(encoding="utf-8").splitlines()
out: list[str] = []
i = 0
while i < len(lines):
    line = lines[i]
    if "store.set_repair_resolution(" not in line:
        out.append(line)
        i += 1
        continue
    block = [line]
    i += 1
    depth = line.count("(") - line.count(")")
    while i < len(lines) and depth > 0:
        block.append(lines[i])
        depth += lines[i].count("(") - lines[i].count(")")
        i += 1
    joined = "\n".join(block)
    if "resolution_kind=" in joined and "ResolutionKind.UNRESOLVED" not in joined and "basis_refs=" not in joined:
        insert_at = len(block) - 1
        indent = " " * 12
        for j, candidate in enumerate(block):
            if "restoration_status=" in candidate:
                indent = candidate[: len(candidate) - len(candidate.lstrip())]
                insert_at = j + 1
                break
        block.insert(insert_at, f'{indent}basis_refs=("basis-1",),')
    out.extend(block)
p.write_text("\n".join(out) + "\n", encoding="utf-8")

# ClosureAuthority tests: explicit Work/Run Decision links and rollback execution evidence.
p = Path("tests/test_closure_authority.py")
text = p.read_text(encoding="utf-8")
if "RollbackExecutionReceipt" not in text.splitlines()[8:20]:
    text = text.replace(
        "from control_plane.closure_authority import ClosureAuthority, ClosureAuthorityError",
        "from control_plane.closure_authority import (\n    ClosureAuthority,\n    ClosureAuthorityError,\n    RollbackExecutionReceipt,\n)",
    )
old = """    portable.save_decision(decision)\n    return work, run, decision\n"""
new = """    portable.save_decision(decision)\n    work_meta = {**work.metadata, \"human_approval_decision_ref\": decision.id, \"human_approval_action\": action}\n    run_meta = {**run.metadata, \"human_approval_decision_ref\": decision.id, \"human_approval_action\": action}\n    work = work.model_copy(update={\"metadata\": work_meta})\n    run = run.model_copy(update={\"metadata\": run_meta})\n    portable.save_work(work)\n    portable.save_run(run)\n    return work, run, decision\n"""
if new not in text:
    if old not in text:
        raise SystemExit("missing _human_pair test anchor")
    text = text.replace(old, new, 1)
text = text.replace(
    "    ClosureAuthority(legacy, portable).record_rolled_back(repair_id)\n",
    "    ClosureAuthority(legacy, portable).record_rolled_back(\n        repair_id,\n        [RollbackExecutionReceipt(\"git.rollback\", \"req-rollback\", \"provider-git\", \"succeeded\")],\n    )\n",
)
if "test_historical_closed_unresolved_is_not_retrospectively_upgraded" not in text:
    text += '''\n\ndef test_historical_closed_unresolved_is_not_retrospectively_upgraded(tmp_path) -> None:\n    repair_id = "historical-closed"\n    legacy = Store(tmp_path / "legacy.db")\n    legacy.create_repair(repair_id, "fp", "{}")\n    legacy.set_repair_status(repair_id, "closed", finished_at=1, result="legacy")\n    portable = InMemoryStateStore()\n    _terminal_pair(portable, repair_id)\n    legacy.attach_portable_store(portable, enable_read=True)\n    authority = ClosureAuthority(legacy, portable)\n    assert authority.reconcile_restored_projection(repair_id) is False\n    row = legacy.get_repair(repair_id)\n    assert row["resolution_kind"] == ResolutionKind.UNRESOLVED.value\n    assert row["restoration_status"] == RestorationStatus.UNVERIFIED.value\n    legacy.close()\n\n\ndef test_partial_restored_projection_reconciles_only_when_lineage_matches(tmp_path) -> None:\n    repair_id = "partial-restored"\n    legacy = Store(tmp_path / "legacy.db")\n    legacy.create_repair(repair_id, "fp", "{}")\n    legacy.set_repair_status(repair_id, "applying")\n    portable = InMemoryStateStore()\n    work, run, proof = _terminal_pair(portable, repair_id)\n    legacy.attach_portable_store(portable, enable_read=True)\n    legacy.set_repair_resolution(\n        repair_id,\n        resolution_kind=ResolutionKind.RESTORED,\n        restoration_status=RestorationStatus.VERIFIED,\n        proof_refs=[proof.id],\n        basis_refs=[work.id, run.id],\n    )\n    authority = ClosureAuthority(legacy, portable)\n    assert authority.reconcile_restored_projection(repair_id) is True\n    assert legacy.get_repair(repair_id)["status"] == "closed"\n    legacy.close()\n\n\ndef test_canonical_success_cannot_be_downgraded_by_legacy_failure(tmp_path) -> None:\n    repair_id = "anti-downgrade"\n    legacy = Store(tmp_path / "legacy.db")\n    legacy.create_repair(repair_id, "fp", "{}")\n    portable = InMemoryStateStore()\n    _terminal_pair(portable, repair_id)\n    legacy.attach_portable_store(portable, enable_read=True)\n    for status in ("failed", "recovering", "interrupted", "rolled_back"):\n        with pytest.raises(ValueError, match="cannot be downgraded"):\n            legacy.set_repair_status(repair_id, status)\n    legacy.close()\n\n\ndef test_rejection_requires_symmetric_explicit_decision_links(tmp_path) -> None:\n    repair_id = "asymmetric-human-link"\n    legacy = Store(tmp_path / "legacy.db")\n    legacy.create_repair(repair_id, "fp", "{}")\n    legacy.set_repair_status(repair_id, "needs_approval")\n    portable = InMemoryStateStore()\n    work, run, decision = _human_pair(portable, repair_id, "reject")\n    portable.save_run(run.model_copy(update={"metadata": {**run.metadata, "human_approval_decision_ref": "other"}}))\n    with pytest.raises(ClosureAuthorityError, match="missing or asymmetric"):\n        ClosureAuthority(legacy, portable).close_rejected(repair_id)\n    legacy.close()\n\n\ndef test_rollback_requires_successful_execution_receipts(tmp_path) -> None:\n    repair_id = "rollback-receipts"\n    legacy = Store(tmp_path / "legacy.db")\n    legacy.create_repair(repair_id, "fp", "{}")\n    legacy.set_repair_status(repair_id, "needs_approval")\n    portable = InMemoryStateStore()\n    _human_pair(portable, repair_id, "rollback")\n    authority = ClosureAuthority(legacy, portable)\n    with pytest.raises(ClosureAuthorityError, match="requires execution receipts"):\n        authority.record_rolled_back(repair_id, [])\n    with pytest.raises(ClosureAuthorityError, match="every execution to succeed"):\n        authority.record_rolled_back(\n            repair_id,\n            [RollbackExecutionReceipt("git.rollback", "req-x", "provider", "unknown")],\n        )\n    assert legacy.get_repair(repair_id)["status"] == "needs_approval"\n    legacy.close()\n'''
p.write_text(text, encoding="utf-8")

# Human approval regression: rollback can materialize exact grants; reject cannot.
p = Path("tests/test_human_approval_provenance.py")
text = p.read_text(encoding="utf-8")
if "test_rollback_decision_materializes_only_explicit_rollback_grants" not in text:
    text += '''\n\n@pytest.mark.asyncio\nasync def test_rollback_decision_materializes_only_explicit_rollback_grants(tmp_path: Path, monkeypatch) -> None:\n    from portable_runtime.core.capabilities import CapabilityResult\n\n    authority = _authority(tmp_path)\n    await authority.prepare_code_edit(\n        repair_id="repair-human-rollback",\n        repo=str(tmp_path),\n        prompt="prepare candidate",\n    )\n    spec = {\n        "capability": "git.rollback",\n        "resource_ref": f"repo:{tmp_path.resolve()}",\n        "subject_version_refs": ["git:abc123"],\n        "effect_class": "write-local",\n    }\n    decision, grants = authority.record_human_approval(\n        "repair-human-rollback",\n        decided_by="alice",\n        action="rollback",\n        operation_specs=[spec],\n    )\n    assert decision.selected_option == "rollback"\n    assert [grant.allowed_capabilities for grant in grants] == [["git.rollback"]]\n\n    async def fake_invoke(request):\n        return CapabilityResult(\n            request_id=request.id,\n            provider_id="fake-provider",\n            status="succeeded",\n        )\n\n    monkeypatch.setattr(authority.runtime.capabilities, "invoke", fake_invoke)\n    result = await authority.invoke_operation(\n        repair_id="repair-human-rollback",\n        capability="git.rollback",\n        resource_ref=f"repo:{tmp_path.resolve()}",\n        parameters={"repo": str(tmp_path), "branch": "candidate"},\n        effect_class="write-local",\n        subject_version_refs=["git:abc123"],\n    )\n    assert result.status == "succeeded"\n\n\ndef test_reject_decision_never_materializes_effect_grants(tmp_path: Path) -> None:\n    authority = _authority(tmp_path)\n    authority.runtime.store.save_work(\n        __import__("portable_runtime.core.models", fromlist=["Work"]).Work(\n            id="work_legacy_repair-human-reject", title="repair"\n        )\n    )\n    authority.runtime.store.save_run(\n        __import__("portable_runtime.core.models", fromlist=["Run"]).Run(\n            id="run_legacy_repair-human-reject",\n            work_id="work_legacy_repair-human-reject",\n        )\n    )\n    decision, grants = authority.record_human_approval(\n        "repair-human-reject",\n        decided_by="alice",\n        action="reject",\n        operation_specs=[\n            {\n                "capability": "git.rollback",\n                "resource_ref": f"repo:{tmp_path.resolve()}",\n                "subject_version_refs": ["git:abc123"],\n                "effect_class": "write-local",\n            }\n        ],\n    )\n    assert decision.selected_option == "reject"\n    assert grants == []\n'''
p.write_text(text, encoding="utf-8")

Path("tests/test_c3_terminal_architecture.py").write_text(r'''from __future__ import annotations

import ast
from pathlib import Path


def test_case_terminal_writes_have_one_product_owner() -> None:
    root = Path("src/control_plane")
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in {"closure_authority.py", "storage.py", "state_machine.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name == "set_repair_resolution":
                violations.append(f"{path}:{node.lineno}: set_repair_resolution")
                continue
            if name not in {"set_repair_status", "_transition"}:
                continue
            values: list[str] = []
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values.append(arg.value.lower())
                elif isinstance(arg, ast.Attribute):
                    values.append(arg.attr.lower())
            for keyword in node.keywords:
                if keyword.arg == "status" and isinstance(keyword.value, ast.Constant):
                    values.append(str(keyword.value.value).lower())
            if any(value in {"closed", "rolled_back"} for value in values):
                violations.append(f"{path}:{node.lineno}: {name} -> {values}")
    assert violations == []


def test_verified_lifecycle_is_not_owned_by_closure_authority() -> None:
    service = Path("src/control_plane/service.py").read_text(encoding="utf-8-sig")
    assert "self._transition(repair_id, RepairState.VERIFIED)" in service
''', encoding="utf-8")
