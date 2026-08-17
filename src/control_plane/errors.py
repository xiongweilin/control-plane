"""Error classification for retryable vs deterministic failures (batch2 item 10)."""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    """Outcome classification driving retry decisions.

    RETRYABLE: transient infrastructure problems (network, git conflict) that
    may succeed on a later attempt.
    DETERMINISTIC: a stable defect (validation, configuration) that will fail
    identically on retry; auto-retry is suppressed.
    UNKNOWN: not classified; keep legacy attempt-based behaviour.
    """

    RETRYABLE = "retryable"
    DETERMINISTIC = "deterministic"
    UNKNOWN = "unknown"


class TimeoutKind(StrEnum):
    """Timeout taxonomy (batch2 item 3): where a timeout occurred."""

    EXEC = "exec"  # agent run (codex session)
    COMM = "comm"  # alertmanager / feishu / git / ssh network communication
    VERIFY = "verify"  # deterministic verifier
    APPROVAL = "approval"  # waiting for human decision


NETWORK_HINTS = (
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "connection closed",
    "connection timed out",
    "could not resolve host",
    "network is unreachable",
    "no route to host",
    "ssh: connect",
    "getaddrinfo",
    "name or service not known",
    "remote end hung up",
    "broken pipe",
)

GIT_CONFLICT_HINTS = (
    "conflict",
    "merge failed",
    "non-fast-forward",
    "failed to push",
    "cannot lock ref",
    "reference already exists",
    "fetch first",
)

DETERMINISTIC_HINTS = (
    "validationerror",
    "configerror",
    "configurationerror",
    "invalid configuration",
    "workspace dirty",
    "refusing to run",
    "deterministic",
)


def classify_exec_error(message: str) -> ErrorClass:
    """Classify an agent/execution failure message (runner + command layer)."""
    lowered = message.lower()
    if any(hint in lowered for hint in NETWORK_HINTS):
        return ErrorClass.RETRYABLE
    if any(hint in lowered for hint in GIT_CONFLICT_HINTS):
        return ErrorClass.RETRYABLE
    if any(hint in lowered for hint in DETERMINISTIC_HINTS):
        return ErrorClass.DETERMINISTIC
    return ErrorClass.UNKNOWN


def classify_verify_error(message: str) -> ErrorClass:
    """Classify a verifier failure.

    Infrastructure failures (probe/promql/container status unreachable) are
    retryable; evidence mismatches are deterministic.
    """
    lowered = message.lower()
    if any(hint in lowered for hint in NETWORK_HINTS):
        return ErrorClass.RETRYABLE
    if any(
        hint in lowered
        for hint in ("no result", "!= expected", "not up", "unhealthy", "missing", "rejected")
    ):
        return ErrorClass.DETERMINISTIC
    return ErrorClass.UNKNOWN
