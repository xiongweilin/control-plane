# Windows personal deployment

This directory is the personal Windows deployment shell around `agent-kernel`.
It owns Task Scheduler, hidden launch, watchdog/liveness probing, local firewall
setup and log protection. It does not own runtime, cognitive-control,
responsibility, record, authority, recovery or verification semantics.

The Python application is `control_plane`; its generic kernel is installed from
`xiongweilin/agent-kernel` as the `portable-runtime` distribution.

Install dependencies first:

```powershell
uv sync --extra dev
```

Then run `install-control-plane.ps1` from an elevated PowerShell 7 session.
`CONTROL_PLANE_API_KEY` must exist in the user environment.

Canonical files:

- `install-control-plane.ps1` — Scheduled Task and firewall setup.
- `Run-ControlPlane.ps1` / `Run-ControlPlaneHidden.vbs` — process supervisor.
- `Watch-ControlPlane.ps1` / `Watch-ControlPlaneHidden.vbs` — liveness watchdog.

The supervisor and installer resolve the repository root two levels above this
deployment directory, so the scheduled task can run from any working directory.

There is intentionally no portable-runtime copy, portable-local deployment, or
migration shim in this repository.
