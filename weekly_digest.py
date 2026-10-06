from __future__ import annotations

from discord.digest import format_weekly_digest
from discord.publisher import settings_from_env
from discord.webhook import DiscordError, DiscordWebhook
from storage.delivery_history import JsonDeliveryHistory, DeliveryHistoryError


def main() -> None:
    try:
        webhook_url = settings_from_env().webhook_url
    except ValueError as exc:
        raise SystemExit(f"Weekly digest refused: {exc}") from exc
    if not webhook_url:
        raise SystemExit("Weekly digest refused: DISCORD_WEBHOOK_URL is not set")

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
