# Portable Runtime refactor progress

## Completed in this change (2026-08-20 B1)

### B0 已完成（基线）
- Frozen current inventory, dependencies, workflows, storage and baseline.
- Added provider-independent canonical models for Work/Run/Artifact/Evidence/Decision/Action/Outcome/KnowledgeItem/Event.
- Added CapabilityRequest/CapabilityResult, ProviderDescriptor and health contracts.
- Added dynamic ProviderRegistry and deterministic priority routing.
- Added in-memory and SQLite state stores with ID-preserving export/import.
- Added filesystem content-addressed artifact store.
- Added stdio JSONL manifest/message models and one-shot provider adapter.
- Added provider/trigger/workflow templates, echo example and conformance boundary checker.
- Added minimal stable HTTP and CLI surfaces under `portable_runtime`.
- Added a data-only legacy repair adapter and portable-local deployment factory.

### B1 已完成（Provider包装层，3并行已落地）
- **ProcessExecutor 抽象**：`src/portable_runtime/core/process.py` 定义 ProcessSpec/ProcessResult + Protocol + PortableSubprocessExecutor/WindowsProcessExecutor/PosixProcessExecutor；Core 不再直接调用 taskkill/pwsh/bash，Provider 通过该抽象执行。
- **CodexProvider**：`src/portable_runtime/providers/codex/` 包装 `codex exec --model`，capabilities = reason.generate/code.read/code.edit/code.test/shell.exec/git.diff；health() 探测 `codex --version` + gateway；invoke() 通过 ProcessExecutor 运行并落盘 transcript；支持 `[[providers]]` 配置覆盖 `[agent] model`，保留 compat 映射；接口 `create_codex_provider_from_toml()`。
- **Verifier Providers**：`src/portable_runtime/providers/verifiers/` 拆出 6+1 能力：verify.http / verify.promql / verify.container / verify.logs / verify.git / verify.tests / verify.git_diff；统一返回 CapabilityResult(supported/contested)，Legacy Verifier 可改为 facade 调用 CapabilityService；provider 指标与 runtime 指标分离。
- **Policy / Knowledge / Paths 深化**：`core/policies.py` 落地 PolicyDecision/PolicyContext + AllowAll/SensitivePath/ExternalSideEffect/CandidateMerge 四策略；`core/knowledge.py` 增加 promote/deprecate/archive；`core/paths.py` 统一 RuntimePaths(data/cache/logs/plugins/artifacts) 消除 D:\ 硬编码。
- **校验**：`ruff check .` 通过，`mypy src/portable_runtime` 通过（63 files），`check_portable_core_imports.py` 通过，`pytest` 199 passed 保持。

## 批次设计（详见 docs/refactor/execution-batches.md）

| 批次 | 名称 | 并行度 | 状态 | 剩余工作 |
|------|------|--------|------|----------|
| B0 | 冻结+地基 | - | ✅ | - |
| **B1** | Provider包装层 | 3并行 | ✅ 已完成 | - |
| B2 | 工作流化与触发 | 3并行 | 🟡 骨架已落地，待补测试 | 双写 Work/Run、IncidentRepairWorkflow/GenericTaskWorkflow、Trigger 3件套 的 parity tests + 双写真实联调 |
| B3 | 交互与存储闭环 | 3并行 | 🟡 骨架已落地 | Feishu 人审/通知真实打通、Store 抽象收口（Fake 驱动 core tests）、State Export/Import 含 artifacts/路径清洗 |
| B4 | 插件与部署解耦 | 2并行 | ⏳ 待启动 | PluginManager 生命周期8态、stdio 硬化、部署 Profile 拆分（windows-personal-platform vs portable-local + Dockerfile） |
| B5 | 最终收口 | 串行 | ⏳ 待启动 | 文档、陌生助手验收（Uppercase/ReviewWorkflow）、完整替换测试 §64、DoD 38项核验 |

## B2/B3 已落地的骨架（本批次预埋，为下批并行做准备）

- `workflows/context.py` + `workflows/incident_repair` + `generic_task` + `daily_scan` + `knowledge_consolidation`
- `triggers/base.py` + `triggers/alertmanager` + `schedule` + `webhook`
- `interactions/feishu/provider.py` (FeishuTrigger + FeishuHumanProvider + FeishuNotificationProvider)
- 均通过 ruff/mypy，未改变 legacy control_plane 行为（additive）。

## Deliberately deferred（仍按计划延后）

- 双写真实联调与旧 repair 表迁移脚本（需 B2 完整 parity tests）
- Feishu 真实 API 与 legacy `/cp` 兼容层全量
- PluginManager install 守护进程与 alternate DB 后端
- 部署解耦的 Windows VBS/Task Scheduler 迁移与 Dockerfile
- 最终 DoD 完整替换测试与跨平台 CI

下一步按 B2→B3→B4→B5 顺序，每批批内3并行、批间串行，每批结束执行 ruff/mypy/pytest 门禁，详见 execution-batches.md。
