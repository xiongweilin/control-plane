#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$ProjectDir = (Split-Path -Parent $PSScriptRoot),
    [ValidateRange(5, 300)]
    [int]$ProbeIntervalSeconds = 30,
    [ValidateRange(0, 600)]
    [int]$StartupGraceSeconds = 90,
    [ValidateRange(1, 20)]
    [int]$LivenessFailureThreshold = 4
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
$logDir = Join-Path $ProjectDir 'data\logs'
$stdoutLog = Join-Path $logDir 'control-plane.stdout.log'
$stderrLog = Join-Path $logDir 'control-plane.stderr.log'
$launcherLog = Join-Path $logDir 'control-plane.launcher.log'
$liveUrl = 'http://127.0.0.1:18083/live'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Virtual environment not found. Run: uv sync --extra dev"
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Protect-LogDirectory {
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $icacls = Join-Path $env:WINDIR 'System32\icacls.exe'
    & $icacls $logDir `
        /inheritance:r `
        /grant:r `
        "*${currentSid}:(OI)(CI)F" `
        '*S-1-5-18:(OI)(CI)F' `
        '*S-1-5-32-544:(OI)(CI)F' *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to protect log directory (exit $LASTEXITCODE)."
    }
}

function Rotate-ExactLog {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [int]$ArchiveCount = 5
    )

    for ($index = $ArchiveCount - 1; $index -ge 1; $index--) {
        $source = "$Path.$index"
        $target = "$Path.$($index + 1)"
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Move-Item -LiteralPath $source -Destination $target -Force
        }
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
    }
}

function Write-LauncherEvent {
    param([Parameter(Mandatory)][string]$Message)

    $timestamp = (Get-Date).ToString('o')
    Add-Content -LiteralPath $launcherLog -Value "$timestamp $Message" -Encoding utf8
}

Protect-LogDirectory
Rotate-ExactLog -Path $stdoutLog
Rotate-ExactLog -Path $stderrLog
Rotate-ExactLog -Path $launcherLog

$child = $null
$launcherExitCode = 1
$startedAt = Get-Date
$consecutiveFailures = 0

try {
    $child = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'control_plane', '--log-level', 'info') `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -WindowStyle Hidden `
        -PassThru
    Write-LauncherEvent "child_started pid=$($child.Id)"

    while (-not $child.WaitForExit($ProbeIntervalSeconds * 1000)) {
        if (((Get-Date) - $startedAt).TotalSeconds -lt $StartupGraceSeconds) {
            continue
        }

        try {
            $response = Invoke-WebRequest -Uri $liveUrl -Method Get -TimeoutSec 5 -SkipHttpErrorCheck
            if ($response.StatusCode -eq 200) {
                $consecutiveFailures = 0
            }
            else {
                $consecutiveFailures++
                Write-LauncherEvent "liveness_failed status=$($response.StatusCode) consecutive=$consecutiveFailures"
            }
        }
        catch {
            $consecutiveFailures++
            $errorType = $_.Exception.GetType().Name
            Write-LauncherEvent "liveness_failed error_type=$errorType consecutive=$consecutiveFailures"
        }

        if ($consecutiveFailures -ge $LivenessFailureThreshold) {
            Write-LauncherEvent "liveness_threshold_reached action=terminate_child"
            $taskkill = Join-Path $env:WINDIR 'System32\taskkill.exe'
            & $taskkill /PID $child.Id /T /F *> $null
            $child.WaitForExit()
            $launcherExitCode = 1
            break
        }
    }

    if ($child.HasExited -and $consecutiveFailures -lt $LivenessFailureThreshold) {
        $child.Refresh()
        $childExitCode = $child.ExitCode
        Write-LauncherEvent "child_exited exit_code=$childExitCode"
        # A long-running server exiting cleanly is still unexpected and must be restarted.
        $launcherExitCode = if ($childExitCode -eq 0) { 1 } else { $childExitCode }
    }
}
catch {
    $errorType = $_.Exception.GetType().Name
    Write-LauncherEvent "launcher_failed error_type=$errorType"
    if ($null -ne $child -and -not $child.HasExited) {
        $taskkill = Join-Path $env:WINDIR 'System32\taskkill.exe'
        & $taskkill /PID $child.Id /T /F *> $null
    }
    $launcherExitCode = 1
}

exit $launcherExitCode
