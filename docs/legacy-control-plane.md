# Legacy control-plane profile

`control_plane` remains the production compatibility profile. It still owns
the existing repair lifecycle, Codex execution, deterministic verification,
Alertmanager trigger, Feishu notifications, SQLite tables and Windows
deployment scripts. Do not remove it until the corresponding portable workflow
has a passing parity test and a rollback plan.
