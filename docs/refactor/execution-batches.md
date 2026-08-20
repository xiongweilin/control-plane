# 执行批次设计（2026-08-20）—— 全量改造剩余工作

> 依据 D:\download\control-plane-portable-runtime-refactor-plan.md §42-§67，已完成第1批（冻结现状+地基，commit 9817683），剩余按依赖分5批，可并行处用子智能体。

## 批次总览

| 批次 | 名称 | 依赖 | 可并行度 | 目标产出 | 对应计划章节 |
|------|------|------|----------|----------|--------------|
| B0 | 已完成：冻结+地基 | - | - | inventory/baseline + portable_runtime核心模型/Registry/Router/SQLite/Filesystem/Stdio/HTTP/CLI/模板 | §42-§44 |
| **B1** | **Provider包装层** | B0 | **3并行** | CodexProvider + VerifierProviders + ProcessExecutor + Policy/Knowledge深化 | §22-§24, §17, §29, §33 |
| B2 | 工作流化与触发 | B1 | **3并行** | 双写 Work/Run + IncidentRepairWorkflow/GenericTaskWorkflow + Trigger框架 | §15-16, §14, §31-32, §47-48 |
| B3 | 交互与存储闭环 | B2 | **3并行** | Feishu插件化 + Store抽象收口 + State Export/Import补全 | §18-19, §20, §49-52 |
| B4 | 插件与部署解耦 | B3 | **2并行** | PluginManager完整生命周期 + 外部Provider协议硬化 + 部署Profile拆分 | §26-27, §54-57 |
| B5 | 最终收口 | B4 | 串行 | 文档/用例/完整替换测试 + DoD核验 + README QuickStart | §60-65 |

## B1 详细（立即启动，3子智能体并行）

