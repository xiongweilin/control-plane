from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from control_plane.reconciliation import (
    BaselineSnapshot,
    DockerObservationCoordinates,
    DockerOperation,
    DockerPostcondition,
    GitMergeObservationCoordinates,
    GitMergeOperation,
    GitMergePostcondition,
    GitMergeReality,
    GitPushObservationCoordinates,
    GitPushOperation,
    GitPushPostcondition,
    ReconciliationDescriptor,
    ReconciliationDescriptorStore,
    ReconciliationObservation,
    ReconciliationState,
    ReconciliationVerdict,
    classify_docker_state,
    classify_git_merge_ancestry,
    classify_git_push_remote_ref,
)
from portable_runtime.core.capabilities import CapabilityRequest


def _merge_descriptor() -> ReconciliationDescriptor:
    request = CapabilityRequest(
        id="request-merge-1",
        capability="git.merge",
        idempotency_key="idem-merge-1",
        resource_ref="repo:control-plane",
        subject_version_refs=["git:abc123"],
        parameters={"repo": "D:/agent/control-plane", "token": "must-not-persist"},
    )
    return ReconciliationDescriptor.from_request(
        descriptor_id="recon-merge-1",
        request=request,
        provider_id="personal-operations",
        provider_version="1.0.0",
        operation=GitMergeOperation(
            repo="D:/agent/control-plane",
            target_ref="main",
            candidate_ref="fix/control-plane-1",
            candidate_commit="abc123",
            target_baseline_commit="base000",
        ),
        pre_effect_baseline=BaselineSnapshot(values={"target_tip": "base000", "merge_head": None}),
        expected_postcondition=GitMergePostcondition(
            target_ref="main", candidate_commit="abc123", target_baseline_commit="base000"
        ),
        observation_coordinates=GitMergeObservationCoordinates(
            repo="D:/agent/control-plane",
            target_ref="main",
            candidate_ref="fix/control-plane-1",
            merge_head_path="D:/agent/control-plane/.git/MERGE_HEAD",
        ),
    )


def test_descriptor_carries_restart_coordinates_without_raw_parameters() -> None:
    descriptor = _merge_descriptor()

    assert descriptor.request_id == "request-merge-1"
    assert descriptor.provider_id == "personal-operations"
    assert descriptor.idempotency_key == "idem-merge-1"
    assert descriptor.resource_ref == "repo:control-plane"
    assert descriptor.subject_version_refs == ["git:abc123"]
    assert descriptor.operation.kind == "git.merge"
    assert descriptor.expected_postcondition.kind == "git.merge.ancestry"
    assert descriptor.observation_coordinates.kind == "git.merge"
    assert "must-not-persist" not in str(descriptor.request_snapshot)
    assert descriptor.request_snapshot["parameter_names"] == ["repo", "token"]


def test_git_merge_ancestry_classification_distinguishes_concurrent_change() -> None:
    assert (
        classify_git_merge_ancestry(
            GitMergeReality(
                target_tip="abc123",
                target_baseline_commit="base000",
                candidate_commit="abc123",
                candidate_is_ancestor=True,
            )
        )
        == ReconciliationVerdict.APPLIED
    )
    assert (
        classify_git_merge_ancestry(
            GitMergeReality(
                target_tip="base000",
                target_baseline_commit="base000",
                candidate_commit="abc123",
                candidate_is_ancestor=False,
            )
        )
        == ReconciliationVerdict.NOT_APPLIED
    )
    assert (
        classify_git_merge_ancestry(
            GitMergeReality(
                target_tip="other999",
                target_baseline_commit="base000",
                candidate_commit="abc123",
                candidate_is_ancestor=False,
            )
        )
        == ReconciliationVerdict.CONCURRENT_CHANGE
    )
    assert (
        classify_git_merge_ancestry(
            GitMergeReality(
                target_tip="base000",
                target_baseline_commit="base000",
                candidate_commit="abc123",
                candidate_is_ancestor=None,
            )
        )
        == ReconciliationVerdict.UNKNOWN
    )
    assert (
        classify_git_merge_ancestry(
            GitMergeReality(
                target_tip="base000",
                target_baseline_commit="base000",
                candidate_commit="abc123",
                candidate_is_ancestor=True,
                merge_head="abc123",
            )
        )
        == ReconciliationVerdict.IN_PROGRESS
    )


