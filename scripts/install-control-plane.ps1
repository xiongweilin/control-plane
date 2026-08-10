#Requires -Version 7.0

param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this installer from an elevated PowerShell 7 session.'
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir '.venv\Scripts\python.exe'))) {
    throw "Virtual environment not found. Run: uv sync --extra dev"
}
if (-not [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'User')) {
    throw "CONTROL_PLANE_API_KEY user environment variable is not set."
}

$launcherVbs = Join-Path $PSScriptRoot 'Run-ControlPlaneHidden.vbs'
$action = New-ScheduledTaskAction `
    -Execute (Join-Path $env:WINDIR 'System32\wscript.exe') `
    -Argument "//B //NoLogo `"$launcherVbs`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$watchdog = Join-Path $PSScriptRoot 'Watch-ControlPlaneHidden.vbs'
$watchdogAction = New-ScheduledTaskAction `
    -Execute (Join-Path $env:WINDIR 'System32\wscript.exe') `
    -Argument "//B //NoLogo `"$watchdog`""
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden

$taskSchedulerLog = 'Microsoft-Windows-TaskScheduler/Operational'
& (Join-Path $env:WINDIR 'System32\wevtutil.exe') set-log $taskSchedulerLog /enabled:true
if ($LASTEXITCODE -ne 0) {
    throw "Failed to enable $taskSchedulerLog (exit $LASTEXITCODE)."
}

$ruleName = 'ControlPlane 18083'
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 18083 -RemoteAddress LocalSubnet | Out-Null
}

Register-ScheduledTask -TaskName 'ControlPlane' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Register-ScheduledTask -TaskName 'ControlPlaneWatchdog' -Action $watchdogAction -Trigger $watchdogTrigger -Settings $watchdogSettings -Principal $principal -Force | Out-Null

Write-Host 'ControlPlane, ControlPlaneWatchdog, task history, and firewall rule installed.' -ForegroundColor Green
Write-Host 'Start now: Start-ScheduledTask -TaskName ControlPlane' -ForegroundColor Yellow
