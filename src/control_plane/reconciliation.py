"""Durable descriptors and pure postcondition classification for side effects.

The provider is responsible for performing an operation; this module records
the coordinates needed to ask reality again after a timeout, crash, or process
restart.  A descriptor is intentionally not an execution journal: it stores
the request identity, pre-effect baseline, expected postcondition, and
observation coordinates.  Reconciliation code must re-observe the external
system rather than trusting a previous provider result.

This module is private to ``control-plane``.  It does not modify or extend the
public ``portable-runtime`` package.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReconciliationState(StrEnum):
    """Durable state of a side-effect descriptor.

    ``unknown`` and ``needs-reconciliation`` are deliberately non-terminal:
    they mean that the next observer must query reality again.  They must not
    be converted into provider or verification failure without evidence.
    """

    PENDING = "pending"
    APPLIED = "applied"
    NOT_APPLIED = "not-applied"
    IN_PROGRESS = "in-progress"
    CONCURRENT_CHANGE = "concurrent-change"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"
    NEEDS_RECONCILIATION = "needs-reconciliation"


class ReconciliationVerdict(StrEnum):
    """Classification returned by a deterministic observer."""

    APPLIED = "applied"
    NOT_APPLIED = "not-applied"
    IN_PROGRESS = "in-progress"
    CONCURRENT_CHANGE = "concurrent-change"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class BaselineSnapshot(BaseModel):
    """The last reality observed immediately before the side effect."""

    model_config = ConfigDict(extra="forbid")

    captured_at: datetime = Field(default_factory=_utcnow)
    values: dict[str, Any] = Field(default_factory=dict)


class GitMergeOperation(BaseModel):
    """Coordinates and identity of a candidate merge."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.merge"] = "git.merge"
    repo: str = Field(min_length=1)
    target_ref: str = Field(default="main", min_length=1)
    candidate_ref: str = Field(min_length=1)
    candidate_commit: str = Field(min_length=1)
    target_baseline_commit: str = Field(min_length=1)


class GitPushOperation(BaseModel):
    """Coordinates and expected remote ref for a Git push."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.push"] = "git.push"
    repo: str = Field(min_length=1)
    remote: str = Field(default="origin", min_length=1)
    branch: str = Field(default="main", min_length=1)
    expected_commit: str = Field(min_length=1)


class DockerOperation(BaseModel):
    """Coordinates for a Docker Compose desired-state operation."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["docker.restart", "docker.compose.up"]
    project: str = Field(min_length=1)
    project_dir: str = Field(min_length=1)
    services: list[str] = Field(default_factory=list)
    desired_state: Literal["running", "healthy"] = "healthy"


OperationSpec = GitMergeOperation | GitPushOperation | DockerOperation


class GitMergePostcondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.merge.ancestry"] = "git.merge.ancestry"
    target_ref: str = Field(min_length=1)
    candidate_commit: str = Field(min_length=1)
    target_baseline_commit: str = Field(min_length=1)
    rule: Literal["candidate-ancestor-of-target"] = "candidate-ancestor-of-target"


class GitPushPostcondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.push.remote-ref"] = "git.push.remote-ref"
    remote: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    expected_commit: str = Field(min_length=1)
    rule: Literal["remote-ref-equals-expected-commit"] = "remote-ref-equals-expected-commit"


class DockerPostcondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["docker.health"] = "docker.health"
    project: str = Field(min_length=1)
    desired_state: Literal["running", "healthy"] = "healthy"
    rule: Literal["all-observed-containers-satisfy-desired-state"] = (
        "all-observed-containers-satisfy-desired-state"
    )


PostconditionSpec = GitMergePostcondition | GitPushPostcondition | DockerPostcondition


class GitMergeObservationCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.merge"] = "git.merge"
    repo: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    merge_head_path: str = Field(min_length=1)
    status_command: str = "git status --short --branch"


class GitPushObservationCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["git.push"] = "git.push"
    repo: str = Field(min_length=1)
    remote: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    remote_ref: str = Field(min_length=1)
    query: str = "git ls-remote <remote> refs/heads/<branch>"


class DockerObservationCoordinates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["docker"] = "docker"
    project: str = Field(min_length=1)
    project_dir: str = Field(min_length=1)
    compose_project_label: str = Field(min_length=1)
    query: str = "docker ps --filter label=com.docker.compose.project=<project>"


ObservationCoordinates = GitMergeObservationCoordinates | GitPushObservationCoordinates | DockerObservationCoordinates


