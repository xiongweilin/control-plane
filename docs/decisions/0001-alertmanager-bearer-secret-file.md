# ADR-001: Alertmanager 使用 Bearer header 与 secret 文件调用控制平面

## Status

Accepted

## Date

2026-08-09

## Context

Alertmanager 曾把控制平面共享密钥放在 webhook URL 的查询参数中。URL 会进入配置、诊断输出和潜在访问日志，扩大凭据暴露面；Alertmanager 与控制平面之间只需要一个最小共享认证值，不需要把它作为可见配置字段传播。

## Decision

- Alertmanager webhook URL 不携带密钥。
- 共享密钥保存在 Docker 命名 secret 卷中，Alertmanager 以只读方式挂载。
- Alertmanager 使用官方 `http_config.authorization.credentials_file`，发送 `Authorization: Bearer <credential>`。
- 控制平面的告警入口接受 Bearer 或既有 `X-Control-Plane-Key`，不再接受查询参数密钥；其他控制 API 保持 `X-Control-Plane-Key` 契约。
- 密钥值不得进入仓库、文档、日志、命令行参数或验证输出。

## Alternatives Considered

### 保留查询参数

拒绝。实现简单，但 URL 容易被配置检查、代理和日志记录。

### 在 alertmanager.yml 中直接写 Authorization 凭据

拒绝。虽然避免查询参数，但密钥仍在普通配置文件中。

### 使用自定义 X-Control-Plane-Key 文件注入

拒绝。Alertmanager 原生支持标准 Authorization header 与 credentials file，无需自定义模板或代理。

## Consequences

- Alertmanager 容器必须挂载 `observability_alertmanager_secrets`。
- 配置检查和恢复流程必须同时提供 secret 文件。
- 丢失 secret 卷时，告警仍在 Alertmanager 中保留，但投递控制平面会返回认证失败；恢复时从既有安全存储重新装载，不从 Git 或文档恢复密钥值。
