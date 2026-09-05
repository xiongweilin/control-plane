# Control Plane 重建配置

本文记录当前 Windows 个人控制平面的非敏感重建配置。运行时凭据不写入仓库，按 ratio 的重建清单从 Bitwarden 注入。

## 目录和入口

- 仓库：xiongweilin/control-plane
- 目标目录：D:\agent\control-plane
- 依赖入口：pyproject.toml、uv.lock
- Windows 部署：deployments/windows-personal-platform/install-control-plane.ps1
- 进程入口：deployments/windows-personal-platform/Run-ControlPlane.ps1
- 看门狗：deployments/windows-personal-platform/Watch-ControlPlane.ps1
- 示例配置：control_plane.toml.example

## 当前配置

- diagnosis_model：gpt-5.6-luna
- execution_model：gpt-5.6-luna
- gateway_base_url：http://127.0.0.1:4101/v1
- Codex 客户端入口：http://127.0.0.1:4100/v1
- 端点：/healthz、/live、/ready、/metrics、/status、/v1/tasks、/v1/alerts/alertmanager、/v1/controllers/{controller_id}/command、/v1/game-mode、/v1/sessions/inspect
- Feishu transport 由 feishu-dify-gateway 负责。
- Git/Docker effect provider 只允许个人项目和仓库 allowlist 中的目标。
- CS2 游戏模式负责游戏期间暂停并在退出后恢复 Docker 容器。

## 安装和验证

uv sync --extra dev
pwsh -NoProfile -File .\deployments\windows-personal-platform\install-control-plane.ps1
uv run ruff check .
uv run mypy src
uv run pytest -q

安装脚本需要管理员 PowerShell；不要手工创建第二套启动入口。
