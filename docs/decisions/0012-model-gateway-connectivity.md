# ADR-0012：模型网关连通性诊断（OpenCodex 退役后）

- 状态：已接受（2026-08-11）
- 关联：ADR-0010（OpenCodex 网络边界与模型来源，被部分超驰）、ADR-0005（审计与脱敏）

## 背景

OpenCodex 于 2026-08-11 退役卸载，本机模型路由切换为
`4000（codex-zstd-proxy）→ 4001（LiteLLM）→ opencode-go/*`（事实源：
[[运维笔记/模型路由]]）。控制平面的模型来源诊断仍探测已退役的
`opencodex_base_url`（默认 `127.0.0.1:10100/v1`），导致每次启动预检
误报 "OpenCodex 代理不可达"，且措辞沿用旧名。

## 决策

1. 诊断目标改为本机模型网关：配置字段 `opencodex_base_url` →
   `gateway_base_url`（默认 `http://127.0.0.1:4001/v1`），
   `opencodex_timeout_seconds` → `gateway_timeout_seconds`；
   客户端 `OpenCodexClient` → `GatewayClient`（文件 `opencodex.py` →
   `gateway.py`）。
2. 网络边界收紧为 **loopback-only**：`_validate_gateway_network` 对非
   loopback 地址直接 `ConfigurationError`，不再保留
   `opencodex_api_key` 的远端鉴权路径（本机网关诊断无远端形态）。
3. 三源连通性改为 CLI / 模型网关（`GET /v1/models`）/ 默认模型，
   指标 `control_plane_model_connectivity{source}` 的标签
   `opencodex` → `gateway`；预检与漂移通知措辞同步为
   "LiteLLM 网关"。
4. 旧配置字段不再读取；迁移期用户直接改用新字段（本地
   `control_plane.toml` 与示例已同步更新）。

## 后果

- 正面：启动预检回到有效探测（4001 可达、默认模型在清单时静默通过），
  措辞与当前模型层一致；移除不再需要的远端鉴权路径。
- 代价：指向远端模型的非 loopback 诊断不再受支持（本机网关本就是
  loopback 设计）。
- 兼容：`codex exec` 的 Agent 执行路由不受影响（走 Codex 当前配置
  4000/4001），诊断探针仍不决定执行入口。
