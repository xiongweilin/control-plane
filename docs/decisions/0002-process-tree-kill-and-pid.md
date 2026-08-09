# ADR-002: 进程树级取消与 PID 单实例守护

## Status

Accepted

## Date

2026-08-09

## Context

控制平面通过计划任务（VBS 包装）常驻。运行现场曾出现同一时刻存在两个
`python.exe` 控制平面进程：计划任务停止时只终止了 VBS 包装进程或直接子进程，
`codex exec` 派生的 git/python/node/ssh 孙进程随会话残留。另外 CodexRunner 与
CommandExecutor 原先只调用 `proc.kill()`，只能杀直接子进程；计划任务“彻底终止
Python 子进程”失败即源于此。缺少 PID 文件时，任何启动路径都无法判断旧实例是否
仍存活，重启可能叠加成双实例。

## Decision

- 服务启动（`__main__` 与 FastAPI lifespan 双保险）调用
  `runtime.acquire_single_instance(data/control-plane.pid)`：PID 文件指向的进程
  存活且不是自己 → 拒绝启动；陈旧 PID（进程已死或 PID 归属变化）→ 覆盖写入。
- 超时/取消路径（CodexRunner、CommandExecutor）统一改用
  `terminate_process_tree_async(pid)`：Windows 上执行
  `taskkill /PID <pid> /T /F`（按父进程链递归终止），失败时回退 `os.kill`。
- 提供残留验证函数 `assert_no_residual_processes(pid, names)`：先快照
  Win32_Process 的父/子关系（BFS 收集全部后代，含已脱离重挂的孙进程），杀树后
  复查，确保无 git/python/node/ssh 存活；测试用假进程模拟，不真跑 codex。
- 优雅停止：lifespan 退出时取消扫描/整理/恢复任务、关闭 SQLite（WAL 刷盘）、
  写 run_records.stopped_at、删除 PID 文件。

## Alternatives Considered

### psutil 依赖

拒绝。为进程树枚举引入新依赖不值得；taskkill /T /F 是 Windows 原生且可靠的
树终止方式，快照枚举用一次性 PowerShell `Get-CimInstance` 完成。

### 仅杀直接子进程

拒绝。正是该行为的残留导致了双实例与孤儿进程问题。

## Consequences

- 计划任务停止可彻底终止整个 codex 会话进程树。
- 重启幂等：新实例启动前检测旧 PID，避免双实例（原双 python 进程场景被防御）。
- `assert_no_residual_processes` 用于测试与人工诊断；误杀风险受限于
  taskkill 的 PID 参数（仅本服务派生的进程树）。
