from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerState,
    ControllerStatus,
)

_RESULT_EVENT = "ControllerCapabilityResultObserved"


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
    if result is None:
        return False
    return str(result.get("status", "")) == "succeeded"


def _latest_invocation(
    state: ControllerState,
    invocations: list[ControllerDecision],
) -> ControllerDecision:
    if state.last_decision_ref:
        for decision in invocations:
            if decision.id == state.last_decision_ref:
                return decision
    # StateStore event listings are newest-first; the fallback supports
    # policy unit tests that record durable events without advancing state.
    return invocations[0]


@dataclass(frozen=True, slots=True)
class AutonomousRepairPolicy:
    """Two attempts: diagnosis, execution, kernel verification.

    The policy only selects controller actions. Codex invocation, provider
    dispatch, persistence, effect admission and real operations remain inside
    Agent Kernel. After the second unresolved verification it waits for a human
    command instead of continuing autonomously.
    """

    controller: CognitiveController = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None
    project: str | None = None
    verification_labels: dict[str, str] = field(default_factory=dict)
    human_instruction: str | None = None
    max_attempts: int = 2

    @property
    def policy_ref(self) -> str:
        return "control-plane/autonomous-repair-v1"

    def _diagnosis_count(self, state: ControllerState) -> int:
        return sum(
            1
            for decision in self.controller.decisions(state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
            and decision.capability == "reason.generate"
            and decision.parameters.get("model") == self.diagnosis_model
            and decision.parameters.get("phase") == "diagnosis"
        )

    def _diagnosis(
        self,
        state: ControllerState,
        *,
        retry_context: str = "",
    ) -> ControllerDecision:
        attempt = self._diagnosis_count(state) + 1
        instruction = (
            f"Diagnose this incident for autonomous repair attempt {attempt}/"
            f"{self.max_attempts}.\n"
            "Identify the most likely root cause and give a concrete, bounded "
            "execution instruction. Do not claim recovery or completion. Prefer "
            "the smallest repair that can be tested.\n\n"
            f"Incident:\n{self.prompt}"
        )
        if self.human_instruction:
            instruction += (
                "\n\nHuman instruction (authoritative task direction, not truth):\n"
                f"{self.human_instruction}"
            )
        if retry_context:
            instruction += f"\n\nPrevious attempt evidence:\n{retry_context}"
        parameters: dict[str, Any] = {
            "model": self.diagnosis_model,
            "phase": "diagnosis",
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
            reason="use the configured diagnosis model for the current repair attempt",
        )

    def _execute(
        self,
        state: ControllerState,
        diagnosis: str,
    ) -> ControllerDecision:
        instruction = (
            "Execute the diagnosed repair now. Use the current repository only. "
            "Make the smallest necessary local changes and run relevant checks/tests. "
            "Do not push, merge, access remote credentials, or run Docker directly; "
            "those effects belong to Agent Kernel providers. Finish with a concise "
            "execution summary.\n\n"
            f"Diagnosis and plan:\n{diagnosis}"
        )
        parameters: dict[str, Any] = {
            "model": self.execution_model,
            "phase": "execution",
        }
        if self.repo:
            parameters["repo"] = self.repo
            capability = "shell.exec"
        else:
            capability = "reason.generate"
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability=capability,
            instruction=instruction,
            parameters=parameters,
            reason="use the configured execution model after diagnosis",
        )

    def _verify(self, state: ControllerState) -> ControllerDecision:
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.INVOKE_CAPABILITY,
            capability="monitor.alert.active",
            instruction="Verify whether the triggering Prometheus alert is still active.",
            parameters={"labels": dict(self.verification_labels), "phase": "verification"},
            reason="verify the real monitored condition through the kernel provider boundary",
        )

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status in {ControllerStatus.WAITING, ControllerStatus.REOPEN_REQUIRED}:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="explicitly reopen a paused autonomous repair controller",
            )
        if state.status is ControllerStatus.CLOSED:
            raise ValueError("closed repair controller must not restart implicitly")

        invocations = [
            decision
            for decision in self.controller.decisions(state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
        ]
        if not invocations:
            return self._diagnosis(state)

        results = _result_by_decision(self.controller, state.id)
        last = _latest_invocation(state, invocations)
        last_result = results.get(last.id)
        attempts = self._diagnosis_count(state)

        if last_result is None:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.WAIT,
                reason="the previous capability has no durable observed result",
            )

        if last.capability == "reason.generate" and last.parameters.get("phase") == "diagnosis":
            if not _succeeded(last_result):
                if attempts < self.max_attempts:
                    return self._diagnosis(state, retry_context=_message(last_result))
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="two diagnosis attempts failed",
                )
            return self._execute(state, _message(last_result))

        if last.parameters.get("phase") == "execution":
            if not _succeeded(last_result):
                if attempts < self.max_attempts:
                    return self._diagnosis(state, retry_context=_message(last_result))
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="the second execution attempt failed",
                )
            if self.project:
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.INVOKE_CAPABILITY,
                    capability="docker.compose.up",
                    instruction=(
                        "Apply the already prepared local repair for the configured project."
                    ),
                    parameters={"project": self.project, "phase": "apply"},
                    reason=(
                        "apply the allowlisted personal project through the kernel effect "
                        "boundary"
                    ),
                )
            return self._verify(state)

        if last.capability == "docker.compose.up":
            if not _succeeded(last_result):
                if attempts < self.max_attempts:
                    return self._diagnosis(state, retry_context=_message(last_result))
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="the second kernel-managed project apply failed",
                )
            return self._verify(state)

        if last.capability == "monitor.alert.active":
            metadata = last_result.get("metadata", {}) if isinstance(last_result, dict) else {}
            active = metadata.get("active") if isinstance(metadata, dict) else None
            if _succeeded(last_result) and active is False:
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.CLOSE,
                    reason="the triggering alert is no longer active after repair",
                )
            if attempts < self.max_attempts:
                return self._diagnosis(state, retry_context=_message(last_result))
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.WAIT,
                reason="the incident remains unresolved after two autonomous repair attempts",
            )

        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.WAIT,
            reason=f"unexpected autonomous repair stage after {last.capability}",
        )


