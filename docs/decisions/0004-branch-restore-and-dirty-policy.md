# ADR-004: Git 分支恢复与 dirty worktree 策略

## Status

Accepted

## Date

2026-08-09

## Context

修复 Agent 结束时必须在 `finally` 把工作目录恢复到原分支。此前
`_restore_workspace_states` 直接 `git switch` 回原 ref：若工作树存在用户未提交
修改，切换会被 git 拒绝或（在极端情况下）掩盖差异，且失败只记录日志不阻断。
另外脏工作树中启动 agent，候选分支可能建立在错误基线上。

## Decision

- 恢复前先 `git status --porcelain`：非空 → **放弃恢复**，记录
  “workspace is dirty; restore abandoned to protect uncommitted changes”，
  不执行 `git switch`，保留用户文件；错误计入恢复错误列表并通知。
- 启动前检查工作树：`dirty_worktree_policy` 两种取值：
  - `reject`（默认）：脏工作树直接拒绝执行 agent（确定性失败，不自动重试）；
  - `isolate`：`git worktree add --detach <临时目录> main`，agent 在隔离
    worktree 内建候选分支并提交，结束后 `git worktree remove --force` 清理
    （隔离目录内未提交内容随目录丢弃），候选分支保留在仓库 refs 中，主工作树
    不受影响。
- 候选分支统一命名 `candidate_branch_prefix`（默认 `fix/control-plane-`）。

## Alternatives Considered

### 强制切换并依赖 git 冲突报错

拒绝。会丢失对“谁改了什么”的可见性，且恢复失败影响主工作树。

### 恢复前 stash

拒绝。stash 有丢失/混淆用户改动与 agent 改动的风险，隔离 worktree 更干净。

## Consequences

- 用户未提交修改永不因控制平面恢复被覆盖；脏场景明确告警并记录证据。
- `isolate` 模式增加一次 worktree 创建/删除开销，但主工作树零污染。
- 真实 git 临时仓库测试覆盖：干净恢复、dirty 放弃、reject 拒绝、isolate 隔离。
