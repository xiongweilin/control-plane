#Requires -Version 7.0

[CmdletBinding()]
param(
    [string]$TaskName = 'ControlPlane',
    [string]$LiveUrl = 'http://127.0.0.1:18083/live'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    $response = Invoke-WebRequest -Uri $LiveUrl -Method Get -TimeoutSec 5 -SkipHttpErrorCheck
    if ($response.StatusCode -eq 200) {
        exit 0
    }
}
catch {
    # The task state below determines whether recovery is safe; response bodies are not logged.
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
switch ($task.State) {
    'Ready' {
        Start-ScheduledTask -TaskName $TaskName
        exit 0
    }
    'Running' {
        # Run-ControlPlane.ps1 owns hung-child detection and restart thresholds.
        exit 0
    }
    default {
        throw "$TaskName cannot be recovered from state $($task.State)"
    }
}
