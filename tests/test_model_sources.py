from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from control_plane.approvals import ApprovalManager
from control_plane.budget import Budget
from control_plane.config import ControlPlaneConfig
from control_plane.service import RepairService
from control_plane.storage import Store


class FakeAgent:
    def __init__(self, version: str = "codex-cli 0.145.0", error: str = "") -> None:
        self.version = version
        self.error = error

    def cli_info(self):
        if self.error:
            from control_plane.codex_runner import CodexCliUnavailableError

            raise CodexCliUnavailableError(self.error)
        return Path("C:\\tools\\codex.exe"), self.version


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def notify(self, severity: str, title: str, text: str) -> None:
        self.calls.append((severity, title, text))


def _config(tmp_path) -> ControlPlaneConfig:
    return replace(
        ControlPlaneConfig(),
        api_key="x",
        data_dir=tmp_path / "data",
        patch_dir=tmp_path / "data" / "patches",
        evidence_dir=tmp_path / "data" / "evidence",
        state_db=tmp_path / "data" / "control-plane.db",
        notification_enabled=False,
        prometheus_url="",
        alertmanager_url="",
    )


def _service(tmp_path, agent=None, notifier=None) -> RepairService:
    store = Store(_config(tmp_path).state_db)
    cfg = _config(tmp_path)
    service = RepairService(
        cfg,
        store,
        Budget(store, 10, 8),
        agent or FakeAgent(),
        ApprovalManager(),
        notifier or FakeNotifier(),
    )
    return service


async def test_check_model_sources_all_ok(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_gateway_models",
        AsyncMock(return_value=["opencode-go/deepseek-v4-flash", "gpt-5.6-luna"]),
    )
    result = await service.check_model_sources()
    assert result["cli"]["ok"] is True
    assert result["gateway"]["ok"] is True
    assert result["gateway"]["model_count"] == 2
    assert result["model"]["ok"] is True
    await service.close()


async def test_check_model_sources_cli_missing(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, agent=FakeAgent(error="Codex CLI not runnable"))
    monkeypatch.setattr(
        service,
        "_gateway_models",
        AsyncMock(return_value=["opencode-go/deepseek-v4-flash"]),
    )
    result = await service.check_model_sources()
    assert result["cli"]["ok"] is False
    assert "not runnable" in result["cli"]["error"]
    assert result["model"]["ok"] is True
    await service.close()


async def test_check_model_sources_default_model_missing(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_gateway_models",
        AsyncMock(return_value=["gpt-5.6-luna"]),
    )
    result = await service.check_model_sources()
    assert result["model"]["ok"] is False
    assert "missing" in result["model"]["error"]
    await service.close()


async def test_check_model_sources_proxy_unreachable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_gateway_models", AsyncMock(return_value=None))
    result = await service.check_model_sources()
    assert result["gateway"]["ok"] is False
    assert result["model"]["ok"] is False
    await service.close()


async def test_check_model_drift_detects_change(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.store.set_setting("models:baseline", "a,b")
    monkeypatch.setattr(
        service,
        "_gateway_models",
        AsyncMock(return_value=["a", "b", "c"]),
    )
    result = await service.check_model_drift()
    assert result["drifted"] is True
    assert result["detail"]["added"] == ["c"]
    assert result["detail"]["removed"] == []
    assert service.store.get_setting("models:baseline") == "a,b,c"
    assert any(title == "模型网关模型清单变化" for _, title, _ in service.notifier.calls)
    await service.close()


async def test_check_model_drift_first_run_establishes_baseline(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_gateway_models",
        AsyncMock(return_value=["x", "y"]),
    )
    result = await service.check_model_drift()
    assert result["drifted"] is False
    assert service.store.get_setting("models:baseline") == "x,y"
    await service.close()


async def test_startup_preflight_reports_problems(tmp_path, monkeypatch) -> None:
    notifier = FakeNotifier()
    service = _service(tmp_path, notifier=notifier)
    monkeypatch.setattr(service, "_gateway_models", AsyncMock(return_value=None))
    result = await service.startup_model_preflight()
    assert result["ok"] is False
    assert any("不可达" in problem for problem in result["problems"])
    assert any(title == "模型启动预检未通过" for _, title, _ in notifier.calls)
    await service.close()


async def test_gateway_models_closes_client(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    closed = []

    class FakeClient:
        async def list_models(self) -> list[str]:
            return ["m1"]

        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "control_plane.gateway.GatewayClient",
        lambda *args, **kwargs: FakeClient(),
    )
    assert await service._gateway_models() == ["m1"]
    assert closed == [True]
    await service.close()
