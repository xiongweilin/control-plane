from __future__ import annotations

from typing import Any

from meta_controller import StagedMetaPolicy
from portable_runtime.controller import ControllerDecision, ControllerState

from .alert_policy import AutonomousRepairPolicy, ManualTaskPolicy, _event_result


def _recent_reality_context(policy: AutonomousRepairPolicy, state: ControllerState) -> str:
    """Project the previous bounded round into the next diagnosis.

    This is evidence context, not a success claim. The new diagnosis must still
    judge the current reality and Agent Kernel retains the canonical records.
    """

    events = policy.bridge.result_events(state.id)
    if not events:
        return ""
    parts: list[str] = []
    for event in events[-6:]:
        stage = str(event.payload.get("stage", "observation"))
        result = _event_result(event)
        status = str(result.get("status", "unknown"))
        message = str(result.get("message", ""))[-1500:]
        metadata = result.get("metadata")
        parts.append(f"[{stage}] status={status}\n{message}")
        if isinstance(metadata, dict) and metadata:
            safe_metadata = {
                key: value
                for key, value in metadata.items()
                if key in {"active", "blocker", "attempt_index", "capability"}
            }
            if safe_metadata:
                parts.append(f"[{stage}] metadata={safe_metadata}")
    return "\n\n".join(parts)[-6000:]


class MetaAutonomousRepairPolicy(AutonomousRepairPolicy, StagedMetaPolicy):
    """Personal repair specialization using reusable meta-controller selection."""

    select = StagedMetaPolicy.select

    def _accept_failed_diagnosis_as_unknown(self) -> bool:
        # Preserve the existing fail-closed behavior: malformed/failed diagnosis
        # may form only a read-class UNKNOWN closure, never an effect permission.
        return True

    def _diagnosis(self, state: ControllerState, *, retry_context: str = "") -> ControllerDecision:
        if not retry_context and self._diagnosis_count(state) > 0:
            retry_context = _recent_reality_context(self, state)
        decision = super()._diagnosis(state, retry_context=retry_context)

        tags: list[str] = ["failure-localization", "proxy-observation"]
        if self._diagnosis_count(state) > 0:
            tags.append("retry")
        if self._is_line_ending_cleanup():
            tags.append("representation-mismatch")
        hints = self.experience_hints(state, *tags)
        if not hints:
            return decision
        return decision.model_copy(
            update={"instruction": f"{decision.instruction}\n\n{hints}"}
        )


class MetaManualTaskPolicy(ManualTaskPolicy, StagedMetaPolicy):
    """Explicit personal task specialization using reusable stage selection."""

    select = StagedMetaPolicy.select

    def _diagnosis(self, state: ControllerState) -> ControllerDecision:
        decision = super()._diagnosis(state)
        hints = self.experience_hints(state, "failure-localization")
        if not hints:
            return decision
        return decision.model_copy(
            update={"instruction": f"{decision.instruction}\n\n{hints}"}
        )


__all__ = ["MetaAutonomousRepairPolicy", "MetaManualTaskPolicy"]
