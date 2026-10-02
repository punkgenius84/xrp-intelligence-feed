from __future__ import annotations

import os

from discord.digest import format_weekly_digest
from discord.webhook import DiscordError, DiscordWebhook, is_valid_webhook_url
from storage.delivery_history import JsonDeliveryHistory, DeliveryHistoryError


def main() -> None:
    webhook_url = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not webhook_url:
        raise SystemExit("Weekly digest refused: DISCORD_WEBHOOK_URL is not set")
    if not is_valid_webhook_url(webhook_url):
        raise SystemExit("Weekly digest refused: DISCORD_WEBHOOK_URL is not a Discord webhook URL")

    try:
        history = JsonDeliveryHistory().load()
    except DeliveryHistoryError as exc:
        raise SystemExit(f"Weekly digest refused: {exc}") from exc

    message = format_weekly_digest(history)
    print(message)

    try:
        DiscordWebhook(webhook_url).send(message)
    except DiscordError as exc:
        raise SystemExit(f"Weekly digest post failed: {exc}") from exc

    print("Weekly digest: posted")


if __name__ == "__main__":
    main()
