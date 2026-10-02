from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
import time
from typing import Any, Callable, Mapping

from storage.outbox import publication_key

from discord.webhook import DiscordError, DiscordWebhook, is_valid_webhook_url

DEFAULT_MAX_POSTS = 5
MAX_POSTS_LIMIT = 25
_TRUTHY = {"1", "true", "yes", "on"}
_MARKDOWN = re.compile(r"([\\*_~`|>])")


@dataclass(frozen=True)
class DiscordSettings:
    webhook_url: str = ""
    max_posts: int = DEFAULT_MAX_POSTS
    dry_run: bool = False


@dataclass
class PublishReport:
    eligible: int = 0
    posted: int = 0
    failed: int = 0
    not_selected: int = 0
    dry_run: bool = False
    skipped_unconfigured: bool = False
    posted_keys: list[str] = field(default_factory=list)
    failed_keys: list[str] = field(default_factory=list)
    not_selected_keys: list[str] = field(default_factory=list)


def settings_from_env(env: Mapping[str, str] | None = None) -> DiscordSettings:
    """Read and validate Discord settings. Raises ValueError with a token-free message."""
    env = os.environ if env is None else env
    url = (env.get("DISCORD_WEBHOOK_URL") or "").strip()
    if url and not is_valid_webhook_url(url):
        raise ValueError("DISCORD_WEBHOOK_URL is set but is not a Discord webhook URL")
    raw_max = (env.get("DISCORD_MAX_POSTS") or "").strip()
    max_posts = DEFAULT_MAX_POSTS
    if raw_max:
        try:
            max_posts = int(raw_max)
        except ValueError:
            raise ValueError("DISCORD_MAX_POSTS must be a whole number") from None
        if not 1 <= max_posts <= MAX_POSTS_LIMIT:
            raise ValueError(f"DISCORD_MAX_POSTS must be from 1 to {MAX_POSTS_LIMIT}")
    dry_run = (env.get("DISCORD_DRY_RUN") or "").strip().lower() in _TRUTHY
    return DiscordSettings(webhook_url=url, max_posts=max_posts, dry_run=dry_run)


def _clean(value: str, limit: int) -> str:
    text = _MARKDOWN.sub(r"\\\1", " ".join(str(value).split()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_message(item: Any) -> str:
    when = _when(item).astimezone(timezone.utc).strftime("%Y-%m-%d")
    source = _clean(item.source, 100) or "Unknown source"
    lines = [
        f"**Source:** {source} · **Date:** {when}",
        f"**{_clean(item.title, 220)}**",
        "**Why this fired:** " + (
            _clean(item.score_reasons[0], 240)
            if item.score_reasons
            else "Matched configured relevance criteria."
        ),
    ]
    if item.detected_entities:
        lines.append("**Entities:** " + _clean(", ".join(item.detected_entities[:6]), 120))
    lines.append(f"**Score:** {item.relevance_score}")
    lines.append(item.url.strip())  # last, so Discord unfurls the link preview
    return "\n".join(lines)


def _when(item: Any) -> datetime:
    value = item.published_at or item.collected_at
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def publish(items: list[Any], settings: DiscordSettings, *, webhook: DiscordWebhook | None = None,
            sleep: Callable[[float], None] = time.sleep, pause: float = 1.0,
            out: Callable[[str], None] = print) -> PublishReport:
    report = PublishReport(eligible=len(items), dry_run=settings.dry_run)
    if not items:
        return report
    # If capped, keep the highest-scoring items (newest first on ties), then post oldest-first
    # so the channel reads chronologically.
    ranked = sorted(items, key=lambda i: (-i.relevance_score, -_when(i).timestamp()))
    selected = sorted(ranked[: settings.max_posts], key=_when)
    report.not_selected = len(items) - len(selected)
    report.not_selected_keys = [publication_key(item) for item in ranked[settings.max_posts:]]

    if settings.dry_run:
        for item in selected:
            out("[dry run] would post to Discord:\n" + format_message(item) + "\n")
    elif not settings.webhook_url:
        report.skipped_unconfigured = True
        out("Discord: DISCORD_WEBHOOK_URL is not set; nothing was posted")
        return report
    else:
        hook = webhook or DiscordWebhook(settings.webhook_url, sleep=sleep)
        consecutive_failures = 0
        for index, item in enumerate(selected):
            try:
                hook.send(format_message(item))
            except DiscordError as exc:
                consecutive_failures += 1
                out(f"::warning::Discord post failed for {item.url}: {exc}")
                report.failed += 1
                report.failed_keys.append(publication_key(item))
                if consecutive_failures >= 2:
                    remaining = selected[index + 1:]
                    report.failed += len(remaining)
                    report.failed_keys.extend(publication_key(value) for value in remaining)
                    out("Discord: stopping after 2 consecutive failures")
                    break
                continue
            consecutive_failures = 0
            report.posted += 1
            report.posted_keys.append(publication_key(item))
            if index < len(selected) - 1:
                sleep(pause)

    label = "would post" if settings.dry_run else "posted"
    count = len(selected) if settings.dry_run else report.posted
    line = f"Discord: {label} {count} of {report.eligible} relevant"
    if report.failed:
        line += f"; {report.failed} failed"
    if report.not_selected:
        line += (f"; {report.not_selected} over the {settings.max_posts}-post cap "
                 "remain queued for a later run")
    out(line)
    return report
