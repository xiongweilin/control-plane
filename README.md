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
/cp policy <fingerprint> auto|manual|ignore
/cp run <fingerprint>
/cp ignore <fingerprint>
/cp evidence
/cp pause | resume
/cp promote <candidate_id>
/cp dismiss <candidate_id>
/task <描述> 派发任务给 Agent 执行
```

飞书普通消息（非命令）会走 Dify Chatflow 对话；`/new` 开始新会话。
`/task <描述>` 会把任务派发给控制平面的 Codex Agent（deepseek-v4-flash），
执行过程同样推送：任务已接收 → Agent 启动 → 心跳 → 完成/失败。

告警级策略：每个告警指纹（fingerprint）可以单独设置 `auto`（自动修复，默认）、
`manual`（收到告警后等你决定，`/cp run` 执行或 `/cp ignore` 忽略）或 `ignore`（直接忽略）。
告警恢复后，该指纹的自动修复尝试计数会重置。

沉淀文件位置（可直接打开）：

- `D:\download\agent\control-plane\data\agent-sessions\{repair_id}.jsonl` 与 `-last.md`（Codex 会话原文与摘要）
- `D:\download\agent\control-plane\data\evidence\`（EvidenceRecord JSON）
- `D:\download\agent\control-plane\data\patches\`（候选补丁）
- `D:\download\agent\control-plane\data\control-plane.db`（repairs/actions/candidates/playbooks）

也可用 `/cp evidence` 或 `GET /v1/evidence`（需 X-Control-Plane-Key）查看最近证据。

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

## Agent 操作边界

Agent 可对运行中的 Compose 项目（dify、feedback-analysis-agent、catalog-ops-automation、
observability、feishu-dify-gateway）执行 `docker compose restart / up -d`、`docker restart`
与只读诊断，以及 URL 探针与 PromQL 查询。禁止不可逆操作：删除/清空数据卷或数据库
（`docker compose down -v`、`docker volume rm`、DROP/TRUNCATE、删除持久化数据）、
`docker compose down`、`git push --force` 或删除 main/受保护分支、修改凭据/防火墙/sshd、
停止或删除含持久化数据的容器。

## 验证器（确定性检查）

修复完成后由确定性模块验证，不接受模型自述：

- 容器：`docker ps` 状态必须 `Up`，`unhealthy` / `restarting` 判失败；无容器判失败。
- 探针：HTTP 状态码需在期望集合内（默认 200/301/302/307/401），可校验响应体关键字。
- PromQL：查询必须有结果；指定 `expected` 时所有样本值必须等于期望值。
- 日志：按时间窗口（默认近 10 分钟、200 行）扫描致命模式（Traceback/panic/FATAL 等，可配置）。
- Git：仅当 Agent 实际改动代码（记录 branch）才做 diff 校验；非 git 目录跳过 diff。

按告警类型自动推导证据：`ServiceDown`（实例为 URL）→ 探针 + `probe_success == 1`；
`PrometheusScrapeFailed` → `up{instance=...} == 1`；无代码/探针证据的纯运维修复
回退到白名单项目容器基线。无任何确定性检查时判失败（minimum_evidence）。

## 每日沉淀整理

控制平面每日 21:30 自动运行一次沉淀整理（`POST /v1/digest` 可手动触发）：

- 用模型审阅候选经验、近期证据文件与会话摘要；
- `DROP` 的候选自动归档，`KEEP` 的候选保留；
- 无论结果如何都会向飞书发送一次消息：有保留项时列出候选并提示
  `/cp promote <id>` 晋升、`/cp dismiss <id>` 归档；无沉淀或无价值时发送简短确认。

配置：`digest_enabled`、`digest_time`、`digest_max_candidates`。