class ReconciliationObservation(BaseModel):
    """A fresh observation and its deterministic classification."""

    model_config = ConfigDict(extra="forbid")

    observed_at: datetime = Field(default_factory=_utcnow)
    verdict: ReconciliationVerdict
    message: str = Field(default="", max_length=2_000)
    details: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class ReconciliationDescriptor(BaseModel):
    """Durable, restart-safe coordinates for one external operation."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(default="unknown", min_length=1)
    capability: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    resource_ref: str | None = None
    subject_version_refs: list[str] = Field(default_factory=list)
    request_snapshot: dict[str, Any] = Field(default_factory=dict)
    operation: OperationSpec = Field(..., discriminator="kind")
    pre_effect_baseline: BaselineSnapshot
    expected_postcondition: PostconditionSpec = Field(..., discriminator="kind")
    observation_coordinates: ObservationCoordinates = Field(..., discriminator="kind")
    state: ReconciliationState = ReconciliationState.PENDING
    last_observation: ReconciliationObservation | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        if self.capability != self.operation.kind:
            raise ValueError("capability must match operation kind")
        if isinstance(self.operation, GitMergeOperation):
            if not isinstance(self.expected_postcondition, GitMergePostcondition):
                raise ValueError("git.merge requires git.merge.ancestry postcondition")
            if not isinstance(self.observation_coordinates, GitMergeObservationCoordinates):
                raise ValueError("git.merge requires git.merge observation coordinates")
        elif isinstance(self.operation, GitPushOperation):
            if not isinstance(self.expected_postcondition, GitPushPostcondition):
                raise ValueError("git.push requires git.push.remote-ref postcondition")
            if not isinstance(self.observation_coordinates, GitPushObservationCoordinates):
                raise ValueError("git.push requires git.push observation coordinates")
        elif isinstance(self.operation, DockerOperation):
            if not isinstance(self.expected_postcondition, DockerPostcondition):
                raise ValueError("Docker operations require docker.health postcondition")
            if not isinstance(self.observation_coordinates, DockerObservationCoordinates):
                raise ValueError("Docker operations require docker observation coordinates")
        return self

    @classmethod
    def from_request(
        cls,
        *,
        descriptor_id: str,
        request: Any,
        provider_id: str,
        provider_version: str,
        operation: OperationSpec,
        pre_effect_baseline: BaselineSnapshot,
        expected_postcondition: PostconditionSpec,
        observation_coordinates: ObservationCoordinates,
    ) -> Self:
        """Build a descriptor from a ``CapabilityRequest``-compatible object.

        Only non-secret request identity is retained.  Raw parameters are not
        copied into the snapshot; operation-specific coordinates above are the
        canonical data needed to re-observe reality.
        """

        request_id = str(getattr(request, "id", ""))
        if not request_id:
            raise ValueError("request.id is required")
        model_dump = getattr(request, "model_dump", None)
        if callable(model_dump):
            raw = model_dump(mode="json", exclude={"parameters"})
        else:
            raw = {
                "id": request_id,
                "capability": str(getattr(request, "capability", operation.kind)),
                "resource_ref": getattr(request, "resource_ref", None),
                "subject_version_refs": list(getattr(request, "subject_version_refs", []) or []),
                "idempotency_key": getattr(request, "idempotency_key", None),
            }
        raw["parameter_names"] = sorted(str(key) for key in getattr(request, "parameters", {}) or {})
        return cls(
            id=descriptor_id,
            request_id=request_id,
            provider_id=provider_id,
            provider_version=provider_version,
            capability=operation.kind,
            idempotency_key=str(getattr(request, "idempotency_key", None) or request_id),
            resource_ref=getattr(request, "resource_ref", None),
            subject_version_refs=[str(value) for value in getattr(request, "subject_version_refs", []) or []],
            request_snapshot=raw,
            operation=operation,
            pre_effect_baseline=pre_effect_baseline,
            expected_postcondition=expected_postcondition,
            observation_coordinates=observation_coordinates,
        )

    def with_observation(self, observation: ReconciliationObservation) -> Self:
        """Return a new descriptor carrying a fresh observation and state."""

        state_map = {
            ReconciliationVerdict.APPLIED: ReconciliationState.APPLIED,
            ReconciliationVerdict.NOT_APPLIED: ReconciliationState.NOT_APPLIED,
            ReconciliationVerdict.IN_PROGRESS: ReconciliationState.IN_PROGRESS,
            ReconciliationVerdict.CONCURRENT_CHANGE: ReconciliationState.CONCURRENT_CHANGE,
            ReconciliationVerdict.MISMATCH: ReconciliationState.MISMATCH,
            ReconciliationVerdict.UNKNOWN: ReconciliationState.UNKNOWN,
        }
        return self.model_copy(
            update={
                "state": state_map[observation.verdict],
                "last_observation": observation,
                "updated_at": _utcnow(),
            }
        )


class GitMergeReality(BaseModel):
    """Fresh Git facts used by ancestry classification.

    ``candidate_is_ancestor`` is computed by the observer with Git's ancestry
    relation (for example ``git merge-base --is-ancestor``).  Keeping that
    command out of this pure classifier makes the decision testable and lets a
    restart-safe adapter re-run it later.
    """

    model_config = ConfigDict(extra="forbid")

    target_tip: str | None = None
    target_baseline_commit: str = Field(min_length=1)
    candidate_commit: str = Field(min_length=1)
    candidate_is_ancestor: bool | None = None
    merge_head: str | None = None
    conflicts: bool = False


def classify_git_merge_ancestry(reality: GitMergeReality) -> ReconciliationVerdict:
    """Classify a merge without treating changed target state as success.

    * ``applied``: candidate is in target ancestry;
    * ``not-applied``: target is still the pre-effect baseline;
    * ``concurrent-change``: target moved, but candidate is absent;
    * ``in-progress``: Git still reports an active merge/conflict;
    * ``unknown``: required ancestry facts could not be observed.
    """

    if reality.merge_head or reality.conflicts:
        return ReconciliationVerdict.IN_PROGRESS
    if reality.candidate_is_ancestor is True:
        return ReconciliationVerdict.APPLIED
    if reality.target_tip is None or reality.candidate_is_ancestor is None:
        return ReconciliationVerdict.UNKNOWN
    if reality.target_tip == reality.target_baseline_commit:
        return ReconciliationVerdict.NOT_APPLIED
    return ReconciliationVerdict.CONCURRENT_CHANGE


def classify_git_push_remote_ref(*, expected_commit: str, observed_commit: str | None) -> ReconciliationVerdict:
    """Classify a push from a freshly observed remote ref."""

    if observed_commit is None:
        return ReconciliationVerdict.UNKNOWN
    if observed_commit == expected_commit:
        return ReconciliationVerdict.APPLIED
    return ReconciliationVerdict.MISMATCH


def classify_docker_state(*, healthy: bool | None, desired_state: str = "healthy") -> ReconciliationVerdict:
    """Classify Docker desired state without attributing an event occurrence."""

    if healthy is None:
        return ReconciliationVerdict.UNKNOWN
    if healthy:
        return ReconciliationVerdict.APPLIED
    return ReconciliationVerdict.MISMATCH


class ReconciliationDescriptorStore:
    """Small SQLite-backed durable store for descriptors.

    The JSON payload is the schema authority; indexed columns make startup
    recovery queries cheap.  Saving is an upsert, so a provider can persist the
    descriptor before execution and persist each fresh observation idempotently.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._shared_connection: sqlite3.Connection | None = None
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            self._shared_connection = sqlite3.connect(self.path, timeout=10)
            self._shared_connection.row_factory = sqlite3.Row
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._shared_connection is not None:
            return self._shared_connection
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        """Close the shared in-memory connection, if one is in use."""

        if self._shared_connection is not None:
            self._shared_connection.close()
            self._shared_connection = None

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_descriptors (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reconciliation_state ON reconciliation_descriptors(state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reconciliation_request ON reconciliation_descriptors(request_id)"
            )

    def save(self, descriptor: ReconciliationDescriptor) -> ReconciliationDescriptor:
        payload = json.dumps(descriptor.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reconciliation_descriptors
                    (id, request_id, capability, state, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    request_id=excluded.request_id,
                    capability=excluded.capability,
                    state=excluded.state,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    descriptor.id,
                    descriptor.request_id,
                    descriptor.capability,
                    descriptor.state.value,
                    descriptor.updated_at.isoformat(),
                    payload,
                ),
            )
        return descriptor

    def get(self, descriptor_id: str) -> ReconciliationDescriptor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM reconciliation_descriptors WHERE id = ?", (descriptor_id,)
            ).fetchone()
        return ReconciliationDescriptor.model_validate(json.loads(row["payload"])) if row else None

    def get_by_request(self, request_id: str) -> ReconciliationDescriptor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM reconciliation_descriptors WHERE request_id = ? ORDER BY updated_at DESC LIMIT 1",
                (request_id,),
            ).fetchone()
        return ReconciliationDescriptor.model_validate(json.loads(row["payload"])) if row else None

    def list_open(self) -> list[ReconciliationDescriptor]:
        states = (
            ReconciliationState.PENDING.value,
            ReconciliationState.UNKNOWN.value,
            ReconciliationState.IN_PROGRESS.value,
            ReconciliationState.CONCURRENT_CHANGE.value,
            ReconciliationState.MISMATCH.value,
            ReconciliationState.NEEDS_RECONCILIATION.value,
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload FROM reconciliation_descriptors
                WHERE state IN (?, ?, ?, ?, ?, ?)
                ORDER BY updated_at
                """,
                states,
            ).fetchall()
        return [ReconciliationDescriptor.model_validate(json.loads(row["payload"])) for row in rows]

    def record_observation(
        self,
        descriptor_id: str,
        observation: ReconciliationObservation,
    ) -> ReconciliationDescriptor:
        descriptor = self.get(descriptor_id)
        if descriptor is None:
            raise KeyError(f"unknown reconciliation descriptor: {descriptor_id}")
        return self.save(descriptor.with_observation(observation))


__all__ = [
    "BaselineSnapshot",
    "DockerObservationCoordinates",
    "DockerOperation",
    "DockerPostcondition",
    "GitMergeObservationCoordinates",
    "GitMergeOperation",
    "GitMergePostcondition",
    "GitMergeReality",
    "GitPushObservationCoordinates",
    "GitPushOperation",
    "GitPushPostcondition",
    "ObservationCoordinates",
    "OperationSpec",
    "ReconciliationDescriptor",
    "ReconciliationDescriptorStore",
    "ReconciliationObservation",
    "ReconciliationState",
    "ReconciliationVerdict",
    "classify_docker_state",
    "classify_git_merge_ancestry",
    "classify_git_push_remote_ref",
]