def test_push_and_docker_classifiers_keep_unobservable_state_unknown() -> None:
    assert classify_git_push_remote_ref(expected_commit="abc123", observed_commit=None) == ReconciliationVerdict.UNKNOWN
    assert classify_git_push_remote_ref(expected_commit="abc123", observed_commit="abc123") == ReconciliationVerdict.APPLIED
    assert classify_git_push_remote_ref(expected_commit="abc123", observed_commit="other999") == ReconciliationVerdict.MISMATCH
    assert classify_docker_state(healthy=None) == ReconciliationVerdict.UNKNOWN
    assert classify_docker_state(healthy=True) == ReconciliationVerdict.APPLIED
    assert classify_docker_state(healthy=False) == ReconciliationVerdict.MISMATCH


def test_descriptor_store_survives_reopen_and_records_observation(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    descriptor = _merge_descriptor()
    store = ReconciliationDescriptorStore(path)
    store.save(descriptor)
    observed = store.record_observation(
        descriptor.id,
        ReconciliationObservation(
            verdict=ReconciliationVerdict.CONCURRENT_CHANGE,
            message="target moved while candidate was not observed in ancestry",
            details={"target_tip": "other999"},
        ),
    )

    reopened = ReconciliationDescriptorStore(path)
    loaded = reopened.get(descriptor.id)
    assert loaded is not None
    assert loaded.state == ReconciliationState.CONCURRENT_CHANGE
    assert loaded.last_observation == observed.last_observation
    assert reopened.get_by_request(descriptor.request_id) is not None
    assert [item.id for item in reopened.list_open()] == [descriptor.id]


def test_descriptor_store_lists_unknown_and_pending(tmp_path: Path) -> None:
    store = ReconciliationDescriptorStore(tmp_path / "runtime.db")
    descriptor = _merge_descriptor()
    store.save(descriptor)
    assert [item.id for item in store.list_open()] == [descriptor.id]
    store.record_observation(
        descriptor.id,
        ReconciliationObservation(verdict=ReconciliationVerdict.UNKNOWN, message="remote ref unavailable"),
    )
    assert [item.id for item in store.list_open()] == [descriptor.id]


def test_descriptor_store_supports_a_single_process_memory_store() -> None:
    store = ReconciliationDescriptorStore(":memory:")
    descriptor = _merge_descriptor()
    store.save(descriptor)
    assert store.get(descriptor.id) == descriptor
    store.close()


def test_descriptor_consistency_rejects_wrong_operation_schema() -> None:
    with pytest.raises(ValidationError, match="capability must match operation kind"):
        ReconciliationDescriptor(
            id="bad",
            request_id="request-bad",
            provider_id="provider",
            capability="git.push",
            idempotency_key="idem-bad",
            operation=GitMergeOperation(
                repo="repo",
                candidate_ref="candidate",
                candidate_commit="abc123",
                target_baseline_commit="base000",
            ),
            pre_effect_baseline=BaselineSnapshot(),
            expected_postcondition=GitMergePostcondition(
                target_ref="main", candidate_commit="abc123", target_baseline_commit="base000"
            ),
            observation_coordinates=GitMergeObservationCoordinates(
                repo="repo", target_ref="main", candidate_ref="candidate", merge_head_path="repo/.git/MERGE_HEAD"
            ),
        )


def test_operation_schemas_cover_push_and_docker() -> None:
    push = ReconciliationDescriptor(
        id="recon-push",
        request_id="request-push",
        provider_id="personal-operations",
        capability="git.push",
        idempotency_key="idem-push",
        operation=GitPushOperation(repo="repo", expected_commit="abc123"),
        pre_effect_baseline=BaselineSnapshot(values={"remote_commit": "base000"}),
        expected_postcondition=GitPushPostcondition(remote="origin", branch="main", expected_commit="abc123"),
        observation_coordinates=GitPushObservationCoordinates(
            repo="repo", remote="origin", branch="main", remote_ref="refs/heads/main"
        ),
    )
    docker = ReconciliationDescriptor(
        id="recon-docker",
        request_id="request-docker",
        provider_id="personal-operations",
        capability="docker.restart",
        idempotency_key="idem-docker",
        operation=DockerOperation(kind="docker.restart", project="dify", project_dir="D:/compose/dify"),
        pre_effect_baseline=BaselineSnapshot(values={"containers": ["dify-api"]}),
        expected_postcondition=DockerPostcondition(project="dify"),
        observation_coordinates=DockerObservationCoordinates(
            project="dify", project_dir="D:/compose/dify", compose_project_label="dify"
        ),
    )
    assert push.expected_postcondition.kind == "git.push.remote-ref"
    assert docker.expected_postcondition.kind == "docker.health"
