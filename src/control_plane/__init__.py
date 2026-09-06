"""Autonomous personal operations deployment/profile for Agent Kernel."""

__version__ = "0.5.0"

# Install the replaceable meta-controller stage-selection layer once at package
# import. Domain-specific repair/task behavior remains in alert_policy; only the
# duplicated generic ControllerPolicy selection topology is replaced.
from . import alert_policy as _alert_policy
from .meta_policy import MetaAutonomousRepairPolicy, MetaManualTaskPolicy

_alert_policy.AutonomousRepairPolicy = MetaAutonomousRepairPolicy
_alert_policy.ManualTaskPolicy = MetaManualTaskPolicy
_alert_policy.UnattendedAlertPolicy = MetaAutonomousRepairPolicy
