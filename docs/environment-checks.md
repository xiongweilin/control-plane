# 环境巡检闭环

`control-plane` 通过 `environment-inspection` provider 执行只读巡检。巡检结果在
`/metrics` 暴露，Alertmanager 仍通过现有 `/v1/alerts/alertmanager` 入口送入 Agent
Kernel。provider 的 `available` 表示“探针本身可用”，不表示被检查对象健康；对象健康
由 `control_plane_environment_check` 的 `status` 标签表达。

`automation` 标签表示处置路由意图，不是当前告警的安全性结论：`none` 表示无需处置，
`codex-judgment` 表示 problem/unknown 必须先进入当前 Codex 判断，`automatic` 只表示
精确的 known-garbage 路径可以作为候选 effect。所有未被抑制的 firing 告警都由 Codex 先判断；
`SAFETY_CLASS`、仓库干净性和具体目标由当前证据共同决定是否允许执行。

## 告警接收与恢复

control-plane 监听 `0.0.0.0:18083`，本机守护脚本仍使用
`http://127.0.0.1:18083/live`；Prometheus 和 Alertmanager 通过 Docker Desktop 的
`host.docker.internal:18083` 访问。Windows 防火墙由安装脚本幂等维护为 TCP、入站、
`RemoteAddress=LocalSubnet`、`Profile=Any`、禁止边界穿透。不要把 Docker Desktop 的
动态网关地址写入配置，也不要把 Docker Compose 改成 host network。

Alertmanager webhook 只做认证、字段清洗、指纹去重和 durable enqueue，然后立即返回
2xx；Codex 诊断、Agent Kernel Work/Run 闭环、效果执行和 Prometheus 验证由后台
dispatcher 完成。`policy.max_concurrent` 限制并发修复数；同一 firing fingerprint
最多执行两轮；有效 diagnosis episode 每轮各包含一次 diagnosis 和一次 execution，
无效 diagnosis 只消耗 diagnosis 重试预算，不进入 execution。diagnosis 与 execution
调用的独立超时都是 900 秒，不存在累计的 episode deadline，也不会用累计 1800 秒
截断第二轮。接收、完成、解析状态和每次阶段结果写入 Agent Kernel 事件存储，进程重启
后可从持久化状态继续尚未完成的轮次。

Codex provider timeout is a transient processing failure for the current phase:
it never proves `IRREVERSIBLE` and never authorizes an effect by itself. A first
valid diagnosis that declares an irreversible operation or finds a dirty target
repository escalates to Feishu immediately and stops automatic effects. A
provider failure, timeout, unavailable result or missing result enters `WAIT`
immediately, without a CognitiveClosure or execution Work. A successful provider
response with a missing or duplicated safety marker is malformed; it may consume
one bounded diagnosis-only retry, but it still cannot form a closure or execute
an effect. A valid but unresolved result consumes the next diagnosis/execution
round; after the second unresolved round the alert is escalated to Feishu. The
Codex CLI session budget is intentionally long enough for a complete 900-second
phase.

自动 effect 仍必须同时满足 `environment.automatic_handling_enabled`、告警 allowlist、
目标资源 allowlist 和 effect rule；这些条件只决定候选 effect 范围，不跳过 Codex 判断。
`SAFETY_CLASS=REVERSIBLE` 且证据完整时才允许相应的确定性 effect；`UNKNOWN` 只能进入
只读执行/验证路径，不能自行扩大权限。Prometheus 未提供 project 映射的告警不会凭
job/instance 猜测项目或执行 Compose effect。

`/ready` 与 `/metrics` 共用短时 kernel health cache，避免 Prometheus 抓取并发触发
重复 provider 探针；环境巡检的 Git 和 PowerShell 子进程使用 control-plane 自己的
有界终止路径，超时后不保留失控子进程。`/live` 只证明 HTTP 进程存活，不等待 Codex、
环境巡检或外部依赖。

## 覆盖范围

当前检查项为：

- `recoverability`：ratio 文档源和 Docker 备份存在且可读；缺失时不允许把环境声明为可恢复；
- `synchronization`：配置仓库的工作树无未提交变更、`origin/main` 与本地 HEAD 一致，且
  `chezmoi verify --skip-secrets` 成功；漂移或探针失败都不允许静默放行；
