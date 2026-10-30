"""Alert notifications. Console always; ntfy.sh push and a JSON webhook when configured.

The engine only queues notifications; they are sent from the background loop so a slow
push service can never delay the reply to DT (which must come within 10 seconds).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import httpx

from .config import Settings

log = logging.getLogger("coldwatch.notify")

LEVEL_NAMES = {1: "staff", 2: "manager"}


@dataclass
class Notification:
    alert_id: int
    level: int  # 1 = staff on shift, 2 = escalated to the manager
    unit: str
    kind: str
    severity: str
    message: str

    @property
    def title(self) -> str:
        prefix = "ESCALATED: " if self.level >= 2 else ""
        return f"{prefix}{self.unit}: {self.kind.replace('_', ' ')}"

    @property
    def text(self) -> str:
        who = LEVEL_NAMES.get(self.level, "staff")
        tail = " Not acknowledged in time." if self.level >= 2 else ""
        return f"[to {who}] {self.message.rstrip('.')}.{tail}"


class Dispatcher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=5)

    def send(self, notifications: list[Notification]) -> None:
        for n in notifications:
            print(f"[coldwatch] ALERT #{n.alert_id} {n.title} - {n.text}", flush=True)
            if self.settings.ntfy_topic:
                self._try(self._ntfy, n)
            if self.settings.webhook_url:
                self._try(self._webhook, n)

    def _ntfy(self, n: Notification) -> None:
        url = f"{self.settings.ntfy_server.rstrip('/')}/{self.settings.ntfy_topic}"
        priority = "urgent" if n.level >= 2 else ("high" if n.severity == "critical" else "default")
        headers = {"Title": n.title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": "thermometer"}
        self.client.post(url, content=n.text.encode(), headers=headers).raise_for_status()

    def _webhook(self, n: Notification) -> None:
        payload = asdict(n) | {"title": n.title, "text": n.text}
        self.client.post(self.settings.webhook_url, json=payload).raise_for_status()

    @staticmethod
    def _try(fn, n: Notification) -> None:
        try:
            fn(n)
        except httpx.HTTPError as exc:
            log.warning("notification for alert %s failed: %s", n.alert_id, exc)
