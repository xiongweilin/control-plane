from types import SimpleNamespace

from control_plane.app import (
    _diagnosis_status,
    _fallback_policy_count,
    _policy_count,
)


def _event(type_, payload):
    return SimpleNamespace(type=type_, payload=payload)


def _diagnosis_decision_event(phase="diagnosis"):
    return _event(
        "ControllerDecisionSelected",
        {
            "decision": {
                "capability": "reason.generate",
                "parameters": {"phase": phase},
            }
        },
    )


def _policy_with_events(events, *, raises=False):
    def list_events(_state_id):
        if raises:
            raise RuntimeError("store unavailable")
        return list(events)

    store = SimpleNamespace(list_events=list_events)
    return SimpleNamespace(controller=SimpleNamespace(store=store))


def test_fallback_count_without_state_is_zero() -> None:
    assert _fallback_policy_count(object(), None, "diagnosis") == 0


def test_fallback_count_without_event_store_is_zero() -> None:
    assert _fallback_policy_count(SimpleNamespace(), SimpleNamespace(id="s"), "diagnosis") == 0


def test_fallback_count_store_failure_is_zero() -> None:
    policy = _policy_with_events([], raises=True)
    assert _fallback_policy_count(policy, SimpleNamespace(id="s"), "diagnosis") == 0


def test_fallback_count_diagnosis_counts_matching_decisions() -> None:
    policy = _policy_with_events(
        [
            _diagnosis_decision_event(),
            _event("Other", {}),
            _event("ControllerDecisionSelected", {"decision": "not-a-dict"}),
            _diagnosis_decision_event(phase="execution"),
            _diagnosis_decision_event(),
        ]
    )
    assert _fallback_policy_count(policy, SimpleNamespace(id="s"), "diagnosis") == 2


def test_fallback_count_execution_uses_bridge_result_events() -> None:
    events = [
        _event("X", {"stage": "execution"}),
        _event("X", {"stage": "diagnosis"}),
        _event("X", {}),
    ]
    policy = _policy_with_events([])
    policy.bridge = SimpleNamespace(result_events=lambda _sid: events)
    assert _fallback_policy_count(policy, SimpleNamespace(id="s"), "execution") == 1


def test_fallback_count_bridge_failure_is_zero() -> None:
    def boom(_sid):
        raise RuntimeError("bridge down")

    policy = _policy_with_events([])
    policy.bridge = SimpleNamespace(result_events=boom)
    assert _fallback_policy_count(policy, SimpleNamespace(id="s"), "execution") == 0


def test_policy_count_prefers_direct_method() -> None:
    policy = SimpleNamespace(_diagnosis_count=lambda _state: 3)
    assert _policy_count(policy, SimpleNamespace(id="s"), "_diagnosis_count", "diagnosis") == 3


def test_policy_count_clamps_negative_direct_result() -> None:
    policy = SimpleNamespace(_diagnosis_count=lambda _state: -2)
    assert _policy_count(policy, SimpleNamespace(id="s"), "_diagnosis_count", "diagnosis") == 0


def test_policy_count_falls_back_when_method_raises() -> None:
    def boom(_state):
        raise RuntimeError("nope")

    policy = SimpleNamespace(_diagnosis_count=boom)
    assert _policy_count(policy, SimpleNamespace(id="s"), "_diagnosis_count", "diagnosis") == 0


def test_diagnosis_status_timeout_without_result() -> None:
    assert _diagnosis_status(None, error=TimeoutError("timed out")) == "timeout"
    assert _diagnosis_status(None, error=ValueError("bad")) == "no_valid_diagnosis"
    assert _diagnosis_status(None) == "no_valid_diagnosis"


def test_diagnosis_status_valid_requires_single_safety_class() -> None:
    ok = {"status": "succeeded", "message": "done\nSAFETY_CLASS=REVERSIBLE\n"}
    assert _diagnosis_status(ok) == "valid"
    assert _diagnosis_status({"status": "succeeded", "message": "done"}) == "malformed"


def test_diagnosis_status_maps_failure_modes() -> None:
    assert _diagnosis_status({"status": "timeout"}) == "timeout"
    assert _diagnosis_status({"status": "failed", "message": "operation timeout"}) == "timeout"
    assert _diagnosis_status({"status": "malformed"}) == "malformed"
    assert _diagnosis_status({"status": "failed", "message": "boom"}) == "provider_failed"
    assert _diagnosis_status({"status": "failed", "error": {"code": 1}}) == "provider_failed"
