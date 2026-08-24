from __future__ import annotations

from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def ensure_replace(path: str, old: str, new: str, count: int = 1) -> None:
    text = read(path)
    if new in text:
        return
    if old not in text:
        raise SystemExit(f"missing patch anchor in {path}: {old[:160]!r}")
    write(path, text.replace(old, new, count))


def replace_region(path: str, start_marker: str, end_marker: str, replacement: str) -> None:
    text = read(path)
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    if text[start:end] == replacement:
        return
    write(path, text[:start] + replacement + text[end:])


# --- storage: disposition basis lineage + canonical-success anti-downgrade ---
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
replace_region(
    "src/control_plane/storage.py",
    "    def set_repair_resolution(\n",
    "    def set_repair_status(",
    '''    def set_repair_resolution(
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

''',
)
ensure_replace(
    "src/control_plane/storage.py",
    "        if work is None and run is None:\n            return True\n        work_status = {",
    "        if work is None and run is None:\n            return True\n        canonical_success = (\n            work is not None\n            and run is not None\n            and getattr(work, \"status\", \"\") == \"completed\"\n            and getattr(run, \"status\", \"\") == \"succeeded\"\n        )\n        if canonical_success:\n            if status not in {\"verified\", \"closed\"}:\n                raise ValueError(\n                    \"canonical successful terminal pair cannot be downgraded by legacy lifecycle\"\n                )\n            return True\n        work_status = {",
)

# --- portable approval compatibility: rollback has exact explicit grants; reject none ---
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

# --- service: one product closure owner ---
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

replace_region(
    "src/control_plane/service.py",
    "    async def _apply_approval_decision(\n",
    "    async def _approval_operation_specs(",
    '''    async def _apply_approval_decision(self, repair_id: str, decision: str | None) -> None:
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
            await self._record_canonical_human_approval(
                repair_id,
                decided_by=decided_by,
                principal_ref=self.config.owner_principal,
                principal_source="control-plane-api-key",
                action="approve",
                note=note,
            )
            await self._apply_code_candidates(None, repair_id)
        elif decision == "rollback":
            await self._record_canonical_human_approval(
                repair_id,
                decided_by=decided_by,
                principal_ref=self.config.owner_principal,
                principal_source="control-plane-api-key",
                action="rollback",
                note=note,
            )
            receipts = await self._rollback(None, repair_id)
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

''',
)

replace_region(
    "src/control_plane/service.py",
    "    async def _complete_repair(\n",
    "    def _tool_context(",
    '''    async def _complete_repair(
        self,
        repair_id: str,
        fingerprint: str,
        alert: Alert,
        proposal: dict[str, Any],
    ) -> None:
        """Verify, establish authoritative closure, then run ancillary learning."""
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
            raise RuntimeError(f"Verification failed:\\n{report.summary}")
        if self.portable_authority is None:
            raise RuntimeError("successful repair requires portable completion authority")
        verification_refs = self.portable_authority.record_verification(
            repair_id,
            report=report,
            summary=report.summary,
            evidence_refs=[check.evidence_ref for check in report.checks if check.evidence_ref],
        )
        self.portable_authority.finalize_repair(
            repair_id,
            verified=True,
            verification_refs=verification_refs,
            summary=report.summary,
        )
        self._transition(repair_id, RepairState.VERIFIED)
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

''',
)

# Task crash-recovery: canonical task terminal fact must be projected by ClosureAuthority.
service = read("src/control_plane/service.py")
start = service.index('        if payload.get("kind") == "task":')
end = service.index("        try:\n            # Alert payloads", start)
replacement = '''        if payload.get("kind") == "task":
            if self._task_result_already_verified(repair_id):
                if self.closure_authority is None:
                    raise RuntimeError("verified task recovery requires ClosureAuthority")
                self.closure_authority.close_restored(repair_id)
            else:
                self._keep_resumed_repair_recovering(
                    repair_id,
                    "effect applied but task-result verification is unavailable",
                )
            return

'''
write("src/control_plane/service.py", service[:start] + replacement + service[end:])

replace_region(
    "src/control_plane/service.py",
    "    async def _rollback(",
    "    async def _create_candidate(",
    '''    async def _rollback(
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

''',
)