@dataclass(frozen=True, slots=True)
class ManualTaskPolicy:
    """Model-backed interpretation followed by one execution step."""

    controller: CognitiveController = field(repr=False, compare=False)
    prompt: str
    diagnosis_model: str
    execution_model: str
    repo: str | None = None

    @property
    def policy_ref(self) -> str:
        return "control-plane/manual-task-v1"

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status in {ControllerStatus.WAITING, ControllerStatus.REOPEN_REQUIRED}:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="explicitly resume the manual personal task",
            )
        if state.status is ControllerStatus.CLOSED:
            raise ValueError("closed manual task must not restart implicitly")
        invocations = [
            decision
            for decision in self.controller.decisions(state.id)
            if decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
        ]
        results = _result_by_decision(self.controller, state.id)
        if not invocations:
            params: dict[str, Any] = {
                "model": self.diagnosis_model,
                "phase": "diagnosis",
            }
            if self.repo:
                params["repo"] = self.repo
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.INVOKE_CAPABILITY,
                capability="reason.generate",
                instruction=(
                    "Interpret this explicit personal command, identify the concrete work "
                    "required, and give a bounded execution instruction.\n\n" + self.prompt
                ),
                parameters=params,
                reason="use the configured diagnosis model to interpret the personal task",
            )
        last = _latest_invocation(state, invocations)
        result = results.get(last.id)
        if last.parameters.get("phase") == "diagnosis":
            if not _succeeded(result):
                return ControllerDecision(
                    controller_ref=state.id,
                    state_version=state.version,
                    kind=ControllerDecisionKind.WAIT,
                    reason="manual task diagnosis failed and requires user attention",
                )
            params = {"model": self.execution_model, "phase": "execution"}
            if self.repo:
                params["repo"] = self.repo
                capability = "shell.exec"
            else:
                capability = "reason.generate"
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.INVOKE_CAPABILITY,
                capability=capability,
                instruction=(
                    "Execute this explicit personal task now within the available boundary. "
                    "Do not claim effects you cannot verify.\n\n" + _message(result)
                ),
                parameters=params,
                reason="use the configured execution model after task interpretation",
            )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.CLOSE,
            reason="the explicit personal task received its execution result",
        )


async def drive_policy(
    controller: CognitiveController,
    controller_id: str,
    policy: AutonomousRepairPolicy | ManualTaskPolicy,
    *,
    max_steps: int = 16,
) -> ControllerState:
    """Drive one profile policy until explicit close or human wait."""

    state = controller.get(controller_id)
    if state is None:
        raise ValueError(f"unknown controller state: {controller_id}")
    for _ in range(max_steps):
        if state.status in {ControllerStatus.CLOSED, ControllerStatus.WAITING}:
            return state
        decision = await policy.select(state)
        state = await controller.apply(decision)
    raise RuntimeError("personal controller policy exceeded its bounded step budget")


# Compatibility import for callers/tests from the first unattended-only profile.
UnattendedAlertPolicy = AutonomousRepairPolicy
