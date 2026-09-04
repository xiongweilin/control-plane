from __future__ import annotations

from dataclasses import dataclass

from portable_runtime.controller import (
    CognitiveController,
    ControllerDecision,
    ControllerDecisionKind,
    ControllerState,
    ControllerStatus,
)


@dataclass(frozen=True, slots=True)
class UnattendedAlertPolicy:
    """Diagnose one unattended alert through Agent Kernel, then close.

    The profile chooses only the next controller action. Capability execution,
    Work semantics, authorization, provider dispatch, persistence and effects
    remain owned by Agent Kernel.
    """

    prompt: str
    repo: str | None = None

    @property
    def policy_ref(self) -> str:
        return "control-plane/unattended-alert-diagnosis-v1"

    async def select(self, state: ControllerState) -> ControllerDecision:
        if state.status in {ControllerStatus.WAITING, ControllerStatus.REOPEN_REQUIRED}:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.REOPEN,
                reason="resume the unattended read-only alert diagnosis loop",
            )
        if state.status is ControllerStatus.CLOSED:
            raise ValueError("closed unattended alert diagnosis must not be restarted implicitly")
        if state.last_result_ref is None:
            return ControllerDecision(
                controller_ref=state.id,
                state_version=state.version,
                kind=ControllerDecisionKind.INVOKE_CAPABILITY,
                capability="reason.generate",
                instruction=self.prompt,
                parameters={"repo": self.repo} if self.repo else {},
                reason="diagnose one current unattended alert through the kernel capability boundary",
            )
        return ControllerDecision(
            controller_ref=state.id,
            state_version=state.version,
            kind=ControllerDecisionKind.CLOSE,
            reason="one read-only diagnosis result has been observed; stop before any repair effect",
        )


async def drive_alert_diagnosis(
    controller: CognitiveController,
    controller_id: str,
    policy: UnattendedAlertPolicy,
    *,
    max_steps: int = 4,
) -> ControllerState:
    """Run the small policy loop to explicit cognitive closure."""

    state = controller.get(controller_id)
    if state is None:
        raise ValueError(f"unknown controller state: {controller_id}")
    for _ in range(max_steps):
        if state.status is ControllerStatus.CLOSED:
            return state
        state = await controller.step(controller_id, policy)
    raise RuntimeError("unattended alert diagnosis did not reach closure within the bounded policy loop")
