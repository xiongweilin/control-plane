#Requires -Version 7.0

param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir '.venv\Scripts\python.exe'))) {
    throw "Virtual environment not found. Run: uv sync --extra dev"
}
if (-not [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'User')) {
    throw "CONTROL_PLANE_API_KEY user environment variable is not set."
}

$python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$action = New-ScheduledTaskAction -Execute $python -Argument '-m control_plane --log-level info' -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'ControlPlane' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

$ruleName = 'ControlPlane 18083'
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18083 -RemoteAddress LocalSubnet | Out-Null
}

Write-Host 'ControlPlane scheduled task and firewall rule installed.' -ForegroundColor Green
Write-Host 'Start now: Start-ScheduledTask -TaskName ControlPlane' -ForegroundColor Yellow
