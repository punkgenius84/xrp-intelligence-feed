from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter

from storage.delivery_history import PostedItem

MAX_DIGEST_LENGTH = 2_000
MAX_ITEMS = 12
MAX_STRATEGIC_EVENTS = 5


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _weekly_intelligence_events(
    events: list[dict] | None,
    *,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, dict]]:
    selected: list[tuple[datetime, dict]] = []
    for event in events or []:
        if not isinstance(event, dict) or event.get("status", "active") != "active":
            continue
        evidence = event.get("evidence", [])
        if not isinstance(evidence, list):
            continue
        dates = [
            stamp
            for item in evidence
            if isinstance(item, dict)
            for stamp in [_parse_timestamp(item.get("published_at"))]
            if stamp is not None and start <= stamp <= end
        ]
        if dates:
            selected.append((max(dates), event))
    selected.sort(
        key=lambda pair: (
            -pair[0].timestamp(),
            str(pair[1].get("event_id", "")),
        )
    )
    return selected


def format_weekly_digest(
    items: list[PostedItem],
    *,
    end: datetime | None = None,
    intelligence_events: list[dict] | None = None,
) -> str:
    current = end or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    start = current - timedelta(days=7)
    weekly = [
        item for item in items
        if start <= item.posted_at.astimezone(timezone.utc) <= current
    ]
    weekly.sort(key=lambda item: (item.posted_at, item.key))
    strategic = _weekly_intelligence_events(
        intelligence_events, start=start, end=current
    )

    lines = [
        "**XRP & Crypto Intelligence — Weekly Digest**",
        f"{start.strftime('%Y-%m-%d')} → {current.strftime('%Y-%m-%d')}",
        f"**Posted items:** {len(weekly)} · **Validated intelligence developments:** {len(strategic)}",
    ]

    if strategic:
        lines.append("**Strategic signals & partnership read-through:**")
        for published, event in strategic[:MAX_STRATEGIC_EVENTS]:
            event_type = str(event.get("event_type") or "development").replace("_", " ").title()
            summary = " ".join(str(event.get("summary") or "").split())
            significance = " ".join(str(event.get("significance") or "").split())
            if not summary:
                continue
            date = published.strftime("%Y-%m-%d")
            lines.append(f"• [{date}] **{event_type}:** {summary[:260]}")
            if significance:
                label = "Strategic read-through" if event_type.casefold() == "partnership" else "Why it matters"
                lines.append(f"  **{label}:** {significance[:420]}")
            evidence = event.get("evidence", [])
            first_source = next(
                (
                    item for item in evidence
                    if isinstance(item, dict)
                    and isinstance(item.get("url"), str)
                    and item["url"].startswith("https://")
                ),
                None,
            )
            if first_source:
                source_title = " ".join(str(first_source.get("title") or "Source").split())[:100]
                lines.append(f"  [Source: {source_title}]({first_source['url']})")
        if len(strategic) > MAX_STRATEGIC_EVENTS:
            lines.append(f"…and {len(strategic) - MAX_STRATEGIC_EVENTS} more validated developments.")
    else:
        lines.append("No new validated intelligence developments were recorded during this period.")

    if weekly:
        sources = Counter(item.source for item in weekly if item.source)
        if sources:
            top_sources = ", ".join(
                f"{source} ({count})"
                for source, count in sources.most_common(5)
            )
            lines.append(f"**News sources:** {top_sources}")

        lines.append("**What posted:**")
        for item in weekly[:MAX_ITEMS]:
            if item.published_at is not None:
                date = item.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
                date_label = f"Published {date}"
            else:
                date = item.posted_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
                date_label = f"Posted {date}"
            reason = f" — Why: {item.score_reason}" if item.score_reason else ""
            lines.append(
                f"• {date_label} · {item.source} — {item.title}{reason}\n  {item.url}"
            )

        if len(weekly) > MAX_ITEMS:
            lines.append(f"…and {len(weekly) - MAX_ITEMS} more news-feed posts.")
    else:
        lines.append("**News-feed posts:** none.")

    message = "\n".join(lines)
    if len(message) <= MAX_DIGEST_LENGTH:
        return message
    return message[: MAX_DIGEST_LENGTH - 1].rstrip() + "…"
