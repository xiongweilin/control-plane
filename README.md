# control-plane

个人平台控制平面：接收本地 Alertmanager 告警，触发**完整 Codex agent 会话**（`codex exec` + `deepseek/deepseek-v4-flash`，经本机 OpenCodex 路由），让模型使用 Codex 全量工具诊断与修复，而非裸 Responses API 的少量函数；代码/配置修改必须提交到 `fix/control-plane-<id>` 候选分支，经飞书审批后才合并。所有事件按《控制平面》（由《证据与演化语义》晋升合并）记录到 SQLite 与 JSON 证据文件；成功修复自动沉淀候选经验，经验晋升 official playbook 需飞书审批。

## 架构

```text
Prometheus → Alertmanager
              └─ webhook → control-plane :18083（Windows 常驻）
                              ├─ 指纹去重 / 冷却 / 预算
                              ├─ Codex agent 会话（deepseek-v4-flash 完整工具环境）
                              ├─ 独立验证器 + 自动回滚
                              ├─ SQLite + data/evidence/*.json
                              └─ 飞书通知 / 审批（feishu-dify-gateway 扩展命令）
```

## 快速开始

```powershell
uv sync --extra dev
uv run ruff check .
uv run pytest
Copy-Item control_plane.toml.example control_plane.toml
[Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', '<随机密钥>', 'User')
uv run python -m control_plane
```

## 权限矩阵

- Codex agent 在指定项目仓库内以完整工具环境运行，受任务 prompt 硬约束、全局 AGENTS.md 与控制平面独立验证约束。
- 自动运维（可逆）：agent 可对白名单 compose 项目执行 restart / up -d、只读诊断、清理与健康等待；控制平面事后独立核验容器状态与 git 状态。
- 候选 + 审批：agent 对代码/配置的修改必须提交到 `fix/control-plane-<id>` 分支；控制平面读取分支差异、拒绝修改验证器/告警规则/权限/control-plane 自身，审批后合并 main 并推送。
- 默认禁止：文件写入（候选分支除外）、依赖变更、数据库写、云端写、凭据访问、删除数据、修改验证器/告警规则/权限。

## Agent 触发说明

控制平面启动的是原生 `codex.exe`（npm 包内 `@openai/codex-win32-x64`）：

```text
codex exec -m opencode-go/deepseek-v4-flash -C <项目 Windows 路径>
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
  --json -o <会话摘要> -
```

工作目录使用 Windows 原生路径（WSL 已于 2026-08-07 退役）。控制平面是权威边界：它注入硬约束、独立验证结果、把关代码合并审批并执行回滚；`--dangerously-bypass-approvals-and-sandbox` 只让会话在控制平面内部自动执行，不绕过控制平面自身的门禁。

## 飞书命令

```text
/cp status
/cp approve <repair_id> | reject <repair_id> | rollback <repair_id>
/cp pause | resume
/cp promote <candidate_id>
```

## 交互体验

修复生命周期通过飞书分阶段通知，减少“静默等待”：

- 开始修复：repair_id、告警描述、今日 Agent 预算剩余；已知模式会提示已支持次数。
- Agent 启动：目标仓库 / 分支 / 模型；运行中默认每 120 秒发一次心跳。
- 验证通过 → 修复完成（验证摘要 + 回滚命令）；失败则说明原因与下一步指引。
- 噪音分级：测试/烟雾告警（AlertmanagerE2E、smoke-* 等）只记录不触发修复；
  冷却期跳过会提示剩余时间；候选再次复现提示“已知模式第 N 次”。
- 审批：代码变更后等待 `/cp approve|reject|rollback`，或直接调用
  `POST /v1/approvals/{repair_id}/decision`（需 X-Control-Plane-Key）。
- 可观测：`/metrics` 暴露修复计数/状态、候选、预算与当日 Agent 调用，
  已接入 Prometheus 与 Metratio Overview 看板。

相关配置：`notify_heartbeat_seconds`、`notify_cooldown_skip`、
`notify_ignored_noise`、`test_alert_alertnames`、`test_alert_instance_prefixes`。

## 部署

```powershell
./scripts/install-control-plane.ps1
```

脚本注册开机计划任务 `ControlPlane` 并添加仅限本地子网的防火墙规则（TCP 18083）。密钥通过用户环境变量 `CONTROL_PLANE_API_KEY` 提供，不进入仓库、日志或命令历史。

## 候选经验生命周期

- 成功修复自动创建 candidate（90 天期限，默认到期归档）。
- 候选只进入 Agent 推理上下文，不自动取得修改权限。
- `/cp promote <candidate_id>` 需要飞书审批授权；晋升后进入 official playbook 并参与自动工具决策。
