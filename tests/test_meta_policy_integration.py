from __future__ import annotations

from meta_controller import StagedMetaPolicy

from control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy


def test_control_plane_policies_use_meta_controller_directly() -> None:
    assert issubclass(AutonomousRepairPolicy, StagedMetaPolicy)
    assert issubclass(ManualTaskPolicy, StagedMetaPolicy)
    assert AutonomousRepairPolicy.select is StagedMetaPolicy.select
    assert ManualTaskPolicy.select is StagedMetaPolicy.select
