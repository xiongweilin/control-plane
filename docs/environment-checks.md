# 环境巡检闭环

`control-plane` 通过 `environment-inspection` provider 执行只读巡检。巡检结果在
`/metrics` 暴露，Alertmanager 仍通过现有 `/v1/alerts/alertmanager` 入口送入 Agent
Kernel。provider 的 `available` 表示“探针本身可用”，不表示被检查对象健康；对象健康
由 `control_plane_environment_check` 的 `status` 标签表达。

`automation` 标签表示处置路由意图，不是当前告警的安全性结论：`none` 表示无需处置，
`codex-judgment` 表示 problem/unknown 必须先进入当前 Codex 安全性判断，`automatic`
只表示精确的 known-garbage 路径可以作为候选 effect。只有 Codex 输出
`SAFETY_CLASS=IRREVERSIBLE` 时，告警结果才可命名为 fail-safe；`UNKNOWN` 仍禁止 effect，
但不等同于 fail-safe。

## 告警接收与恢复

control-plane 监听 `0.0.0.0:18083`，本机守护脚本仍使用
`http://127.0.0.1:18083/live`；Prometheus 和 Alertmanager 通过 Docker Desktop 的
`host.docker.internal:18083` 访问。Windows 防火墙由安装脚本幂等维护为 TCP、入站、
`RemoteAddress=LocalSubnet`、`Profile=Any`、禁止边界穿透。不要把 Docker Desktop 的
动态网关地址写入配置，也不要把 Docker Compose 改成 host network。

Alertmanager webhook 只做认证、字段清洗、指纹去重和 durable enqueue，然后立即返回
2xx；Codex 诊断、Agent Kernel Work/Run 闭环、效果执行和 Prometheus 验证由后台有界
dispatcher 完成。`policy.max_concurrent` 限制并发修复数，
`policy.per_repair_timeout_seconds` 限制同一 Alertmanager fingerprint 活动周期的累计
repair 时长；一次 firing 周期只能创建一个 controller/Work。Codex provider 返回失败或
超时时，重试只能在原 Work 的有界认知循环内进行，webhook 重发不得创建第二个
controller/Work；首次 enqueue 的 deadline 持久化并跨进程重启复用。只有收到 resolved
后再次 firing，才开始新的告警周期。接收、完成、解析状态写入 Agent Kernel 事件存储，
进程重启时恢复同一 Work；deadline 到达后停止新的 Codex 调用并等待明确命令。

Codex provider timeout is a transient processing failure for the current
attempt: it never proves `IRREVERSIBLE`, never creates a `fail-safe` judgment,
and does not authorize a new controller/Work for the same firing fingerprint.
The original Work keeps the bounded attempt budget and the persisted repair
deadline; after that deadline the alert waits for an owner command. The Codex
CLI session budget is intentionally longer than a single HTTP round-trip
because one diagnosis may contain multiple model turns.

自动 effect 仍必须同时满足 `environment.automatic_handling_enabled` 和告警 allowlist；
这两个条件只决定候选 effect 范围，不直接决定告警是否 fail-safe。每一条告警都必须
先调用 Codex 对当前证据作可逆性判断；只有 Codex 明确输出 `SAFETY_CLASS=IRREVERSIBLE`
时才转为 fail-safe。`SAFETY_CLASS=UNKNOWN` 不命名为 fail-safe，但同样禁止所有 effect，
直到后续判断明确证明可逆。Prometheus 未提供 project 映射的告警不会凭 job/instance
猜测项目或执行 Compose effect。

`/ready` 与 `/metrics` 共用短时 kernel health cache，避免 Prometheus 抓取并发触发
重复 provider 探针；环境巡检的 Git、PowerShell 和 SSH 子进程使用 control-plane 自己
的有界终止路径，超时后不保留失控子进程。`/live` 只证明 HTTP 进程存活，不等待 Codex、
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
- `windows_defender`、`third_party_protection`：WinDefend 状态和第三方防护责任可证明性；
- `ditto_listener`、`smb_rpc_listeners`：Ditto `0.0.0.0:23443` 以及 SMB/RPC 监听；若现有入站 Block 规则明确覆盖监听，则记录防火墙证据并不将监听本身判为暴露；
- `windows_recursive_scan`：递归扫描 access errors，错误不会被当作扫描成功；
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
- `cloud_protected_root`、`cloud_tailscale_profile`、`cloud_swap`、`cloud_selinux`、
  `cloud_cve`：可选的只读 SSH 云端探针。

