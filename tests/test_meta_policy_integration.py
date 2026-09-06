from __future__ import annotations

from meta_controller import StagedMetaPolicy

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy
from control_plane.meta_policy import MetaAutonomousRepairPolicy, MetaManualTaskPolicy


def test_default_policy_aliases_use_meta_controller() -> None:
    assert AutonomousRepairPolicy is MetaAutonomousRepairPolicy
    assert ManualTaskPolicy is MetaManualTaskPolicy
    assert issubclass(AutonomousRepairPolicy, StagedMetaPolicy)
    assert issubclass(ManualTaskPolicy, StagedMetaPolicy)


def test_profile_policy_code_is_reused_not_copied() -> None:
    assert MetaAutonomousRepairPolicy.__mro__[1].__name__ == "AutonomousRepairPolicy"
    assert MetaManualTaskPolicy.__mro__[1].__name__ == "ManualTaskPolicy"
