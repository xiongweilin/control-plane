# ADR-0010：OpenCodex 网络边界与模型来源

- 状态：已接受（2026-08-09，批次 5）
- 关联：ADR-0003（超时分类）、ADR-0005（审计与脱敏）

## 背景

控制平面经本机 OpenCodex 代理（OpenCode Go）路由模型。此前：

1. Codex CLI 可执行路径硬编码 npm 包 vendor 内部目录
   （`.../codex-win32-x64/vendor/<target>/bin/codex.exe`），包升级/结构
   变化即失效；本机实际安装经 Scoop shim / npm 全局 shim 暴露。
2. `opencodex_base_url` 无边界约束：若指向非 loopback 端点，凭据与流量
   边界依赖隐式假设。
3. OpenCodex 错误正文直接 `response.text[:500]` 进入异常，可能携带
   敏感值；Responses API 的 refusal / incomplete / 未知输出类型未显式
   处理。

## 决策

### 1. Codex CLI 解析优先级

`resolve_codex_cli()`：显式 `[agent] codex_cli` > PATH 上的 `codex`
（Scoop shim 或 npm 全局 shim）> npm 包内 `codex.exe`（结构容忍查找）
> 裸 `codex`。会话前 `--version` 预检，缺失/失败以
`CodexCliUnavailableError` 拒绝；版本变化持久化并告警。

### 2. Loopback 默认，非 loopback 需显式鉴权

`opencodex_base_url` 默认 `http://127.0.0.1:10100/v1`（loopback）。
指向非 loopback 主机时必须显式提供 `opencodex_api_key`
（环境变量 `CONTROL_PLANE_OPENCODEX_API_KEY` 或
`[agent] opencodex_api_key`），否则启动即 `ConfigurationError`。
每个请求携带 `X-Request-Id` 便于追踪。

### 3. 错误正文脱敏

OpenCodex 错误正文先字段级脱敏（敏感键）再做行内密钥值扫描
（`api_key`/`token`/`secret`/`password`/`authorization`/`bearer` 后跟
长值），之后才进入异常或日志。

### 4. 模型来源三源连通性

三来源：Codex CLI（`--version`）、OpenCodex 代理（`GET /models`）、
默认模型（`config.model` 在清单中）。`check_model_sources()` 提供最小
连通性回归；`check_model_drift()` 只读漂移检查（基线 `models:baseline`）；
`startup_model_preflight()` 启动预检（`model_preflight_enabled` 默认开，
不阻断服务）。

## 后果

- 正面：升级 npm 包不再破坏 CLI 解析；非 loopback 端点必须显式声明
  凭据；敏感值不会原样进入异常/日志；模型层故障在启动与运行中可观测。
- 代价：非 loopback OpenCodex 部署需额外配置
  `opencodex_api_key`；新增 3 个指标与 1 个预检通知。
- 兼容：默认 loopback 配置无需任何改动。