- `known_garbage`：只统计 `known_garbage_paths` 中的精确、可回滚处理路径；不把 Docker
  容器/卷/镜像/cache 或历史经验混入自动清理；
- `automatic_handling`：Agent Kernel 注册的 personal-operations provider 可用且自动处理开关开启；
- `codex_primary`：`codex-primary` provider 健康、CLI 路径存在且不是旧 OpenCodex 路径；
- `docker_exited_containers`、`docker_build_cache`：退出容器和 build cache 大小；
  只有现有游戏会话 owner 提供新鲜状态、明确的游戏进程名或 PID，实时进程探针命中，
  且状态明确声明 `DockerExpectedDown=true` 时，Docker Desktop/容器被主动停止才属于
  预期状态，记录为 `ok` 并标注 `suppressed_by_game_mode=true`；状态缺失、过期、
  没有进程身份或没有 Docker 预期声明时不得抑制告警。未配置游戏会话 owner 时，个人
  Windows 环境可使用严格的 Steam bridge：只读取 Steam libraryfolders/appmanifest 元数据，
  将已安装游戏目录缓存 60 秒，并要求前台窗口所属进程路径位于已安装游戏目录内，且
  Docker daemon 不可用、Docker Desktop 进程已退出；Steam 客户端、WebHelper、下载器和
  未知进程不满足条件。
- `v2rayn_path`、`v2rayn_status`：实际 `v2rayN.exe` 路径与运行状态漂移；
- Windows 个人环境只做上述运维可靠性事实采集；不主动检查 Defender/第三方防护、
  监听端口/防火墙、递归目录权限、云端基线或 CVE。这些安全基线不属于本 profile 的
  日常巡检闭环。

## 指标和建议告警

核心指标：

```text
control_plane_environment_check{check,status,severity,automation,configured}
control_plane_environment_last_check_timestamp_seconds
control_plane_environment_probe_errors
control_plane_recoverability_status
control_plane_synchronization_status
control_plane_known_garbage_paths
control_plane_automatic_handling_status
control_plane_ready_provider_mismatch{provider}
control_plane_ready_status
control_plane_synchronization_repository_status{path,project}
control_plane_docker_exited_containers
control_plane_docker_build_cache_bytes
```

`status` 值为 `ok`、`problem`、`unknown`，对应数值为 0、1、2；建议告警规则使用
`status=~"problem|unknown"`。同步规则应按 `path,project` 维度使用
`control_plane_synchronization_repository_status`，避免只看 aggregate 值。建议的告警名如下：

修复 Work 指标的状态语义与 Agent Kernel 的 canonical Work status 保持一致：
`personal-incident-repair` 和历史兼容用的 `personal-incident-repair-blocked` 都纳入统计；
新告警的 failed/invalid diagnosis 不得创建后者；
`control_plane_repairs_active` 只统计 `status="running"` 的修复；`open`、`ready` 和
`waiting` 属于等待处理，统一进入 `control_plane_repairs_total{status="waiting"}` 和
`control_plane_repairs_recoverable`。因此“进行中”不再把尚未执行的 Work 算入其中。

| 检查 | 告警名 |
| --- | --- |
| 可恢复性 | `ControlPlaneRecoverabilityDegraded` |
| 同步状态 | `ControlPlaneSynchronizationDegraded` |
| 已知垃圾 | `ControlPlaneGarbageDetected` |
| 自动处理能力 | `ControlPlaneAutomaticHandlingUnavailable` |
| codex_primary | `ControlPlaneCodexUnavailable` / `ControlPlaneCodexLegacyPath` |
| readiness/provider 一致性 | `ControlPlaneReadyProviderMismatch` |
| Docker | `DockerExitedContainers` / `DockerBuildCacheAccumulating` |
| v2rayN | `V2rayNPathDrift` / `V2rayNStatusDrift` |

