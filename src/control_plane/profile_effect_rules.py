from __future__ import annotations

from typing import Any

from portable_runtime.core.capability_contract import CapabilityEffectRule


def register_profile_effect_rules(runtime: Any) -> None:
    """Register deployment-specific effect rules without widening Kernel semantics."""
    rules = [
        CapabilityEffectRule(
            capability="shell.exec",
            impact_class="write-local",
            authorization_required=False,
            resource_required=True,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="notify.send",
            impact_class="write-remote",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="git.merge",
            impact_class="write-local",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="git.push",
            impact_class="write-remote",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityEffectRule(
            capability="git.fast_forward",
            impact_class="write-local",
            authorization_required=False,
            resource_required=True,
            version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="git.push_exact_ref",
            impact_class="write-remote",
            authorization_required=False,
            resource_required=True,
            version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityEffectRule(
            capability="git.discard_line_ending_changes",
            impact_class="write-local",
            authorization_required=False,
            resource_required=True,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="chezmoi.apply",
            impact_class="write-local",
            authorization_required=False,
            resource_required=True,
            version_required=True,
            blast_radius=1,
            exposure=1,
        ),
        CapabilityEffectRule(
            capability="git.rollback",
            impact_class="write-local",
            authorization_required=True,
            resource_required=True,
            version_required=True,
            blast_radius=2,
            exposure=2,
        ),
        CapabilityEffectRule(
            capability="docker.restart",
            impact_class="write-remote",
            authorization_required=True,
            resource_required=True,
            version_required=False,
            blast_radius=2,
            exposure=2,
        ),
        # allowed_auto_projects is the standing personal authorization for this
        # one bounded apply operation; the provider independently enforces it.
        CapabilityEffectRule(
            capability="docker.compose.up",
            impact_class="write-remote",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=3,
            exposure=3,
        ),
        CapabilityEffectRule(
            capability="maintenance.cleanup_known_garbage",
            impact_class="write-local",
            authorization_required=False,
            resource_required=False,
            version_required=False,
            blast_radius=1,
            exposure=1,
        ),
    ]
    for rule in rules:
        runtime.contract_registry.register_effect_rule(rule)
