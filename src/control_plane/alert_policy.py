from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from meta_controller import StagedMetaPolicy
from portable_runtime.controller import (
    CognitiveClosure,
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerState,
    ControllerStatus,
    RevisionAssessment,
    RevisionDisposition,
    RevisionScope,
)
from portable_runtime.core.capabilities import CapabilityResult
from portable_runtime.core.models import Event, new_id
from portable_runtime.responsibility import EffectClass

from .kernel_bridge import PersonalKernelBridge

_RESULT_EVENT = "ControllerCapabilityResultObserved"
_DECISION_EVENT = "ControllerDecisionSelected"
_SYNC_ALERT_NAME = "ControlPlaneSynchronizationDegraded"
_LINE_ENDING_DISCARD_CAPABILITY = "git.discard_line_ending_changes"
_SYNC_CAPABILITIES = frozenset(
    {"git.fast_forward", "git.push_exact_ref", "chezmoi.apply"}
)
_SYNC_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SYNC_FIELD_PATTERN = re.compile(r"(?im)^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$")
_AUTONOMOUS_ATTEMPT_LIMIT = 2


@dataclass(frozen=True, slots=True)
class SynchronizationPlan:
    """Structured sync intent selected by diagnosis and guarded by a provider."""

    capability: str
    parameters: dict[str, Any]
    subject_version_refs: tuple[str, ...]


def _sync_plan_from_result(
    result: dict[str, Any] | None,
    *,
    repo: str | None,
    project: str | None,
) -> SynchronizationPlan | None:
    if not result or str(result.get("status", "")) != "succeeded" or not repo or not project:
        return None
    fields = {
        match.group(1).upper(): match.group(2).strip()
        for match in _SYNC_FIELD_PATTERN.finditer(_message(result))
    }
    capability = fields.get("SYNC_CAPABILITY", "").lower()
    if capability not in _SYNC_CAPABILITIES:
        return None

    if capability == "chezmoi.apply":
        source_dir = fields.get("SYNC_SOURCE_DIR", "")
        expected_source_sha = fields.get("SYNC_EXPECTED_SOURCE_SHA", "").lower()
        if not source_dir or not _SYNC_SHA_PATTERN.fullmatch(expected_source_sha):
            return None
        return SynchronizationPlan(
            capability=capability,
            parameters={
                "repo": repo,
                "source_dir": source_dir,
                "project": project,
                "expected_source_sha": expected_source_sha,
            },
            subject_version_refs=(f"chezmoi:{source_dir}:{expected_source_sha}",),
        )

    remote = fields.get("SYNC_REMOTE", "")
    branch = fields.get("SYNC_BRANCH", "")
    expected_old_sha = fields.get("SYNC_EXPECTED_OLD_SHA", "").lower()
    if (
        not remote
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", remote)
        or not branch
        or any(value in branch for value in ("..", " ", "\t"))
        or not _SYNC_SHA_PATTERN.fullmatch(expected_old_sha)
    ):
        return None

    parameters: dict[str, Any] = {
        "repo": repo,
        "project": project,
        "remote": remote,
        "branch": branch,
        "expected_old_sha": expected_old_sha,
    }
    if capability == "git.fast_forward":
        expected_remote_sha = fields.get("SYNC_EXPECTED_REMOTE_SHA", "").lower()
        if not _SYNC_SHA_PATTERN.fullmatch(expected_remote_sha):
            return None
        parameters["expected_remote_sha"] = expected_remote_sha
        version_ref = expected_remote_sha
    else:
        expected_new_sha = fields.get("SYNC_EXPECTED_NEW_SHA", "").lower()
        if not _SYNC_SHA_PATTERN.fullmatch(expected_new_sha):
            return None
        parameters["expected_new_sha"] = expected_new_sha
        version_ref = expected_new_sha
    return SynchronizationPlan(
        capability=capability,
        parameters=parameters,
        subject_version_refs=(f"git:{repo}:{branch}:{version_ref}",),
    )


def _ordered_decisions(
    controller: CognitiveController,
    controller_id: str,
) -> list[tuple[Event, ControllerDecision]]:
    values: list[tuple[Event, ControllerDecision]] = []
    for event in controller.store.list_events(controller_id):
        if event.type != _DECISION_EVENT:
            continue
        raw = event.payload.get("decision")
        if isinstance(raw, dict):
            values.append((event, ControllerDecision.model_validate(raw)))
    return sorted(values, key=lambda item: item[0].created_at)


def _result_by_decision(
    controller: CognitiveController,
    controller_id: str,
) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for event in controller.store.list_events(controller_id):
        if event.type != _RESULT_EVENT:
            continue
        decision_ref = event.payload.get("decision_ref")
        raw = event.payload.get("result")
        if isinstance(decision_ref, str) and isinstance(raw, dict):
            values[decision_ref] = dict(raw)
    return values


