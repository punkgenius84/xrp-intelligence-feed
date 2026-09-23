import os
import requests

class DiscordWebhook:
    def __init__(self, webhook_url: str | None = None):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")

    def send(self, content: str) -> None:
        if not self.webhook_url:
            raise RuntimeError("DISCORD_WEBHOOK_URL is not configured")
        response = requests.post(self.webhook_url, json={"content": content}, timeout=20)
        response.raise_for_status()