# --- startup: restored projection before stale-interrupted projection ---
ensure_replace(
    "src/control_plane/app.py",
    "from .codex_runner import CodexRunner\n",
    "from .codex_runner import CodexRunner\nfrom .closure_authority import ClosureAuthorityError\n",
)
app = read("src/control_plane/app.py")
old_start = '''        now = int(time.time())
        keep_pending = {RepairState.NEEDS_APPROVAL.value, RepairState.RECOVERING.value}
        for row in store.list_repairs_with_fallback(limit=1_000):
            if row["status"] in keep_pending:
                continue
'''
new_start = '''        projection_conflicts: set[str] = set()
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
                continue
'''
if new_start not in app:
    if old_start not in app:
        raise SystemExit("missing startup stale-reconciliation anchor")
    write("src/control_plane/app.py", app.replace(old_start, new_start, 1))

# --- tests: update C2 callers to explicit disposition basis ---
p = Path("tests/test_repair_resolution.py")
lines = p.read_text(encoding="utf-8").splitlines()
out: list[str] = []
i = 0
while i < len(lines):
    if "store.set_repair_resolution(" not in lines[i]:
        out.append(lines[i])
        i += 1
        continue
    block = [lines[i]]
    i += 1
    depth = block[0].count("(") - block[0].count(")")
    while i < len(lines) and depth > 0:
        block.append(lines[i])
        depth += lines[i].count("(") - lines[i].count(")")
        i += 1
    joined = "\n".join(block)
    if "ResolutionKind.UNRESOLVED" not in joined and "basis_refs=" not in joined:
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

# ClosureAuthority helpers/tests need explicit human links and rollback execution receipts.
p = Path("tests/test_closure_authority.py")
text = p.read_text(encoding="utf-8")
text = text.replace(
    "from control_plane.closure_authority import ClosureAuthority, ClosureAuthorityError",
    "from control_plane.closure_authority import (\n    ClosureAuthority,\n    ClosureAuthorityError,\n    RollbackExecutionReceipt,\n)",
)
old = """    portable.save_decision(decision)\n    return work, run, decision\n"""
new = """    portable.save_decision(decision)\n    work = work.model_copy(\n        update={\n            \"metadata\": {\n                **work.metadata,\n                \"human_approval_decision_ref\": decision.id,\n                \"human_approval_action\": action,\n            }\n        }\n    )\n    run = run.model_copy(\n        update={\n            \"metadata\": {\n                **run.metadata,\n                \"human_approval_decision_ref\": decision.id,\n                \"human_approval_action\": action,\n            }\n        }\n    )\n    portable.save_work(work)\n    portable.save_run(run)\n    return work, run, decision\n"""
if new not in text:
    if old not in text:
        raise SystemExit("missing human pair test anchor")
    text = text.replace(old, new, 1)
