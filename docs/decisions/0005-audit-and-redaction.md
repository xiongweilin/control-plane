# ADR-005: 命令审计与敏感值脱敏

## Status

Accepted

## Date

2026-08-09

## Context

Agent/命令调用缺少可审计记录：谁执行了什么命令、退出码、耗时都不可查；codex
会话 JSONL 直接落盘，token/password/secret/api_key/Authorization 等字段可能随
工具参数写入会话文件与证据。输出大小也无上限，单个会话可撑爆磁盘。

## Decision

- `audit.py` 提供统一脱敏：
  - `redact_value`：递归将敏感键（token/password/secret/api_key/authorization/
    credential/private_key/…，正则不区分大小写）的值替换为 `***`；
  - `redact_args`：处理 `--key=value`、`--key value`、`Authorization: Bearer x`
    三种形态的命令行参数；
  - `redact_text`：对 JSONL 逐行解析后脱敏再序列化。
- `command_audit` 表记录每次命令/agent 调用：run_id、repair_id、kind
  （command|agent）、脱敏后 argv、退出码、耗时（ms）、truncated、error_class。
  CommandExecutor 与 CodexRunner 均写入；审计写入失败不影响修复（best-effort）。
- agent 输出上限 `max_agent_output_bytes`（默认 200KB）：会话 JSONL 写入前先
  `redact_text` 再截断，截断时记录 truncated=true。
- 只读检查命令 `python -m control_plane inspect-sessions`（以及
  `GET /v1/sessions/inspect`）：列出会话文件中“可能含敏感值的字段名”，
  绝不输出字段值。

## Consequences

- 每次命令调用可追溯（脱敏后的参数、退出码、耗时），满足证据链与审计需求。
- 会话文件不再携带敏感键值；既有历史会话文件不受影响（只读检查，不改写）。
- 截断标记让证据消费者知道输出不完整，避免把截断当完整结论。
