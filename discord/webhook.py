from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

import requests


MAX_CONTENT_LENGTH = 2_000
_MAX_RATE_LIMIT_WAIT = 30.0
_WEBHOOK_ENV_VAR = "DISCORD_INTELLIGENCE_DISCORD_WEBHOOK"

_WEBHOOK_URL = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?"
    r"discord(?:app)?\.com/api(?:/v\d+)?/webhooks/"
    r"\d+/[A-Za-z0-9_-]+/?$"
)


class DiscordError(RuntimeError):
    """A Discord delivery problem.

    Error messages intentionally never contain the webhook URL or token.
    """


def is_valid_webhook_url(value: str) -> bool:
    """Return True when value looks like a valid Discord webhook URL."""
    return bool(_WEBHOOK_URL.fullmatch(value.strip()))


class DiscordWebhook:
    def __init__(
        self,
        webhook_url: str | None = None,
        *,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        raw_url = (
            webhook_url
            if webhook_url is not None
            else os.getenv(_WEBHOOK_ENV_VAR)
        )

        self.webhook_url = (raw_url or "").strip()
        self._session = session or requests
        self._sleep = sleep

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        """Extract a non-negative retry delay from a Discord response."""
        value: Any = None

        try:
            value = response.json().get("retry_after")
        except Exception:  # noqa: BLE001
            # Fall back to the HTTP header if the response body is unreadable.
            pass

        if value is None:
            headers = getattr(response, "headers", {})
            value = headers.get("Retry-After")

        try:
            wait = float(value)
        except (TypeError, ValueError):
            return None

        return wait if wait >= 0 else None

    def send(self, content: str) -> None:
        """Send content to the configured Discord webhook."""
        if not isinstance(content, str):
            raise DiscordError("Discord message content must be a string")

        if not self.webhook_url:
            raise DiscordError(
                f"{_WEBHOOK_ENV_VAR} is not configured"
            )

        if not is_valid_webhook_url(self.webhook_url):
            raise DiscordError(
                f"{_WEBHOOK_ENV_VAR} is not a Discord webhook URL"
            )

        payload = {
            "content": content[:MAX_CONTENT_LENGTH],
            # Prevent @everyone, @here, user, and role mentions.
            "allowed_mentions": {
                "parse": [],
            },
        }

        for attempt in range(2):
            try:
                response = self._session.post(
                    self.webhook_url,
                    json=payload,
                    timeout=20,
                )
            except requests.RequestException as exc:
                # Do not chain the exception because its message may contain
                # the complete webhook URL, including the secret token.
                raise DiscordError(
                    f"Discord request failed ({type(exc).__name__})"
                ) from None

            status_code = getattr(response, "status_code", None)

            if status_code == 429 and attempt == 0:
                wait = self._retry_after(response)

                if wait is None or wait > _MAX_RATE_LIMIT_WAIT:
                    raise DiscordError(
                        "Discord rate limit hit (HTTP 429); not retrying"
                    )

                self._sleep(wait)
                continue

            if status_code == 429:
                raise DiscordError(
                    "Discord rate limit hit (HTTP 429) again after retry"
                )

            if not isinstance(status_code, int) or not 200 <= status_code < 300:
                raise DiscordError(
                    f"Discord returned HTTP {status_code}"
                )

            return