### B1-A：ProcessExecutor + CodexProvider（关键路径）
- 文件：src/portable_runtime/providers/codex/* , src/portable_runtime/core/process.py
- 任务：
  - 抽象 ProcessExecutor（run/terminate，PortableSubprocessExecutor / WindowsProcessExecutor / PosixProcessExecutor），搬运 control_plane/codex_runner.py 的 CLI解析、preflight、timeout、进程树清理、transcript采集、redaction
  - 实现 CodexProvider 注册 capabilities: reason.generate, code.read, code.edit, code.test, shell.exec, git.diff；health() 暴露 codex --version / gateway探针；invoke() 复用 ProcessExecutor
  - 模型配置迁移：读 [[providers]] / [providers.config] model/cli，废弃 [agent] model 全局单例，保留 compat 映射
  - 保留 legacy service → CapabilityService 的临时桥接，不改旧 repair 行为
- 验收：旧 repair_flow 测试仍绿；新 test_codex_provider.py 用 Fake/真实 codex --version 通过；core 不 import codex

### B1-B：Verifier Providers
- 文件：src/portable_runtime/providers/verifiers/*（http, promql, container, logs, git, tests）
- 任务：
  - 将 control_plane/verifier.py 拆为 6个 capability：verify.http / verify.promql / verify.container / verify.logs / verify.git / verify.tests
  - 统一通过 CapabilityProvider 调用，输出 Evidence（supported/contested），禁止 execution自证
  - LegacyVerifier facade 内部改调 CapabilityService 保持旧 API 兼容
  - 抽离 provider 专属 metrics：provider_codex_* vs runtime_*
- 验收：test_verifier 全量通过；新增 test_verifier_providers 使用 FakeProvider 通过

### B1-C：Policy / Knowledge / Store深化
- 文件：src/portable_runtime/core/policies.py, src/portable_runtime/core/knowledge.py, src/portable_runtime/stores/*
- 任务：
  - Policy 接口落地：PermissionPolicy / SensitivePathPolicy / ExternalSideEffectPolicy / CandidateMergePolicy（从 control_plane 迁移但不进 Router）
  - KnowledgeItem 服务：candidate→official→deprecated 迁移，从 candidate experience 提升
  - Store conformance 增强：为 InMemory/SQLite 增加 store-conformance 测试套件（CRUD/事务/重启/并发/ID保留/事件序）
  - RuntimePaths 统一路径（data_dir/cache_dir/log_dir/plugin_dir），消除 D:\ 硬编码
- 验收：新增 test_policy.py / test_knowledge.py / test_store_conformance.py；mypy 仍通过

## B2 详细（依赖 B1，3并行）

### B2-A：Work/Run 双写
- 文件：src/portable_runtime/stores/migration.py, src/portable_runtime/compat/legacy_control_plane.py 扩展
- 任务：Alert到来时同时写旧 repair 表 + 新 Work(id=work_legacy_{repair_id})/Run；实现 legacy_repair_id↔work/run 映射查询；提供迁移脚本和回滚
- 验收：任一新 repair 均有 Work+Run；重启恢复一致；旧 API 返回兼容

### B2-B：Workflow引擎
- 文件：src/portable_runtime/workflows/{incident_repair,generic_task,daily_scan,knowledge_consolidation}/*
- 任务：Workflow 协议补全（accepts + run + WorkflowContext.capabilities），IncidentRepairWorkflow 8步流水，GenericTaskWorkflow 通用能力路由；Workflow 不 import 具体 Provider
- 验收：同一 Workflow 可用 FakeProvider 完成测试，旧 repair 行为与 baseline 一致但不再直调 subprocess

### B2-C：Trigger框架
- 文件：src/portable_runtime/triggers/{alertmanager,webhook,schedule}/*
- 任务：TriggerSource 协议 + TriggerEvent + TriggerEmitter；AlertmanagerTrigger 仅解析 webhook→TriggerEvent；ScheduleTrigger 替代 Windows Task Scheduler；全部不能直调 Codex
- 验收：可手工创建相同 Work 并驱动 workflow，无需 Alertmanager

## B3 详细（依赖 B2，3并行）

### B3-A：Feishu插件化
- 文件：src/portable_runtime/interactions/feishu/*, src/portable_runtime/providers/human/*
- 任务：FeishuTrigger + FeishuHumanProvider(human.review/approve) + FeishuNotificationProvider(notify.send) + LegacyCommandMapper；Core 不再 import feishu；禁用后 CLI 可完成 approval
- 验收：禁用 Feishu 后 Runtime 仍能启动/列 Work；CLI approve 测试通过

### B3-B：Store抽象收口
- 文件：src/portable_runtime/stores/{sqlite,filesystem,memory} + interfaces/store.py
- 任务：Core 仅依赖 StateStore/EventStore/ArtifactStore；Fake/InMemory 可跑全部 core test；验证双写切换读取
- 验收：test_store_conformance 对 Fake/Sqlite 均绿

### B3-C：State Export/Import补全
- 文件：src/portable_runtime/core/runtime.py export/import, src/portable_runtime/deployment/*
- 任务：导出含 manifest.json/events.jsonl/works/runs/evidence/knowledge/artifacts/ 索引 + artifacts/ 目录；绝对路径清洗；支持 Windows(SqLite+Codex) → Linux(Postgres/Fake) 语义连续；CLI/API 打通
- 验收：§53 跨部署场景测试通过

## B4 详细（依赖 B3，2并行）

### B4-A：外部Provider协议硬化 + PluginManager
- 文件：src/portable_runtime/plugin/*, src/portable_runtime/providers/stdio.py, templates/*
- 任务：stdio-jsonl 增加 timeout/cancel/invalid-input 结构化错误、provider退出不杀 Runtime；PluginManager 实现 discovered→validated→loaded→enabled→disabled→unhealthy→failed→unloaded；CLI：plugin {validate,install,enable,disable,reload,remove,doctor,list}
- 验收：provider conformance 测试 8项全过；运行时 replace 无需重启

### B4-B：部署解耦
- 文件：deployments/{windows-personal-platform,portable-local}, Dockerfile, src/portable_runtime/deployment/*
- 任务：将 Task Scheduler/PowerShell/VBS/watchdog 移入 windows-profile；新增 portable-local：uv run python -m portable_runtime 即可运行；提供 Dockerfile
- 验收：portable-local 不依赖 Codex/Feishu/Docker 可启动；personal-platform 仍与当前部署一致

## B5 详细（依赖 B4，串行收口）

- 文档：README 新 QuickStart（不依赖 Codex/Feishu/Docker/Prometheus 的 Echo demo 在前，Personal Platform 在后）+ docs/{architecture,provider-api,provider-protocol,plugin-authoring,workflow-authoring,store-api,state-migration,legacy-control-plane,deployment-*.md}
- 测试：陌生助手 UppercaseProvider + ReviewWorkflow 验收；完整替换测试（RuntimeA: FakeCodex → 导出 → RuntimeB: AlternateProvider 导入继续，§64）；Portability：Windows/Linux/macOS core tests
- DoD：逐项打钩 §65 六域 38项，记录 docs/refactor/progress.md & decisions.md

## 并发控制

- 每批批内并行，批间串行；使用 3-4 并发槽（root+3子智能体）
- 每子智能体独立分支文件集，避免冲突；每批结束做 ruff/mypy/pytest 联合门禁
- 禁止一次性大重命名（§58 每 commit 单一边界）

