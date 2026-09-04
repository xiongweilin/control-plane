from portable_runtime.controller import ControllerDecisionKind, ControllerState, ControllerStatus

from control_plane.alert_policy import UnattendedAlertPolicy


async def test_alert_policy_invokes_one_read_only_diagnosis() -> None:
    state = ControllerState(id="controller-test")
    decision = await UnattendedAlertPolicy(prompt="diagnose", repo="C:/repo").select(state)
    assert decision.kind is ControllerDecisionKind.INVOKE_CAPABILITY
    assert decision.capability == "reason.generate"
    assert decision.parameters == {"repo": "C:/repo"}


async def test_alert_policy_closes_after_result() -> None:
    state = ControllerState(id="controller-test", last_result_ref="event-result")
    decision = await UnattendedAlertPolicy(prompt="diagnose").select(state)
    assert decision.kind is ControllerDecisionKind.CLOSE


async def test_alert_policy_requires_explicit_reopen_for_interrupted_state() -> None:
    state = ControllerState(
        id="controller-test",
        status=ControllerStatus.WAITING,
        pending_ref="request-1",
    )
    decision = await UnattendedAlertPolicy(prompt="diagnose").select(state)
    assert decision.kind is ControllerDecisionKind.REOPEN


async def test_alert_policy_ref_is_stable() -> None:
    policy = UnattendedAlertPolicy(prompt="diagnose")
    assert policy.policy_ref == "control-plane/unattended-alert-diagnosis-v1"
