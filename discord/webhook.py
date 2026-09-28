from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import requests

MAX_CONTENT_LENGTH = 2000
_MAX_RATE_LIMIT_WAIT = 30.0
_WEBHOOK_URL = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+/?$"
)


class DiscordError(RuntimeError):
    """A Discord delivery problem. Messages never contain the webhook URL or token."""


def is_valid_webhook_url(value: str) -> bool:
    return bool(_WEBHOOK_URL.fullmatch(value.strip()))


class DiscordWebhook:
    def __init__(self, webhook_url: str | None = None, *, session: Any = None,
                 sleep: Callable[[float], None] = time.sleep):
        raw = webhook_url if webhook_url is not None else os.getenv("DISCORD_WEBHOOK_URL")
        self.webhook_url = (raw or "").strip()
        self._session = session or requests
        self._sleep = sleep

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        try:
            value = response.json().get("retry_after")
        except Exception:  # noqa: BLE001 - any unreadable body just means "unknown"
            value = None
        if value is None:
            value = getattr(response, "headers", {}).get("Retry-After")
        try:
            wait = float(value)
        except (TypeError, ValueError):
            return None
        return wait if wait >= 0 else None

    def send(self, content: str) -> None:
        if not self.webhook_url:
            raise DiscordError("DISCORD_WEBHOOK_URL is not configured")
        if not is_valid_webhook_url(self.webhook_url):
            raise DiscordError("DISCORD_WEBHOOK_URL is not a Discord webhook URL")
        # allowed_mentions.parse=[] means headline text can never ping @everyone or roles.
        payload = {"content": content[:MAX_CONTENT_LENGTH], "allowed_mentions": {"parse": []}}
        for attempt in range(2):
            try:
                response = self._session.post(self.webhook_url, json=payload, timeout=20)
            except requests.RequestException as exc:
                # Deliberately not chained: requests' messages include the full URL.
                raise DiscordError(f"Discord request failed ({type(exc).__name__})") from None
            if response.status_code == 429 and attempt == 0:
                wait = self._retry_after(response)
                if wait is None or wait > _MAX_RATE_LIMIT_WAIT:
                    raise DiscordError("Discord rate limit hit (HTTP 429); not retrying")
                self._sleep(wait)
                continue
            if not 200 <= response.status_code < 300:
                raise DiscordError(f"Discord returned HTTP {response.status_code}")
            return
        raise DiscordError("Discord rate limit hit (HTTP 429) again after retry")