所有环境告警在 control-plane 中都先进入 Codex 判断，不按检查名直接得出运行时安全结论；
旧 alert-name 只作为历史提示。`ControlPlaneReadinessDegraded` 和
`ControlPlaneReadyProviderMismatch` 已由 observability 的 `rules/control-plane.yml` 使用
上述同源指标进行告警。游戏模式下，若 Prometheus/Alertmanager 与
`personal-monitoring` 同时因容器停止而不可用，且上述游戏会话条件全部满足时，`/ready` 保留
`expected_degraded` 事实但返回正常状态；这不是运行时异常。指标告警只负责通知和形成
waiting Work，不代表修复已经授权或成功。当前没有配置可用的通用游戏会话 owner 时，
`/ready` 不会进入该 expected-degraded 分支。

## 自动处理边界

每一条未被抑制的 firing Alertmanager 告警都会先经 Agent Kernel 的 Work/Run/Revision
路径请求一次 diagnosis Codex；请求、结果完成和有效 diagnosis 分开记录。只有
`reason.generate` 返回成功且结果中恰好有一个 `SAFETY_CLASS=REVERSIBLE`、
`SAFETY_CLASS=IRREVERSIBLE` 或 `SAFETY_CLASS=UNKNOWN` 标记时，才算有效 diagnosis。
显式 `UNKNOWN` 是一次成功认知后的只读判断；provider failure、timeout、unavailable、
缺少标记或多个标记都是无效 diagnosis，不得折叠为 `UNKNOWN`，也不得形成 repair closure
或执行 effect。provider failure/timeout/unavailable/无结果直接进入 WAIT；成功 provider
返回 malformed 标记时最多只重试一次 diagnosis，之后进入 WAIT 并升级 Feishu，
`execution_attempts` 保持为 0。

有效 diagnosis 若判断操作不可逆，或发现目标仓库不干净，立即发送 Feishu 并禁止自动 effect。
否则继续一次 execution Codex；验证仍未完成时再进行第二次 diagnosis 和第二次 execution。
单个 firing 告警最多两次 diagnosis、两次 execution；`UNKNOWN` 不开放 effect，但仍可通过
只读 execution/verification 完成判断，不会绕过该两轮闭环。

不再按 alert name 维护第二套自动修复 allowlist：所有未被抑制的 firing 告警都进入两轮
Codex 闭环；只有告警携带已配置的 `project`、个人自动处理开关开启且目标目录通过
repo/project allowlist 时，才会把目标 repo 交给可逆 effect 路径。同步自动路径由 diagnosis
选择并填充精确的 `git.fast_forward`、`git.push_exact_ref` 或 `chezmoi.apply`；provider
会重新核对仓库、分支、remote、旧/新 SHA、祖先关系、owner 和 worktree。

所有非确定性判断和执行计划都必须通过 Codex；provider 只执行代码中固定、可验证的确定性
动作。允许的自动 effect 包括配置 allowlist 内的本地仓库修改、精确 Git 同步、
`chezmoi.apply`、配置 Compose effect，以及 `maintenance.cleanup_known_garbage`。已知垃圾
仍只把 `known_garbage_paths` 中的精确路径移入 `garbage_quarantine_dir`，下一轮探针验证
源路径已消失；路径校验失败、移动失败或验证仍失败时，不扩大范围而进入下一轮或告警。

状态库恢复规则：历史 `personal-incident-repair-blocked` 只能处于等待，不得以 `running` 表示；
它不是当前失败 diagnosis 的合法 Work 类型；
绑定认知失败证据所创建的临时 Run 必须收束为 `interrupted`。启动时会按
900 秒清理失效的 execution claim，使未完成的 repair 状态回到可恢复的等待路径；这只是
启动恢复清理，不是 episode deadline，也不限制两轮累计时长。

本 profile 不主动巡检或自动处置 Defender/第三方防护、端口/防火墙、递归目录权限、云端
基线和 CVE。任何未被确定性 capability 覆盖的外部、凭据或不可逆动作都必须停在
diagnosis/Feishu 路径；人工升级信息应包含检查名、最近一次探针时间、原始 detail、
受影响主机/路径和拟执行动作的回滚方案。升级事件中的
`notification_provider_accepted` 只表示通知 provider 接受了请求；本地脚本没有 Feishu
receipt 时，`notification_delivery_confirmed` 必须保持为 `false`，不能把 provider 成功
写成最终送达。
