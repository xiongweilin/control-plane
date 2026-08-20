# Portable local deployment

The minimal local deployment is independent of Codex, Feishu, Prometheus and
Docker:

```powershell
.venv\Scripts\python.exe -m portable_runtime status
.venv\Scripts\python.exe -m portable_runtime work submit --title "Echo test"
```

Use `--state path\to\portable-runtime.db` to select the SQLite state file.