def _message(result: dict[str, Any] | None) -> str:
    if not result:
        return ""
    return str(result.get("message", ""))[-12_000:]


def _succeeded(result: dict[str, Any] | None) -> bool:
    return result is not None and str(result.get("status", "")) == "succeeded"


_SAFETY_CLASS_PATTERN = re.compile(
    r"(?im)^\s*SAFETY_CLASS\s*[=:]\s*(REVERSIBLE|IRREVERSIBLE|UNKNOWN)\s*$"
)


def classify_safety(result: dict[str, Any] | None) -> str:
    """Read the current Codex safety judgment from the diagnosis result.

    A missing or malformed judgment is ``unknown``. Unknown is denied all
    effect capabilities, but still receives the bounded read-only execution
    and verification pass before the next round or Feishu escalation.
    """

    match = _SAFETY_CLASS_PATTERN.search(_message(result))
    return match.group(1).lower() if match else "unknown"


def _event_result(event: Event) -> dict[str, Any]:
    raw = event.payload.get("result")
    return dict(raw) if isinstance(raw, dict) else {}


def _run_ref(events: list[Event]) -> str | None:
    for event in reversed(events):
        value = event.payload.get("run_ref")
        if isinstance(value, str) and value:
            return value
    return None


def _latest_run(runtime: Any, work_id: str) -> Any | None:
    runs = runtime.store.list_runs(work_id)
    if not runs:
        return None
    return max(runs, key=lambda run: (run.started_at or run.created_at, run.created_at))


def _recent_reality_context(bridge: PersonalKernelBridge, controller_id: str) -> str:
    """Project prior Work reality into a renewed diagnosis without claiming truth."""

    events = bridge.result_events(controller_id)
    if not events:
        return ""
    parts: list[str] = []
    for event in events[-6:]:
        stage = str(event.payload.get("stage", "observation"))
        result = _event_result(event)
        status = str(result.get("status", "unknown"))
        message = str(result.get("message", ""))[-1500:]
        parts.append(f"[{stage}] status={status}\n{message}")
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            bounded = {
                key: value
                for key, value in metadata.items()
                if key in {"active", "blocker", "attempt_index", "capability"}
            }
            if bounded:
                parts.append(f"[{stage}] metadata={bounded}")
    return "\n\n".join(parts)[-6000:]


