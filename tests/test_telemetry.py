from __future__ import annotations

from fastapi import FastAPI

from control_plane import telemetry


def test_configure_telemetry_is_noop_without_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_INSTRUMENTED", False)

    app = FastAPI()

    assert telemetry.configure_telemetry(app) is False


def test_configure_telemetry_instruments_once(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "control-plane-test")
    monkeypatch.setattr(telemetry, "_INSTRUMENTED", False)

    app = FastAPI()

    assert telemetry.configure_telemetry(app) is True
    # A second call must stay idempotent instead of stacking another provider.
    assert telemetry.configure_telemetry(app) is True
