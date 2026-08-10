Option Explicit

Dim shell, fso, scriptDir, ps1, pwsh, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = fso.BuildPath(scriptDir, "Watch-ControlPlane.ps1")
pwsh = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\scoop\apps\pwsh\current\pwsh.exe"
If Not fso.FileExists(pwsh) Then pwsh = "pwsh.exe"

command = Chr(34) & pwsh & Chr(34) & " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Chr(34) & ps1 & Chr(34)
shell.Run command, 0, True
