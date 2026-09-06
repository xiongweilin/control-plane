from __future__ import annotations

from meta_controller import StagedMetaPolicy
from portable_runtime.controller import CognitiveController, ControllerDecisionKind
from portable_runtime.core.runtime import Runtime

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy
from control_plane.kernel_bridge import PersonalKernelBridge


def test_control_plane_policies_use_meta_controller_directly() -> None:
    assert issubclass(AutonomousRepairPolicy, StagedMetaPolicy)
    assert issubclass(ManualTaskPolicy, StagedMetaPolicy)
    assert AutonomousRepairPolicy.select is StagedMetaPolicy.select
    assert ManualTaskPolicy.select is StagedMetaPolicy.select


def test_autonomous_repair_diagnosis_consumes_epistemic_control_frame() -> None:
    runtime = Runtime(runtime_id="personal-platform")
    controller = CognitiveController(runtime)
    bridge = PersonalKernelBridge(runtime, controller, owner_principal="principal:test")
    state, _assessment_ref = bridge.begin(
        title="Repair alert",
        description="repair the current incident",
        kind="personal-incident-repair",
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "Broken"},
    )
    policy = AutonomousRepairPolicy(
        controller=controller,
        bridge=bridge,
        prompt="repair the current incident",
        diagnosis_model="codex/gpt-5.6-luna",
        execution_model="codex/gpt-5.6-luna",
        repo="C:/repo",
        project="test",
        verification_labels={"alertname": "Broken"},
    )

    diagnosis = policy._diagnosis(state)

    assert diagnosis.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    assert diagnosis.capability == "reason.generate"
    assert diagnosis.parameters["phase"] == "diagnosis"
    assert diagnosis.parameters["meta_intent"] == "acquire-evidence"
    assert diagnosis.parameters["meta_candidate_ref"]
    assert diagnosis.instruction is not None
    assert "META_INTENT=ACQUIRE_EVIDENCE" in diagnosis.instruction
    assert "does not establish target recovery" in diagnosis.instruction
