# ADR-003: 超时分类（exec / comm / verify / approval）

## Status

Accepted

## Date

2026-08-09

## Context

原先只有 `per_repair_timeout_seconds` 一个超时，CodexRunner 超时、git/网络命令
超时、验证器超时、等待人工审批超时共用同一语义：无法区分故障阶段，也无法决定
哪些超时值得重试、哪些必须人工介入。

## Decision

- `[timeouts]` 配置四项：`exec_seconds`（agent 运行，默认 900）、
  `comm_seconds`（alertmanager/feishu/git/ssh 网络，默认 30）、
  `verify_seconds`（验证器，默认 60）、`approval_seconds`（等待人工审批，
  0=不限时，>0 超时后升级）、`git_push_seconds`、`lease_ttl_seconds`。
- 状态机新增 `TIMED_OUT`（执行超时终态）与 `RECOVERING`（重启恢复中），
  超时路径写入 repairs.timeout_kind（exec/comm/verify/approval）与证据文件。
- 执行超时：agent 无候选提交 → `TIMED_OUT`/failed + `timeout_kind=exec`；
  已提交候选 → 照旧进入 `needs_approval` 待审。
- 通信超时：CommandExecutor 抛出
  `Command timed out after Ns` → `timeout_kind=comm`，错误分类为 retryable。
- 验证超时：`_complete_repair` 用 `wait_for(verify_seconds)` 包裹，
  超时抛 `VerificationTimeoutError` → `timeout_kind=verify`。
- 审批超时：`approval_timeout_seconds > 0` 时等待有界，超时转 `ESCALATED`
  并 `timeout_kind=approval`，通知人工介入；`/cp approve|reject|rollback` 仍可处理。

## Consequences

- 超时阶段可审计、可度量（repairs.timeout_kind + evidence）。
- 可重试性判定独立于超时位置：exec 超时（有提交）→ 待审；comm/verify 超时 →
  retryable；approval 超时 → escalated，不再自动重试。
