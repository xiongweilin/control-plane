from __future__ import annotations

from control_plane.errors import (
    ErrorClass,
    TimeoutKind,
    classify_exec_error,
    classify_verify_error,
)


def test_timeout_kind_values() -> None:
    assert TimeoutKind.EXEC.value == "exec"
    assert TimeoutKind.COMM.value == "comm"
    assert TimeoutKind.VERIFY.value == "verify"
    assert TimeoutKind.APPROVAL.value == "approval"


def test_classify_exec_network_retryable() -> None:
    assert classify_exec_error("ssh: connect to host github.com port 22: Connection timed out") == ErrorClass.RETRYABLE
    assert classify_exec_error("Connection refused") == ErrorClass.RETRYABLE
    assert classify_exec_error("could not resolve host prometheus") == ErrorClass.RETRYABLE


def test_classify_exec_git_conflict_retryable() -> None:
    assert classify_exec_error("merge conflict in main") == ErrorClass.RETRYABLE
    assert classify_exec_error("non-fast-forward; failed to push") == ErrorClass.RETRYABLE


def test_classify_exec_deterministic() -> None:
    assert classify_exec_error("workspace dirty; refusing to run agent") == ErrorClass.DETERMINISTIC
    assert classify_exec_error("ConfigurationError: invalid configuration") == ErrorClass.DETERMINISTIC


def test_classify_exec_unknown() -> None:
    assert classify_exec_error("agent produced no result") == ErrorClass.UNKNOWN


def test_classify_verify() -> None:
    assert classify_verify_error("probe failed: connection refused") == ErrorClass.RETRYABLE
    assert classify_verify_error("value 0 != expected 1") == ErrorClass.DETERMINISTIC
    assert classify_verify_error("no result for query") == ErrorClass.DETERMINISTIC
    assert classify_verify_error("unexpected output") == ErrorClass.UNKNOWN
