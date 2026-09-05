# 环境巡检闭环

`control-plane` 通过 `environment-inspection` provider 执行只读巡检。巡检结果在
`/metrics` 暴露，Alertmanager 仍通过现有 `/v1/alerts/alertmanager` 入口送入 Agent
Kernel。provider 的 `available` 表示“探针本身可用”，不表示被检查对象健康；对象健康
由 `control_plane_environment_check` 的 `status` 标签表达。

## 覆盖范围

当前检查项为：

- `codex_primary`：`codex-primary` provider 健康、CLI 路径存在且不是旧 OpenCodex 路径；
- `windows_defender`、`third_party_protection`：WinDefend 状态和第三方防护责任可证明性；
- `ditto_listener`、`smb_rpc_listeners`：Ditto `0.0.0.0:23443` 以及 SMB/RPC 监听；
- `windows_recursive_scan`：递归扫描 access errors，错误不会被当作扫描成功；
- `docker_exited_containers`、`docker_build_cache`：退出容器和 build cache 大小；
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
control_plane_ready_provider_mismatch{provider}
control_plane_ready_status
control_plane_docker_exited_containers
control_plane_docker_build_cache_bytes
control_plane_windows_recursive_scan_access_errors
```

`status` 值为 `ok`、`problem`、`unknown`，对应数值为 0、1、2；建议告警规则使用
`status=~"problem|unknown"`，并对云端检查加 `configured="true"`。建议的告警名如下：

| 检查 | 告警名 |
| --- | --- |
| codex_primary | `ControlPlaneCodexUnavailable` / `ControlPlaneCodexLegacyPath` |
| readiness/provider 一致性 | `ControlPlaneReadyProviderMismatch` |
| WinDefend/第三方责任 | `WinDefendStopped` / `ThirdPartyProtectionUnknown` |
| Ditto/SMB/RPC | `DittoExposed` / `SMBOrRPCListenerDetected` |
| Windows 递归扫描 | `WindowsRecursiveScanAccessErrors` |
| Docker | `DockerExitedContainers` / `DockerBuildCacheAccumulating` |
| v2rayN | `V2rayNPathDrift` / `V2rayNStatusDrift` |
| 云端保护/Tailscale | `ProtectedDirectoryRootVerificationMissing` / `CloudTailscaleProfileMissing` |
| 云端基线/CVE | `CloudSwapMissing` / `CloudSELinuxPermissive` / `CloudCVEDetected` |

这些告警名在 control-plane 中均属于 fail-safe 集合；`ControlPlaneReadinessDegraded` 和
`ControlPlaneReadyProviderMismatch` 已由 observability 的 `rules/control-plane.yml` 使用
上述同源指标进行告警。指标告警只负责通知和形成 waiting Work，不代表修复已经授权或成功。

## 自动处理边界

环境告警和任何不在 `monitoring.auto_repair_alertnames` allowlist 内的 Alertmanager
告警，都会创建一个 `personal-incident-repair-blocked` waiting Work，且
`requested_capabilities=[]`。系统会保留原始告警事实并通知人工；继续处理必须由 owner
发送现有的 `/task <controller_id> <明确命令>`，之后仍经过 Agent Kernel 的 closure、
WorkProposal、责任准入和 revision 路径。

当前自动修复 allowlist 仅保留既有的 `ContainerRestartStorm`、
`PrometheusScrapeFailed`、`ControlPlaneStaleReady`。自动路径仍受现有 repo/project
allowlist 和 effect rule 约束。

以下动作明确禁止自动执行：启用或切换杀毒、修改 Windows 防火墙、关闭 Ditto/SMB/RPC、
删除 Docker 容器/卷/镜像/build cache、移动或替换 v2rayN、生成/复制/删除 Tailscale
或其他云端凭据、配置 swap、切换 SELinux、安装补丁或修复 CVE。人工升级信息必须包含
检查名、最近一次探针时间、原始 detail、受影响主机/路径和拟执行动作的回滚方案。
