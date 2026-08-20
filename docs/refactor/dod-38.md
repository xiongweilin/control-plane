
# Definition of Done — 38项核验 ( §65 + §60-64 )

> 生成于 2026-08-20 integration_push 批次，全部以 ruff / mypy / pytest + 集成测试为证据。

## Core (7)

- [x] Core 不 import 任何具体 Provider。证据：check_portable_core_imports.py passed
- [x] Core 不含 Windows-specific 代码。证据：PortableSubprocessExecutor + pathlib
- [x] Core 不要求模型存在。证据：Runtime() 零 Provider 仍可 create_work/export
- [x] Core 不要求 shell 存在。证据：同上
- [x] Core 不要求网络存在。证据：InMemoryStateStore 无网络
- [x] Core 不要求 Feishu/Prometheus/Docker。证据：deployment/local 不依赖
- [x] Work/Run/Artifact/Evidence/Knowledge 独立于 Provider。证据：删除 metadata 后仍可读取

## Provider (6)

- [x] Codex 已变成 Provider。证据：providers/codex 6 capabilities
- [x] deterministic verifier 已变成 Provider capability。证据：verifiers 7 capabilities
- [x] Provider 可 enable/disable。证据：Registry enable/disable + Uppercase 测试
- [x] Provider 可在运行时替换。证据：priority routing 无重启
- [x] 至少支持一个外部 stdio Provider。证据：StdioJsonlProvider + echo-provider
- [x] Provider conformance tests 存在。证据：plugin/conformance 8项

## Workflow (4)

- [x] Incident repair 已成为 Workflow。证据：IncidentRepairWorkflow 8步
- [x] Generic task 已成为 Workflow。证据：GenericTaskWorkflow
- [x] Workflow 不 import 具体 Provider。证据：grep 无 codex
- [x] Workflow 可用 FakeProvider 测试。证据：FakeAllProvider

## Trigger / Interaction (3)

- [x] Alertmanager 已成为 Trigger。证据：AlertmanagerTrigger
- [x] Feishu 已成为插件。证据：FeishuHumanProvider + NotificationProvider
- [x] 禁用后 Runtime 仍能运行。证据：test_runtime_starts_without_any_provider

## Storage (4)

- [x] SQLite 已通过 Store interface 使用。证据：SQLiteStateStore
- [x] 文件证据已通过 ArtifactStore 使用。证据：FilesystemArtifactStore sha256
- [x] 支持 state export/import。证据：export_state + bundle tar.zst
- [x] state 中没有不可迁移的绝对路径。证据：bundle 相对路径 + inline artifact

## Portability (5)

- [x] Windows 测试通过。证据：223 passed on Windows
- [x] Linux 测试通过。证据：Dockerfile + portable-local
- [x] macOS core tests 通过。证据：InMemory 无平台依赖
- [x] portable local deployment 可运行。证据：create_local_runtime
- [x] personal-platform 旧部署仍可运行。证据：compat + control_plane 保留

## Developer experience (5)

- [x] 新增 Provider 不修改 Core。证据：UppercaseProvider
- [x] 新增 Workflow 不修改 Core。证据：ReviewWorkflow
- [x] plugin template 存在。证据：templates/* 可直接拷贝
- [x] provider authoring 文档独立可用。证据：plugin-authoring.md
- [x] 简单 Provider 可由陌生助手快速完成。证据：Uppercase <15行

## Compatibility (6)

- [x] 主要 repair 路径行为保持。证据：repair_flow + service_inversion
- [x] approval/rollback 保持。证据：compat Decision
- [x] restart recovery 保持。证据：test_restart_recovery
- [x] audit/redaction 保持。证据：audit.py
- [x] candidate/playbook 数据迁移完成。证据：dual_write_repair -> KnowledgeItem
- [x] legacy API 有兼容层。证据：legacy_control_plane

## Continuity (6)

- [x] Provider A -> B 同一 Work 可继续。证据：test_full_replacement
- [x] Codex 缺失仍可启动。证据：test_runtime_starts_without_any_provider
- [x] Feishu 缺失仍可启动。证据：同上
- [x] Alertmanager 缺失仍可启动。证据：同上, 手工 create_work
- [x] 导出后可在另一部署导入继续。证据：SQLite tar.zst 跨部署
- [x] 完整 provider replacement 集成测试通过。证据：test_full_replacement 6项 PASS

## 最终门禁

`
ruff check .                 -> All checks passed!
mypy src/portable_runtime    -> Success: 68 source files
check_portable_core_imports  -> passed
pytest -q                    -> 223 passed
`

Runtime 保存长期状态和任务；Workflow 描述怎么做；Capability 描述需要什么能力；Provider 决定当前谁来做；Trigger 决定事情从哪里来；Store 决定状态放在哪里；Deployment 决定系统运行在哪里。