云端探针只使用 `ssh -o BatchMode=yes` 和生成的只读命令：保护目录检查包含根节点本身，
Tailscale 检查只读 profile 是否存在，swap/SELinux/CVE 只读取状态。未配置
`cloud_ssh_target` 时，云端检查标记 `configured="false"`，不会产生 SSH 流量；需要
云端覆盖时，应同时填写 `cloud_protected_root` 和 `cloud_tailscale_profile`。

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
control_plane_docker_exited_containers
control_plane_docker_build_cache_bytes
control_plane_windows_recursive_scan_access_errors
```

`status` 值为 `ok`、`problem`、`unknown`，对应数值为 0、1、2；建议告警规则使用
`status=~"problem|unknown"`，并对云端检查加 `configured="true"`。建议的告警名如下：

修复 Work 指标的状态语义与 Agent Kernel 的 canonical Work status 保持一致：
`personal-incident-repair` 和 `personal-incident-repair-blocked` 都纳入统计；
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
| WinDefend/第三方责任 | `WinDefendStopped` / `ThirdPartyProtectionUnknown` |
| Ditto/SMB/RPC | `DittoExposed` / `SMBOrRPCListenerDetected` |
| Windows 递归扫描 | `WindowsRecursiveScanAccessErrors` |
| Docker | `DockerExitedContainers` / `DockerBuildCacheAccumulating` |
| v2rayN | `V2rayNPathDrift` / `V2rayNStatusDrift` |
| 云端保护/Tailscale | `ProtectedDirectoryRootVerificationMissing` / `CloudTailscaleProfileMissing` |
| 云端基线/CVE | `CloudSwapMissing` / `CloudSELinuxPermissive` / `CloudCVEDetected` |

可恢复性、同步状态、自动处理能力告警在 control-plane 中属于需要 Codex 安全复核的告警，
不是按检查名直接得出的运行时 fail-safe 判定；旧 alert-name 只作为历史提示。已知垃圾告警允许进入唯一的受限自动候选路径。`ControlPlaneReadinessDegraded` 和
`ControlPlaneReadyProviderMismatch` 已由 observability 的 `rules/control-plane.yml` 使用
上述同源指标进行告警。游戏模式下，若 Prometheus/Alertmanager 与
`personal-monitoring` 同时因容器停止而不可用，且上述游戏会话条件全部满足时，`/ready` 保留
`expected_degraded` 事实但返回正常状态；这不是运行时异常。指标告警只负责通知和形成
waiting Work，不代表修复已经授权或成功。当前没有配置可用的通用游戏会话 owner 时，
`/ready` 不会进入该 expected-degraded 分支。

## 自动处理边界

每一条 Alertmanager 告警都会先经 Agent Kernel 的 Work/Run/Revision 路径调用 Codex
作当前安全性判断，诊断结果第一行必须是 `SAFETY_CLASS=REVERSIBLE`、
`SAFETY_CLASS=IRREVERSIBLE` 或 `SAFETY_CLASS=UNKNOWN`。只有明确的 `REVERSIBLE` 才能
进入配置 allowlist 允许的候选 effect；`IRREVERSIBLE` 才会标记为 fail-safe；`UNKNOWN`
保持待安全审查状态并禁止文件、Git、Docker、云端或凭据 effect。系统仍会保留告警事实
并进入 waiting Work；需要执行危险或无法证明可逆的动作时，必须由 owner 发送现有的
`/task <controller_id> <明确命令>`，之后继续经过 Agent Kernel 的 closure、WorkProposal、
责任准入和 revision 路径。

当前自动修复 allowlist 仅保留既有的 `ContainerRestartStorm`、
`PrometheusScrapeFailed`、`ControlPlaneStaleReady`。自动路径仍受现有 repo/project
allowlist 和 effect rule 约束。

所有非确定性判断和执行计划都必须通过 Codex；provider 只执行代码中固定、可验证的确定性
动作。唯一允许的自动 effect 是 `maintenance.cleanup_known_garbage`：由 Agent Kernel 建立 Work、
记录 effect、把 `known_garbage_paths` 中存在的精确路径移入 `garbage_quarantine_dir`，
下一轮探针验证源路径已消失；隔离目录可用于回滚。路径校验失败、移动失败或验证仍失败时，
Work 进入失败/等待，不扩大范围。

状态库恢复规则：`personal-incident-repair-blocked` 只能处于等待，不得以 `running` 表示；
绑定认知失败证据所创建的临时 Run 必须收束为 `interrupted`。启动时会按
`policy.per_repair_timeout_seconds` 将没有完成证明且超时的 repair Work 从 `running` 收束为
`waiting`，只修正执行占用状态，不伪造 `completed`。

以下动作明确禁止自动执行：启用或切换杀毒、修改 Windows 防火墙、关闭 Ditto/SMB/RPC、
删除 Docker 容器/卷/镜像/build cache、移动或替换 v2rayN、生成/复制/删除 Tailscale
或其他云端凭据、配置 swap、切换 SELinux、安装补丁或修复 CVE。人工升级信息必须包含
检查名、最近一次探针时间、原始 detail、受影响主机/路径和拟执行动作的回滚方案。
