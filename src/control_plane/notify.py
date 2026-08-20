from __future__ import annotations

import asyncio
import logging

from .config import ControlPlaneConfig
from .metrics import NOTIFY_FAILURES

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: ControlPlaneConfig) -> None:
        self.config = config

    async def notify(
        self,
        severity: str,
        title: str,
        text: str,
        url: str | None = None,
    ) -> None:
        if not self.config.notification_enabled:
            logger.info("notification disabled: %s %s", severity, title)
            return
        script = self.config.feishu_notify_script
        if not script.is_file():
            logger.warning("feishu-notify.ps1 not found at %s", script)
            NOTIFY_FAILURES.labels(reason="script_missing").inc()
            return
        args = [
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Source",
            "control-plane",
            "-Severity",
            severity,
            "-Title",
            title[:500],
            "-Text",
            text[:9_500],
        ]
        if url:
            args += ["-Url", url]
        try:
            proc = await asyncio.create_subprocess_exec(
                "pwsh.exe",
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=30)
        except (OSError, TimeoutError) as exc:
            logger.warning("feishu notification failed: %s", exc)
            NOTIFY_FAILURES.labels(reason="spawn_or_timeout").inc()
