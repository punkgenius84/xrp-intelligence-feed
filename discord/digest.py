from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter

from storage.delivery_history import PostedItem

MAX_DIGEST_LENGTH = 2_000
MAX_ITEMS = 12


def format_weekly_digest(
    items: list[PostedItem],
    *,
    end: datetime | None = None,
) -> str:
    current = end or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    start = current - timedelta(days=7)
    weekly = [
        item for item in items
        if start <= item.posted_at.astimezone(timezone.utc) <= current
    ]
    weekly.sort(key=lambda item: (item.posted_at, item.key))

    lines = [
        "**XRP Intelligence — Weekly Digest**",
        f"{start.astimezone(timezone.utc).strftime('%Y-%m-%d')} → {current.astimezone(timezone.utc).strftime('%Y-%m-%d')}",
        f"**Posted items:** {len(weekly)}",
    ]
    if not weekly:
        lines.append("No publishable items were posted during this period.")
        return "\n".join(lines)

    sources = Counter(item.source for item in weekly if item.source)
    if sources:
        top_sources = ", ".join(
            f"{source} ({count})"
            for source, count in sources.most_common(5)
        )
        lines.append(f"**Sources:** {top_sources}")

    lines.append("**What posted:**")
    for item in weekly[:MAX_ITEMS]:
        date = (item.published_at or item.posted_at).astimezone(timezone.utc).strftime("%Y-%m-%d")
        reason = f" — Why: {item.score_reason}" if item.score_reason else ""
        lines.append(
            f"• {date} · {item.source} — {item.title}{reason}\n  {item.url}"
        )

    if len(weekly) > MAX_ITEMS:
        lines.append(f"…and {len(weekly) - MAX_ITEMS} more posted items.")

    return "\n".join(lines)[:MAX_DIGEST_LENGTH]