@dataclass(frozen=True, slots=True)
class AutonomousRepairPolicy(StagedMetaPolicy):
    """Personal incident policy executed through the Agent Kernel v2 loop.

    Diagnosis is read-class cognition. A successful diagnosis must form a
    CognitiveClosure and hand it to WorkProposal. The profile then admits and
    materializes Work through ResponsibilityKernel, executes effects through the
    Runtime boundary, and returns reality through RevisionAssessment. No direct
    controller-to-effect or direct reopen-to-Work path remains.
    """

    controller: CognitiveController = field(repr=False, compare=False)
    bridge: PersonalKernelBridge = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None
    project: str | None = None
    verification_labels: dict[str, str] = field(default_factory=dict)
    human_instruction: str | None = None
    diagnosis_timeout_seconds: float = 900.0
    execution_timeout_seconds: float = 900.0
    maintenance_capability: str | None = None
    maintenance_parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def policy_ref(self) -> str:
        return "control-plane/autonomous-repair-v2"

    @property
    def attempt_limit(self) -> int:
        """Hard ceiling for alert repair: at most two diagnosis/execution rounds."""

        return _AUTONOMOUS_ATTEMPT_LIMIT

    def _accept_failed_diagnosis_as_unknown(self) -> bool:
        # Preserve the profile's fail-closed behavior: a malformed/failed diagnosis
        # may close only as UNKNOWN/read-only and can never open effect authority.
        return True

    def _session_cutoff(self, controller_id: str) -> datetime | None:
        if not self.human_instruction:
            return None
        event = self.bridge.latest_human_instruction_event(controller_id)
        return event.created_at if event is not None else None

    def _diagnosis_count(self, state: ControllerState) -> int:
        cutoff = self._session_cutoff(state.id)
        count = 0
        for event, decision in _ordered_decisions(self.controller, state.id):
            if cutoff is not None and event.created_at <= cutoff:
                continue
            if (
                decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
                and decision.capability == "reason.generate"
                and decision.parameters.get("phase") == "diagnosis"
            ):
                count += 1
        return count

    def _execution_count(self, state: ControllerState) -> int:
        return sum(
            1
            for event in self.bridge.result_events(state.id)
            if event.payload.get("stage") == "execution"
        )

    def _is_sync_alert(self) -> bool:
        return (
            self.verification_labels.get("alertname") == _SYNC_ALERT_NAME
            and not self._is_line_ending_cleanup()
        )

    def _is_line_ending_cleanup(self) -> bool:
        return self.maintenance_capability == _LINE_ENDING_DISCARD_CAPABILITY

    def _sync_plan(self, diagnosis_result: dict[str, Any] | None) -> SynchronizationPlan | None:
        if not self._is_sync_alert():
            return None
        return _sync_plan_from_result(
            diagnosis_result,
            repo=self.repo,
            project=self.project,
        )

    def _repo_is_dirty(self) -> bool | None:
        if not self.repo:
            return False
        git = shutil.which("git.exe") or shutil.which("git")
        if not git:
            return None
        try:
            completed = subprocess.run(
                [git, "-C", self.repo, "status", "--porcelain=v1", "--untracked-files=all"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return bool((completed.stdout or b"").strip())

    def diagnosis_blocker(self, state: ControllerState) -> str | None:
        """Return a first-diagnosis blocker that must escalate immediately."""

        diagnosis_result = self._latest_diagnosis_result(state)
        safety = classify_safety(diagnosis_result)
        if safety == "irreversible":
            return "irreversible"
        message = _message(diagnosis_result).upper()
        if (
            not self._is_line_ending_cleanup()
            and re.search(r"(?:EXECUTION|REPAIR)_BLOCKER\s*=\s*DIRTY_REPO", message)
        ):
            return "dirty-repository"
        if not self._is_line_ending_cleanup() and self._repo_is_dirty() is True:
            return "dirty-repository"
        return None

    def _diagnosis(self, state: ControllerState, *, retry_context: str = "") -> ControllerDecision:
        attempt = self._diagnosis_count(state) + 1
        if not retry_context and attempt > 1:
            retry_context = _recent_reality_context(self.bridge, state.id)
        instruction = (
            f"Diagnose this incident for autonomous repair attempt {attempt}/"
            f"{self.attempt_limit}.\n"
            "This diagnosis must first judge whether the proposed bounded repair is reversible "
            "in the current reality. On the first line, emit exactly one of: "
            "SAFETY_CLASS=REVERSIBLE, SAFETY_CLASS=IRREVERSIBLE, or SAFETY_CLASS=UNKNOWN. "
            "Use IRREVERSIBLE only when the current action cannot be undone or safely rolled "
            "back; use UNKNOWN when reversibility cannot be established from current evidence. "
            "The safety class is a current judgment, not a historical alert-name label. "
            "After that line, identify the most likely root cause and give one concrete bounded "
            "execution instruction. Do not claim recovery or completion. Prefer the smallest "
            "repair that can be tested.\n\n"
            f"Incident:\n{self.prompt}"
        )
        if self.human_instruction:
            instruction += (
                "\n\nHuman instruction (authoritative task direction, not truth):\n"
                f"{self.human_instruction}"
            )
        if retry_context:
            instruction += f"\n\nPrevious attempt evidence:\n{retry_context}"
        if self._is_line_ending_cleanup():
            instruction += (
                "\n\nThis is an exact allowlisted line-ending-noise cleanup. The provider may act "
                "only on the configured repository, only on unstaged tracked worktree files, "
                "and only when each file is byte-for-byte equal to HEAD after CRLF/CR-to-LF "
                "normalization. Do not use shell commands, do not edit semantic content, and "
                "do not broaden the path or file scope."
            )
        elif self._is_sync_alert():
            instruction += (
                "\n\nFor this synchronization alert, choose exactly one Kernel-qualified action. "
                "After the safety line, emit one SYNC_CAPABILITY line with exactly one of "
                "git.fast_forward, git.push_exact_ref, chezmoi.apply, or NONE. For a Git "
                "action also emit SYNC_REMOTE, SYNC_BRANCH, SYNC_EXPECTED_OLD_SHA and the "
                "required target SHA: use SYNC_EXPECTED_REMOTE_SHA for git.fast_forward or "
                "SYNC_EXPECTED_NEW_SHA for git.push_exact_ref. For chezmoi.apply emit "
                "SYNC_SOURCE_DIR and SYNC_EXPECTED_SOURCE_SHA. SHA values must be full "
                "40-character hex values. Select NONE if the current evidence is not clean, "
                "owner/project binding is unclear, or the operation is not safely bounded. "
                "Do not put shell commands in place of these markers."
            )
        tags = ["failure-localization", "proxy-observation"]
        if attempt > 1:
            tags.append("retry")
        if self._is_line_ending_cleanup():
            tags.append("representation-mismatch")
        hints = self.experience_hints(state, *tags)
        if hints:
            instruction += f"\n\n{hints}"
        parameters: dict[str, Any] = {
            "model": self.diagnosis_model,
            "phase": "diagnosis",
            "attempt_index": attempt,
            "timeout_seconds": self.diagnosis_timeout_seconds,
        }
        if self.repo:
            parameters["repo"] = self.repo
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="reason.generate",
            instruction=instruction,
            parameters=parameters,
            reason="collect a bounded diagnosis before cognitive closure",
        )

    def _latest_diagnosis_result(self, state: ControllerState) -> dict[str, Any] | None:
        diagnosis_decisions = [
            decision
            for _event, decision in _ordered_decisions(self.controller, state.id)
            if (
                decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
                and decision.capability == "reason.generate"
                and decision.parameters.get("phase") == "diagnosis"
            )
        ]
        if not diagnosis_decisions:
            return None
        return _result_by_decision(self.controller, state.id).get(diagnosis_decisions[-1].id)

    def safety_class(self, state: ControllerState) -> str:
        return classify_safety(self._latest_diagnosis_result(state))

    def _effects_allowed(self, diagnosis_result: dict[str, Any] | None) -> bool:
        return classify_safety(diagnosis_result) == "reversible" and (
            not self._is_sync_alert() or self._sync_plan(diagnosis_result) is not None
        )

    def _requested_capabilities(
        self, diagnosis_result: dict[str, Any] | None = None
    ) -> list[str]:
        if not self._effects_allowed(diagnosis_result):
            return ["reason.generate", "monitor.alert.active"]
        if self._is_line_ending_cleanup():
            return [
                "reason.generate",
                _LINE_ENDING_DISCARD_CAPABILITY,
                "monitor.alert.active",
            ]
        sync_plan = self._sync_plan(diagnosis_result)
        if sync_plan is not None:
            return ["reason.generate", sync_plan.capability, "monitor.alert.active"]
        values = ["shell.exec" if self.repo else "reason.generate"]
        if self.project:
            values.append("docker.compose.up")
        if self.maintenance_capability:
            values.append(self.maintenance_capability)
        values.append("monitor.alert.active")
        return values

    def _effect_class(self, diagnosis_result: dict[str, Any] | None = None) -> EffectClass:
        if not self._effects_allowed(diagnosis_result):
            return EffectClass.READ_ONLY
        if self._is_line_ending_cleanup():
            return EffectClass.INTERNAL_REVERSIBLE
        sync_plan = self._sync_plan(diagnosis_result)
        if sync_plan is not None:
            return (
                EffectClass.EXTERNAL_EFFECT
                if sync_plan.capability == "git.push_exact_ref"
                else EffectClass.INTERNAL_REVERSIBLE
            )
        if self.project:
            return EffectClass.EXTERNAL_EFFECT
        if self.repo:
            return EffectClass.INTERNAL_REVERSIBLE
        if self.maintenance_capability:
            return EffectClass.INTERNAL_REVERSIBLE
        return EffectClass.READ_ONLY

    def _form_closure(
        self,
        state: ControllerState,
        diagnosis_result: dict[str, Any] | None,
    ) -> ControllerDecision:
        assessment_ref = self.bridge.assessment_ref(state)
        basis_refs = [assessment_ref]
        if state.last_result_ref:
            basis_refs.append(state.last_result_ref)
        closure = CognitiveClosure(
            controller_ref=state.id,
            controller_state_version=state.version,
            responsibility_ref=state.responsibility_ref,
            subject_ref=state.subject_ref,
            problem_ref=assessment_ref,
            basis_refs=basis_refs,
            selected_direction=(
                "execute the smallest bounded repair selected by the current diagnosis"
            ),
            rationale=_message(diagnosis_result),
            acceptance_criteria=["the triggering alert is no longer active"],
            verification_plan=["query monitor.alert.active after the materialized Work executes"],
            stop_conditions=["stop after two unresolved autonomous attempts"],
            escalation_conditions=["wait for an explicit owner command after the attempt budget"],
            reopen_conditions=[
                "execution fails",
                "project apply fails",
                "monitoring shows the triggering alert remains active",
            ],
            requested_capabilities=self._requested_capabilities(diagnosis_result),
            effect_class=self._effect_class(diagnosis_result),
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.FORM_CLOSURE,
            closure=closure,
            reason="turn the current diagnosis into an explicit bounded cognitive closure",
        )

    def _propose_work(self, state: ControllerState) -> ControllerDecision:
        if not state.active_closure_ref:
            raise ValueError("repair proposal requires an active cognitive closure")
        context = self.bridge.context(state)
        diagnosis_result = self._latest_diagnosis_result(state)
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.PROPOSE_WORK,
            closure_ref=state.active_closure_ref,
            assessment_ref=self.bridge.assessment_ref(state),
            work_kind="personal-incident-repair",
            work_title=context.title,
            work_description=context.description,
            requested_capabilities=self._requested_capabilities(diagnosis_result),
            expected_result="the triggering alert is no longer active",
            stop_conditions=["stop after the bounded autonomous attempt budget"],
            escalation_conditions=["require an explicit owner command before further attempts"],
            effect_class=self._effect_class(diagnosis_result).value,
            reason="handoff the closure to the persistent responsibility kernel as WorkProposal",
        )

    def _current_revision(self, state: ControllerState) -> RevisionAssessment | None:
        if not state.last_revision_ref or not state.active_closure_ref:
            return None
        work = self.bridge.work_for_state(state)
        if work is None:
            return None
        for revision in self.controller.revisions(state.id):
            if (
                revision.id == state.last_revision_ref
                and revision.closure_ref == state.active_closure_ref
                and revision.work_ref == work.id
            ):
                return revision
        return None

    def _revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("repair revision requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("repair revision requires durable Work result observations")

        execution_events = [
            event for event in events if event.payload.get("stage") == "execution"
        ]
        apply_events = [event for event in events if event.payload.get("stage") == "apply"]
        verification_events = [
            event for event in events if event.payload.get("stage") == "verification"
        ]
        execution = execution_events[-1] if execution_events else None
        verification = verification_events[-1] if verification_events else None
        attempts = max(self._diagnosis_count(state), self._execution_count(state))
        blocker = self.diagnosis_blocker(state)
        failed_event = next(
            (
                event
                for event in [execution, *apply_events]
                if event is not None and not _succeeded(_event_result(event))
            ),
            None,
        )

        outcome_refs = [event.id for event in [execution, *apply_events] if event is not None]
        verification_refs = [verification.id] if verification is not None else []
        reason_refs = [event.id for event in events]

        if blocker is not None:
            disposition = RevisionDisposition.WAIT
            scope = RevisionScope.EXECUTION
            reason = (
                "the first diagnosis found an autonomous execution blocker: "
                f"{blocker}; escalate immediately"
            )
            failure_class = "execution-blocked"
        elif execution is None:
            disposition = (
                RevisionDisposition.REOPEN_COGNITION
                if attempts < self.attempt_limit
                else RevisionDisposition.WAIT
            )
            scope = RevisionScope.EXECUTION
            reason = "the repair round produced no durable execution result"
            failure_class = "execution-missing"
        elif failed_event is not None:
            disposition = (
                RevisionDisposition.REOPEN_COGNITION
                if attempts < self.attempt_limit
                else RevisionDisposition.WAIT
            )
            scope = RevisionScope.EXECUTION
            reason = "the materialized repair Work failed before recovery could be verified"
            failure_class = "execution-failure"
        elif verification is None:
            disposition = (
                RevisionDisposition.REOPEN_COGNITION
                if attempts < self.attempt_limit
                else RevisionDisposition.WAIT
            )
            scope = RevisionScope.VERIFICATION
            reason = "the repair produced no durable monitoring verification"
            failure_class = "verification-missing"
        else:
            verify_result = _event_result(verification)
            metadata = verify_result.get("metadata", {})
            active = metadata.get("active") if isinstance(metadata, dict) else None
            if _succeeded(verify_result) and active is False:
                disposition = RevisionDisposition.CLOSE
                scope = RevisionScope.VERIFICATION
                reason = "monitoring confirms the triggering alert is no longer active"
                failure_class = ""
            else:
                disposition = (
                    RevisionDisposition.REOPEN_COGNITION
                    if attempts < self.attempt_limit
                    else RevisionDisposition.WAIT
                )
                scope = RevisionScope.PROBLEM_DEFINITION
                reason = "reality still contradicts the current repair closure"
                failure_class = "alert-still-active"

        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=outcome_refs,
            verification_refs=verification_refs,
            reason_refs=reason_refs,
            failure_class=failure_class,
            revision_scope=scope,
            recommended_disposition=disposition,
            reason=reason,
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="return materialized Work reality to the cognitive controller",
        )

    def _human_reopen_revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("human continuation requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        outcome_refs = [
            event.id for event in events if event.payload.get("stage") != "verification"
        ]
        verification_refs = [
            event.id for event in events if event.payload.get("stage") == "verification"
        ]
        if not outcome_refs and not verification_refs:
            raise ValueError("human continuation has no prior reality observation to revise")
        instruction_event = self.bridge.latest_human_instruction_event(state.id)
        reason_refs = [event.id for event in events]
        if instruction_event is not None:
            reason_refs.append(instruction_event.id)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=outcome_refs,
            verification_refs=verification_refs,
            reason_refs=reason_refs,
            revision_scope=RevisionScope.DECISION,
            recommended_disposition=RevisionDisposition.REOPEN_COGNITION,
            reason="an explicit owner command requests a new cognitive pass over prior reality",
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="explicit owner direction reopens cognition through RevisionAssessment",
        )

    async def execute_work(self, state: ControllerState) -> None:
        work = self.bridge.materialize_work(state)
        runtime = self.bridge.runtime
        run = _latest_run(runtime, work.id)
        if run is None:
            run = runtime.start_run(work.id, workflow_id="personal-incident-repair")

        async def invoke(capability: str, **kwargs: Any) -> CapabilityResult:
            try:
                return await runtime.run_capability(
                    work.id,
                    capability,
                    run_id=run.id,
                    actor_ref=self.bridge.owner_principal,
                    **kwargs,
                )
            except Exception as exc:
                return CapabilityResult(
                    request_id=new_id("request"),
                    provider_id="control-plane",
                    status="failed",
                    message=f"{type(exc).__name__}: {str(exc)[:500]}",
                    metadata={"exception": type(exc).__name__, "capability": capability},
                )

        diagnosis_decisions = [
            decision
            for _event, decision in _ordered_decisions(self.controller, state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and decision.parameters.get("phase") == "diagnosis"
        ]
        if not diagnosis_decisions:
            raise ValueError("materialized repair Work has no diagnosis")
        diagnosis = diagnosis_decisions[-1]
        diagnosis_result = _result_by_decision(self.controller, state.id).get(diagnosis.id)
        safety = classify_safety(diagnosis_result)
        effects_allowed = self._effects_allowed(diagnosis_result)
        blocker = self.diagnosis_blocker(state)
        if blocker is not None:
            blocked = CapabilityResult(
                request_id=new_id("request"),
                provider_id="control-plane",
                status="failed",
                message=f"execution blocked by first diagnosis: {blocker}",
                metadata={"blocker": blocker, "attempt_index": self._execution_count(state) + 1},
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="execution",
                capability="execution.blocked",
                result=blocked,
            )
            return

        execution_attempt = self._execution_count(state) + 1
        if execution_attempt > self.attempt_limit:
            raise RuntimeError("autonomous execution attempt budget is exhausted")

        sync_plan = self._sync_plan(diagnosis_result)
        if safety == "unknown":
            execution_prefix = (
                "The current Codex safety judgment is UNKNOWN. Perform a read-only execution "
                "pass, identify the missing evidence, and do not modify files, call effect "
                "capabilities, access credentials, or claim recovery.\n\n"
            )
        elif self._is_line_ending_cleanup():
            execution_prefix = (
                "Prepare the exact line-ending-noise cleanup for the Kernel provider. Re-read "
                "the diagnosis evidence, do not run Git or shell commands, do not edit files "
                "directly, and let the provider enforce the configured repository and semantic "
                "equality guard. Finish with a concise execution summary.\n\n"
            )
        elif sync_plan is not None:
            execution_prefix = (
                "Prepare the selected synchronization operation for the Kernel provider. Re-read "
                "the diagnosis evidence, do not run git or chezmoi commands, do not modify files, "
                "and do not access remote credentials. The provider will perform the exact bounded "
                "operation and postcondition checks. Finish with a concise execution summary.\n\n"
            )
        else:
            execution_prefix = (
                "Execute the diagnosed reversible repair now. Use the current repository only. "
                "Make the smallest necessary local changes and run relevant checks/tests. Do "
                "not push, merge, access remote credentials, or run Docker directly; "
                "remote/deployment effects belong to Agent Kernel providers. Finish with a "
                "concise execution summary.\n\n"
            )
        instruction = (
            execution_prefix
            + "Diagnosis and plan:\n"
            + _message(diagnosis_result)
        )
        parameters: dict[str, Any] = {
            "model": self.execution_model,
            "phase": "execution",
            "attempt_index": execution_attempt,
            "timeout_seconds": self.execution_timeout_seconds,
        }
        if (
            effects_allowed
            and self.repo
            and sync_plan is None
            and not self._is_line_ending_cleanup()
        ):
            parameters["repo"] = self.repo
        capability = (
            "shell.exec"
            if (
                effects_allowed
                and self.repo
                and sync_plan is None
                and not self._is_line_ending_cleanup()
            )
            else "reason.generate"
        )
        result = await invoke(
            capability,
            instruction=instruction,
            resource_ref=self.repo if parameters.get("repo") else None,
            **parameters,
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="execution",
            capability=capability,
            result=result,
        )

        if result.status == "succeeded" and sync_plan is not None:
            applied = await invoke(
                sync_plan.capability,
                instruction="Apply the diagnosis-selected exact synchronization plan.",
                resource_ref=self.repo,
                subject_version_refs=list(sync_plan.subject_version_refs),
                **sync_plan.parameters,
                phase="apply",
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="apply",
                capability=sync_plan.capability,
                result=applied,
            )

        if (
            result.status == "succeeded"
            and sync_plan is None
            and effects_allowed
            and self.maintenance_capability
        ):
            maintenance_parameters = {
                **self.maintenance_parameters,
                "phase": "apply",
            }
            if self._is_line_ending_cleanup() and self.repo:
                maintenance_parameters["resource_ref"] = self.repo
            applied = await invoke(
                self.maintenance_capability,
                instruction="Apply the already approved bounded maintenance operation.",
                **maintenance_parameters,
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="apply",
                capability=self.maintenance_capability,
                result=applied,
            )

        if (
            result.status == "succeeded"
            and sync_plan is None
            and effects_allowed
            and self.project
        ):
            applied = await invoke(
                "docker.compose.up",
                instruction="Apply the already prepared local repair for the configured project.",
                project=self.project,
                phase="apply",
            )
            self.bridge.record_capability_result(
                controller_id=state.id,
                work_id=work.id,
                run_id=run.id,
                stage="apply",
                capability="docker.compose.up",
                result=applied,
            )

        verified = await invoke(
            "monitor.alert.active",
            instruction="Verify whether the triggering Prometheus alert is still active.",
            labels=dict(self.verification_labels),
            phase="verification",
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="verification",
            capability="monitor.alert.active",
            result=verified,
        )


@dataclass(frozen=True, slots=True)
class ManualTaskPolicy(StagedMetaPolicy):
    """Explicit personal command routed through closure, Work and revision."""

    controller: CognitiveController = field(repr=False, compare=False)
    bridge: PersonalKernelBridge = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None
    human_instruction: str | None = None

    @property
    def policy_ref(self) -> str:
        return "control-plane/manual-task-v2"

    def _diagnosis(self, state: ControllerState) -> ControllerDecision:
        instruction = (
            "Interpret this explicit personal command, identify the concrete bounded work "
            "required, and give one execution instruction. Do not claim completion.\n\n"
            + self.prompt
        )
        if self.human_instruction:
            instruction += "\n\nExplicit owner follow-up:\n" + self.human_instruction
        hints = self.experience_hints(state, "failure-localization")
        if hints:
            instruction += f"\n\n{hints}"
        parameters: dict[str, Any] = {"model": self.diagnosis_model, "phase": "diagnosis"}
        if self.repo:
            parameters["repo"] = self.repo
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="reason.generate",
            instruction=instruction,
            parameters=parameters,
            reason="interpret the explicit personal command before cognitive closure",
        )

    def _execution_capability(self) -> str:
        return "shell.exec" if self.repo else "reason.generate"

    def _effect_class(self) -> EffectClass:
        return EffectClass.INTERNAL_REVERSIBLE if self.repo else EffectClass.READ_ONLY

    def _form_closure(
        self,
        state: ControllerState,
        result: dict[str, Any] | None,
    ) -> ControllerDecision:
        assessment_ref = self.bridge.assessment_ref(state)
        basis_refs = [assessment_ref]
        if state.last_result_ref:
            basis_refs.append(state.last_result_ref)
        closure = CognitiveClosure(
            controller_ref=state.id,
            controller_state_version=state.version,
            responsibility_ref=state.responsibility_ref,
            subject_ref=state.subject_ref,
            problem_ref=assessment_ref,
            basis_refs=basis_refs,
            selected_direction="execute the bounded explicit personal command",
            rationale=_message(result),
            acceptance_criteria=["the explicit personal task execution completes successfully"],
            verification_plan=["review the materialized Work provider result and embedded checks"],
            stop_conditions=["stop on provider failure and wait for owner direction"],
            escalation_conditions=["do not widen repository or effect scope implicitly"],
            reopen_conditions=["execution fails or the owner explicitly requests revision"],
            requested_capabilities=[self._execution_capability()],
            effect_class=self._effect_class(),
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.FORM_CLOSURE,
            closure=closure,
            reason="form an explicit closure before creating personal Work",
        )

    def _propose_work(self, state: ControllerState) -> ControllerDecision:
        if not state.active_closure_ref:
            raise ValueError("manual task proposal requires active closure")
        context = self.bridge.context(state)
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.PROPOSE_WORK,
            closure_ref=state.active_closure_ref,
            assessment_ref=self.bridge.assessment_ref(state),
            work_kind="personal-command",
            work_title=context.title,
            work_description=context.description,
            requested_capabilities=[self._execution_capability()],
            expected_result="the explicit personal task execution completes successfully",
            stop_conditions=["stop on provider failure"],
            escalation_conditions=["wait for explicit owner direction before widening scope"],
            effect_class=self._effect_class().value,
            reason="handoff the personal command closure to WorkProposal",
        )

    def _current_revision(self, state: ControllerState) -> RevisionAssessment | None:
        if not state.last_revision_ref or not state.active_closure_ref:
            return None
        work = self.bridge.work_for_state(state)
        if work is None:
            return None
        return next(
            (
                revision
                for revision in self.controller.revisions(state.id)
                if revision.id == state.last_revision_ref
                and revision.closure_ref == state.active_closure_ref
                and revision.work_ref == work.id
            ),
            None,
        )

    def _revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("manual revision requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("manual revision requires a durable Work result observation")
        result = _event_result(events[-1])
        succeeded = _succeeded(result)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=[event.id for event in events],
            verification_refs=[events[-1].id] if succeeded else [],
            reason_refs=[event.id for event in events],
            failure_class="" if succeeded else "execution-failure",
            revision_scope=RevisionScope.EXECUTION,
            recommended_disposition=(
                RevisionDisposition.CLOSE if succeeded else RevisionDisposition.WAIT
            ),
            reason=(
                "the bounded personal Work completed with its declared embedded checks"
                if succeeded
                else "the personal Work failed and requires explicit owner direction"
            ),
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="return the materialized personal Work result through RevisionAssessment",
        )

    def _human_reopen_revision(self, state: ControllerState) -> ControllerDecision:
        work = self.bridge.work_for_state(state)
        if work is None or not state.active_closure_ref:
            raise ValueError("manual continuation requires materialized Work and active closure")
        events = self.bridge.result_events(state.id, work_id=work.id)
        if not events:
            raise ValueError("manual continuation has no prior Work result")
        instruction_event = self.bridge.latest_human_instruction_event(state.id)
        reason_refs = [event.id for event in events]
        if instruction_event is not None:
            reason_refs.append(instruction_event.id)
        revision = RevisionAssessment(
            controller_ref=state.id,
            controller_state_version=state.version,
            work_ref=work.id,
            closure_ref=state.active_closure_ref,
            run_ref=_run_ref(events),
            outcome_refs=[event.id for event in events],
            verification_refs=[],
            reason_refs=reason_refs,
            revision_scope=RevisionScope.DECISION,
            recommended_disposition=RevisionDisposition.REOPEN_COGNITION,
            reason="explicit owner follow-up requires a new bounded cognitive pass",
            carry_forward_refs=[self.bridge.assessment_ref(state)],
        )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.ASSESS_REVISION,
            revision=revision,
            reason="reopen a waiting manual task through reality-grounded revision",
        )

    async def execute_work(self, state: ControllerState) -> None:
        work = self.bridge.materialize_work(state)
        runtime = self.bridge.runtime
        run = _latest_run(runtime, work.id)
        if run is None:
            run = runtime.start_run(work.id, workflow_id="personal-command")
        diagnoses = [
            decision
            for _event, decision in _ordered_decisions(self.controller, state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and decision.parameters.get("phase") == "diagnosis"
        ]
        if not diagnoses:
            raise ValueError("materialized manual Work has no diagnosis")
        diagnosis = diagnoses[-1]
        result = _result_by_decision(self.controller, state.id).get(diagnosis.id)
        instruction = (
            "Execute this explicit personal task now within the available boundary. Run "
            "relevant checks where applicable and do not claim effects you cannot verify.\n\n"
            + _message(result)
        )
        parameters: dict[str, Any] = {"model": self.execution_model, "phase": "execution"}
        if self.repo:
            parameters["repo"] = self.repo
        capability = self._execution_capability()
        observed = await runtime.run_capability(
            work.id,
            capability,
            instruction=instruction,
            run_id=run.id,
            actor_ref=self.bridge.owner_principal,
            **parameters,
        )
        self.bridge.record_capability_result(
            controller_id=state.id,
            work_id=work.id,
            run_id=run.id,
            stage="execution",
            capability=capability,
            result=observed,
        )


async def drive_policy(
    controller: CognitiveController,
    controller_id: str,
    policy: AutonomousRepairPolicy | ManualTaskPolicy,
    *,
    max_steps: int = 32,
) -> ControllerState:
    """Drive one personal policy through the canonical Agent Kernel v2 loop."""

    state = controller.get(controller_id)
    if state is None:
        raise ValueError(f"unknown controller state: {controller_id}")

    for _ in range(max_steps):
        if state.status is ControllerStatus.CLOSED:
            return state

        if state.status is ControllerStatus.WAITING:
            if not state.work_proposal_ref:
                if policy.human_instruction:
                    state = await controller.step(state.id, policy)
                    continue
                return state

            work = policy.bridge.work_for_state(state)
            current_revision = policy._current_revision(state)
            if current_revision is not None:
                if policy.human_instruction:
                    state = await controller.step(state.id, policy)
                    continue
                return state

            if work is None or not policy.bridge.result_events(state.id, work_id=work.id):
                await policy.execute_work(state)
            state = await controller.step(state.id, policy)
            continue

        state = await controller.step(state.id, policy)

    raise RuntimeError("personal controller policy exceeded its bounded step budget")


UnattendedAlertPolicy = AutonomousRepairPolicy
