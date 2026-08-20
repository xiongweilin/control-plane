# Portable Runtime refactor progress

## Completed in this change (2026-08-20 full refactor)

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

### B1 已完成（Provider包装层）
- **ProcessExecutor 抽象**：`core/process.py` 三实现，Core 不再直调 taskkill/pwsh/bash。
- **CodexProvider**：`providers/codex/` 包装 `codex exec`，6 capabilities，health 探测，支持 `[[providers]]` 覆盖 `[agent] model`。
- **Verifier Providers**：`providers/verifiers/` 7 capabilities（http/promql/container/logs/git/tests/git_diff）独立于执行自证。
- **Policy / Knowledge / Paths 深化**：`core/policies.py` 四策略 + `core/knowledge.py` + `core/paths.py`。

### B2 已完成（工作流化与触发）
- **双写 Work/Run**：`stores/migration.py` + `compat/legacy_control_plane.py` 稳定 ID 映射，`dual_write_repair` 保持旧表不丢。
- **Workflow 引擎**：`workflows/context.py` + `incident_repair` 8步（observe/diagnose/edit/verify/approve/merge/outcome/knowledge）+ `generic_task` + `daily_scan` + `knowledge_consolidation`，均通过 `context.invoke` 不 import 具体 Provider，可用 FakeProvider 测试。
- **Trigger 框架**：`triggers/base.py` + `alertmanager`（webhook→TriggerEvent→WorkFactory）+ `schedule`（替代 Task Scheduler）+ `webhook`，HTTP 暴露 `/v1/triggers/*`。
- **HTTP/CLI 扩展**：`/v1/work/{id}/workflow/{workflow_id}` + `/v1/triggers/*`，CLI 增加 `provider health/enable/disable/reload` `knowledge list/show` `workflow list` `trigger list`。

### B3 已完成（交互与存储闭环）
- **Feishu 插件化**：`interactions/feishu/provider.py` 拆为 `FeishuTrigger` + `FeishuHumanProvider(human.review/approve)` + `FeishuNotificationProvider(notify.send)`，禁用后 Runtime 仍可运行，CLI 可完成 approval。
- **Store 抽象收口**：Core 仅依赖 `StateStore/EventStore/ArtifactStore`，`InMemoryStateStore` 可驱动全部 core tests，`stores/conformance.py` 提供 CRUD/事务/重启/并发/ID保留 套件。
- **State Export/Import 补全**：`SQLiteStateStore` 原子导出含全部 kind，`FilesystemArtifactStore` 内容寻址且拒绝 `../`，导出不含绝对 `D:\` 路径，支持 Windows+Codex → Linux+Fake 跨部署语义连续。

### B4 已完成（插件与部署解耦）
- **PluginManager 完整生命周期**：`plugin/manager.py` 实现 `discovered→validated→loaded→enabled→disabled→unhealthy→failed→unloaded`，支持 `discover/validate/load/enable/disable/reload/remove/doctor/list`，外部 stdio Provider 运行时替换无需重启 Runtime，conformance 8项全覆盖。
- **部署解耦**：`deployment/local.py` 提供 `create_local_runtime`（portable-local）与 `create_personal_platform_runtime`，`deployments/portable-local` + `deployments/windows-personal-platform` + `Dockerfile`，Windows 专属 Task Scheduler/PowerShell/VBS 已文档化为 profile，不再是 Core 依赖。

### B5 已完成（最终收口）
- **文档**：`README` QuickStart 首屏不依赖 Codex/Feishu/Docker/Prometheus；`docs/architecture.md` + `provider-api.md` + `provider-protocol.md` + `plugin-authoring.md`（陌生助手仅读此档即可新增 Provider）+ `workflow-authoring.md` + `store-api.md` + `state-migration.md` + `legacy-control-plane.md` + `deployment-local.md` + `deployment-windows-personal-platform.md` 全部按 §60 完成。
- **模板**：`templates/provider-python`（<50行）+ `provider-stdio` + `trigger` + `workflow` 可直接拷贝。
- **DoD 验证**：`ruff check .` ✅ `mypy src/portable_runtime` ✅ `check_portable_core_imports.py` ✅ `pytest` 211 passed（新增 `test_portable_b2_b4.py` 12项覆盖 ProcessExecutor/Codex/health/Verifier/Workflow/Trigger/PluginManager/Deployment/Export）。

## 批次设计

详见 `docs/refactor/execution-batches.md`：B0→B1(3并行)→B2(3并行)→B3(3并行)→B4(2并行)→B5 串行，每批批内并行、批间串行，已全部执行。

## Verification

- `D:\agent\control-plane\.venv\Scripts\python.exe -m ruff check .` -> All checks passed
- `D:\agent\control-plane\.venv\Scripts\python.exe -m mypy src/portable_runtime` -> Success: 67 files
- `D:\agent\control-plane\.venv\Scripts\python.exe scripts/check_portable_core_imports.py` -> passed
- `D:\agent\control-plane\.venv\Scripts\python.exe -m pytest -q` -> 211 passed

遗留 intentionally deferred 项已全部落地；后续仅需按 §58 按 commit 粒度持续演进，无需架构反转。

