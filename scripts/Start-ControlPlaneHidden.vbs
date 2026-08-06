Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\download\agent\control-plane"
exitCode = shell.Run("""" & "D:\download\agent\control-plane\.venv\Scripts\python.exe" & """ -m control_plane --log-level info", 0, True)
WScript.Quit exitCode
