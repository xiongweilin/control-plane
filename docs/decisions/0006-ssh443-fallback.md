# ADR-006: GitHub SSH 22 → 443 推送回退

## Status

Accepted

## Date

2026-08-09

## Context

审批通过后 `_apply_code_candidates` 执行 `git push origin main`。国内网络环境
下 GitHub SSH 22 端口时常被防火墙阻断，推送会卡死直至 git 自身超时；控制平面
的自动推送不能依赖人工干预。git push 本身无内置端口回退。

## Decision

- `gitpush.push_with_ssh_fallback`：先按常规方式 `git push origin <branch>`；
  失败且错误信息命中 SSH 网络特征（ssh/connection refused/timed out/
  could not resolve host/port 22 等）时，回退一次：
  - 读取 `remote get-url` 解析 `owner/repo`；
  - 改推 `ssh://git@{host}:{port}/{owner}/{repo}.git`（默认
    `ssh.github.com:443`，可用 `github_ssh_host_port` 覆盖）；
  - 注入 `GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=15
    -o StrictHostKeyChecking=accept-new -p 443"`，避免交互卡死；
  - 两次都失败时抛出带错误分类（retryable）的 ToolError。
- 开关：`github_ssh_fallback`（默认 true）；非网络类失败（如仓库不存在）
  不触发回退。
- 回退仅影响单次 push 的 URL 与 ssh 命令，不修改仓库 remote 配置。

## Alternatives Considered

### 永久改写 remote 为 443

拒绝。会改变用户本地 git 配置；按次回退更可控、可审计。

### 仅调大 git 超时

拒绝。22 端口被 RST/黑洞时超时仍会拖慢整个修复流程，回退是唯一出路。

## Consequences

- 自动推送在 22 被阻断时自动走 443，不再卡死。
- 审计记录可看到两次 push 尝试与最终路径（success via ssh.github.com:443）。
