Option Explicit

' Hidden console-suppression wrapper for the ControlPlane scheduled task.
' Runs the pwsh launcher (Run-ControlPlane.ps1) synchronously with SW_HIDE so the
' launcher's console never creates a Windows Terminal hosting window, while the
' task lifecycle, supervision, and exit-code evidence stay identical to a direct
' pwsh action (shell.Run ... True + WScript.Quit propagate the launcher exit code).

Dim shell, fso, scriptDir, ps1, pwsh, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(scriptDir, "Run-ControlPlane.ps1")
pwsh = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\scoop\apps\pwsh\current\pwsh.exe"
If Not fso.FileExists(pwsh) Then pwsh = "pwsh.exe"

command = Chr(34) & pwsh & Chr(34) & " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Chr(34) & ps1 & Chr(34)
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