text = text.replace(
    "    ClosureAuthority(legacy, portable).record_rolled_back(repair_id)\n",
    "    ClosureAuthority(legacy, portable).record_rolled_back(\n        repair_id,\n        [RollbackExecutionReceipt(\"git.rollback\", \"req-rollback\", \"provider-git\", \"succeeded\")],\n    )\n",
)
if "test_rollback_requires_successful_execution_receipts" not in text:
    text += '''

def test_historical_closed_unresolved_is_not_retrospectively_upgraded(tmp_path) -> None:
    repair_id = "historical-closed"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "closed", finished_at=1, result="legacy")
    portable = InMemoryStateStore()
    _terminal_pair(portable, repair_id)
    legacy.attach_portable_store(portable, enable_read=True)
    authority = ClosureAuthority(legacy, portable)
    assert authority.reconcile_restored_projection(repair_id) is False
    assert legacy.get_repair(repair_id)["resolution_kind"] == ResolutionKind.UNRESOLVED.value
    legacy.close()


def test_partial_restored_projection_reconciles_when_lineage_matches(tmp_path) -> None:
    repair_id = "partial-restored"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "applying")
    portable = InMemoryStateStore()
    work, run, proof = _terminal_pair(portable, repair_id)
    legacy.attach_portable_store(portable, enable_read=True)
    legacy.set_repair_resolution(
        repair_id,
        resolution_kind=ResolutionKind.RESTORED,
        restoration_status=RestorationStatus.VERIFIED,
        proof_refs=[proof.id],
        basis_refs=[work.id, run.id],
    )
    authority = ClosureAuthority(legacy, portable)
    assert authority.reconcile_restored_projection(repair_id) is True
    assert legacy.get_repair(repair_id)["status"] == "closed"
    legacy.close()


def test_canonical_success_cannot_be_downgraded_by_legacy_failure(tmp_path) -> None:
    repair_id = "anti-downgrade"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    portable = InMemoryStateStore()
    _terminal_pair(portable, repair_id)
    legacy.attach_portable_store(portable, enable_read=True)
    for status in ("failed", "recovering", "interrupted", "rolled_back"):
        with pytest.raises(ValueError, match="cannot be downgraded"):
            legacy.set_repair_status(repair_id, status)
    legacy.close()


def test_rejection_requires_symmetric_explicit_decision_links(tmp_path) -> None:
    repair_id = "asymmetric-human-link"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "needs_approval")
    portable = InMemoryStateStore()
    _, run, _ = _human_pair(portable, repair_id, "reject")
    portable.save_run(
        run.model_copy(
            update={"metadata": {**run.metadata, "human_approval_decision_ref": "other"}}
        )
    )
    with pytest.raises(ClosureAuthorityError, match="missing or asymmetric"):
        ClosureAuthority(legacy, portable).close_rejected(repair_id)
    legacy.close()


def test_rollback_requires_successful_execution_receipts(tmp_path) -> None:
    repair_id = "rollback-receipts"
    legacy = Store(tmp_path / "legacy.db")
    legacy.create_repair(repair_id, "fp", "{}")
    legacy.set_repair_status(repair_id, "needs_approval")
    portable = InMemoryStateStore()
    _human_pair(portable, repair_id, "rollback")
    authority = ClosureAuthority(legacy, portable)
    with pytest.raises(ClosureAuthorityError, match="requires execution receipts"):
        authority.record_rolled_back(repair_id, [])
    with pytest.raises(ClosureAuthorityError, match="every execution to succeed"):
        authority.record_rolled_back(
            repair_id,
            [RollbackExecutionReceipt("git.rollback", "req-x", "provider", "unknown")],
        )
    assert legacy.get_repair(repair_id)["status"] == "needs_approval"
    legacy.close()
'''
p.write_text(text, encoding="utf-8")

# Human approval regression: rollback exact grants; reject no effect grant.
p = Path("tests/test_human_approval_provenance.py")
text = p.read_text(encoding="utf-8")
if "test_rollback_decision_materializes_explicit_rollback_grant" not in text:
    text += '''

@pytest.mark.asyncio
async def test_rollback_decision_materializes_explicit_rollback_grant(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-rollback",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    decision, grants = authority.record_human_approval(
        "repair-human-rollback",
        decided_by="alice",
        action="rollback",
        operation_specs=[
            {
                "capability": "git.rollback",
                "resource_ref": f"repo:{tmp_path.resolve()}",
                "subject_version_refs": ["git:abc123"],
                "effect_class": "write-local",
            }
        ],
    )
    assert decision.selected_option == "rollback"
    assert [grant.allowed_capabilities for grant in grants] == [["git.rollback"]]
    assert all(grant.source_decision_ref == decision.id for grant in grants)


@pytest.mark.asyncio
async def test_reject_decision_materializes_no_effect_grant(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    await authority.prepare_code_edit(
        repair_id="repair-human-reject",
        repo=str(tmp_path),
        prompt="prepare candidate",
    )
    decision, grants = authority.record_human_approval(
        "repair-human-reject",
        decided_by="alice",
        action="reject",
        operation_specs=[
            {
                "capability": "git.rollback",
                "resource_ref": f"repo:{tmp_path.resolve()}",
                "subject_version_refs": ["git:abc123"],
                "effect_class": "write-local",
            }
        ],
    )
    assert decision.selected_option == "reject"
    assert grants == []
'''
p.write_text(text, encoding="utf-8")

# AST architecture guard for terminal disposition ownership.
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
