# ADR-0011：计划任务监督、兜底触发与退出证据

- 日期：2026-08-10
- 状态：已接受
- 相关：ADR-0002（进程树与 PID）、ADR-0007（最小权限运行账户）、研究记录 0008（Windows 服务迁移）

## 背景

`ControlPlane` 原先只有开机触发器，`wscript.exe` 同步等待 Python 服务退出。任务配置的 `RestartOnFailure` 能处理普通失败，但不能保证覆盖“任务被结束、包装进程与子进程一起被硬终止”这类路径；此时任务保持 `Ready`，直到下次开机都不会再启动。启动器也没有保存 stdout、stderr 或退出码，Task Scheduler Operational 日志又处于关闭状态，因此历史终止原因不可追溯。SQLite 中相应 `run_records` 永久停留在 `running`。

## 决策

1. 保持计划任务形态，不在本次事故窗口迁移运行账户或引入 Windows 服务。
2. `ControlPlane` 直接以 PowerShell 7 执行 `Run-ControlPlane.ps1`，不再经会脱离任务生命周期的 VBS 中间进程。监督器分离并轮转 stdout、stderr 和最小启动日志；以 `/live`（不依赖 Prometheus/Alertmanager）做存活探测，越过 90 秒启动宽限后连续 4 次失败才终止子进程并返回失败。
3. `ControlPlane` 保持开机触发；独立 `ControlPlaneWatchdog` 每分钟只读探测 `/live`，仅在主任务未运行时启动它。主任务运行中由监督启动器负责挂起检测，watchdog 不竞争重启所有权。
4. 安装时启用 `Microsoft-Windows-TaskScheduler/Operational`，让任务启动、结束与重试路径保留系统证据。
5. 新实例创建运行记录时，把更早的 `running` 记录原子收敛为 `interrupted` 并补记 `stopped_at`。单实例锁保证此推断不会覆盖合法并发实例。

## 备选方案

- 只提高 `RestartCount`：不能覆盖任务被显式结束的路径，也不能发现存活但无响应的 Python 进程。
- 在主任务上直接增加重复触发：实现简单，但 `IgnoreNew` 每分钟产生 Task Scheduler 322 警告并污染 `LastTaskResult`；事故修复验证中已观察到该噪音，因此改用独立 watchdog。
- 立即迁移 Windows 服务：能提供更强的服务控制语义，但受 ADR-0007 运行账户与凭据边界约束，应保留为独立迁移窗口。

## 后果与回退

- 正面：任务状态与监督器生命周期一致；硬终止后最迟约 1 分钟由 watchdog 重新触发；挂起实例约 3 分钟内被监督器替换；健康运行不产生重复启动警告；未来事故可区分子进程退出、存活探测失败与整个任务被终止。
- 代价：确定性配置错误会按每分钟重试并持续告警；轮转日志保留当前文件与最近 5 次启动，需按敏感运行数据边界保护。
- 回退：把计划任务恢复为仅开机触发，并将动作改回直接启动 Python；代码层可回退本 ADR 对应提交。Windows 服务迁移决策不受影响。

## 修正记录（2026-08-10）

决策 2 的任务动作由“直接以 PowerShell 7 执行 `Run-ControlPlane.ps1`”调整为
`wscript.exe //B //NoLogo` + `Run-ControlPlaneHidden.vbs` 同步隐藏包装后再执行
`Run-ControlPlane.ps1`。触发原因：Windows Terminal 为默认终端宿主时，直接启动的
pwsh 控制台会在每次任务启动时创建可见的 Windows Terminal 宿主窗口并长驻，违背
watchdog 弹窗治理目标。

语义保持不变，仅消除控制台窗口：

- 包装 VBS 使用 `shell.Run(command, 0, True)`（SW_HIDE 且同步等待）并以
  `WScript.Quit exitCode` 透传 launcher 退出码，任务生命周期、任务状态与
  `LastTaskResult` 语义与直接 pwsh 动作一致，不再存在“脱离任务生命周期的 VBS”问题；
- 监督、stdout/stderr/launcher 轮转日志与退出证据仍由 `Run-ControlPlane.ps1` 承担，
  与决策 2–5 完全一致；
- 孤儿风险与直接 pwsh 等效：任一中间层被硬终止时，其子进程同样成为孤儿，恢复路径
  仍由 watchdog（每分钟 `/live` 探测 + `Start-ScheduledTask`）与任务重启策略兜底；
- 安装入口 `install-control-plane.ps1` 同步改为注册 `wscript.exe` 动作，重装即恢复
  该配置。
