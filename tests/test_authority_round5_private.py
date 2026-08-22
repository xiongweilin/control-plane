"""Private-profile coverage for the round-five authority boundaries."""

from __future__ import annotations

from datetime import timedelta

from portable_runtime.protocol.validation import validate_state_graph
from portable_runtime.records.authorization import create_grant_for_approval
from portable_runtime.records.models import Assertion
from portable_runtime.records.revision import apply_revision, create_revision
from portable_runtime.stores.memory import InMemoryStateStore


def test_revision_authorization_use_survives_later_grant_expiry() -> None:
    store = InMemoryStateStore()
    old = Assertion(
        id="round5_revision_old",
        statement="old",
        lifecycle_status="current",
        epistemic_status="supported",
    )
    new = Assertion(id="round5_revision_new", statement="new", lifecycle_status="draft")
    store.save_record(old)
    store.save_record(new)
    revision = create_revision(old.id, new.id)
    store.save_record(revision)
    grant = create_grant_for_approval(
        principal_ref="human:owner",
        grantee_ref="agent:executor",
        allowed_capabilities=["revision.apply"],
        subject_version_refs=[revision.id],
        resource_scope=[old.id],
        effect_ceiling="write-local",
    )
    store.save_authorization(grant)

    applied = apply_revision(
        revision,
        store=store,
        authorization_ref=grant.id,
        actor_ref="agent:executor",
    )
    use_ref = applied.metadata.get("authorization_use_ref")
    assert isinstance(use_ref, str)
    use = store.get_authorization_use(use_ref)
    assert use is not None and use.capability == "revision.apply"

    # The action-time proof remains valid even when the grant is later expired.
    expired = grant.model_copy(update={"expires_at": grant.valid_from + timedelta(seconds=1)})
    store.save_authorization(expired)
    errors = validate_state_graph(store.export_state())
    assert not any(f"revision {applied.id}" in error and "authorization" in error for error in errors)


def test_terminal_context_capability_is_removed() -> None:
    store = InMemoryStateStore()
    assert not hasattr(store, "terminal_completion")
    assert callable(getattr(store, "commit_terminal", None))
