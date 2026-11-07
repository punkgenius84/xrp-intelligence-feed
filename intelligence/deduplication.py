from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import re
import unicodedata

from models import NewsItem


def _day(item: NewsItem) -> str:
    value = item.published_at or item.collected_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def _headline(item: NewsItem) -> tuple[str, list[str]]:
    text = unicodedata.normalize("NFKC", item.title).casefold()
    tokens = re.findall(r"[a-z0-9]+", text)
    return " ".join(tokens), tokens


def deduplicate(items: list[NewsItem], seen: set[str] | None = None) -> tuple[list[NewsItem], set[str]]:
    """Deduplicate URLs and long exact headlines; syndicated copies add no corroboration."""
    seen = set(seen or ())
    fresh: list[NewsItem] = []
    current_events: dict[str, list[NewsItem]] = {}
    for item in items:
        event_key = item.event_fingerprint
        if item.fingerprint in seen:
            continue
        first = next(iter(current_events.get(event_key, [])), None)
        title, tokens = _headline(item)
        if first is None and len(tokens) >= 6 and len("".join(tokens)) >= 30:
            for candidate in current_events.get(_day(item), []):
                candidate_title, candidate_tokens = _headline(candidate)
                if (len(candidate_tokens) >= 6 and len("".join(candidate_tokens)) >= 30
                        and SequenceMatcher(None, title, candidate_title).ratio() >= 0.94):
                    first = candidate
                    break
        if first is not None:
            if item.source_id and item.source_id != first.source_id and item.source_id not in first.duplicate_sources:
                first.duplicate_sources.append(item.source_id)
            if item.url != first.url and item.url not in first.duplicate_urls:
                first.duplicate_urls.append(item.url)
            continue
        if event_key in seen:
            continue
        current_events.setdefault(event_key, []).append(item)
        current_events.setdefault(_day(item), []).append(item)
        fresh.append(item)
        seen.add(item.fingerprint)
        seen.add(event_key)
    return fresh, seen

def reconsider_newly_qualified(
    items: list[NewsItem],
    fresh: list[NewsItem],
    cards: dict[str, dict],
    *,
    publish_score: int,
    max_age_days: int = 14,
    now: datetime | None = None,
    retention_hours: int = 72,
) -> list[NewsItem]:
    """Revisit recent seen items only when updated scoring moves them across the publish gate.

    This is deliberately narrower than a backlog replay: the candidate must have a
    recent correlation card, must not already be fresh in this run, and must have
    moved from below the configured threshold to at/above it. Delivery history and
    the durable outbox remain the final duplicate-safety controls.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    recent_cutoff = current - timedelta(hours=retention_hours)
    age_cutoff = (
        current - timedelta(days=max_age_days)
        if max_age_days > 0 else None
    )
    fresh_ids = {
        value
        for item in fresh
        for value in (
            getattr(item, "candidate_id", ""),
            getattr(item, "fingerprint", ""),
        )
        if value
    }
    fresh_events = {
        getattr(item, "event_fingerprint", "")
        for item in fresh
        if getattr(item, "event_fingerprint", "")
    }

    reconsidered: list[NewsItem] = []
    for item in items:
        candidate_id = (
            getattr(item, "candidate_id", "")
            or getattr(item, "fingerprint", "")
        )
        event_key = getattr(item, "event_fingerprint", "")
        if not candidate_id or candidate_id in fresh_ids or event_key in fresh_events:
            continue
        card = cards.get(candidate_id)
        if not isinstance(card, dict):
            continue
        previous_score = card.get("relevance_score", 0)
        if type(previous_score) is not int or previous_score >= publish_score:
            continue
        if item.relevance_score < publish_score:
            continue
        last_seen_raw = card.get("last_seen")
        if not isinstance(last_seen_raw, str):
            continue
        try:
            last_seen = datetime.fromisoformat(last_seen_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen.astimezone(timezone.utc) < recent_cutoff:
            continue

        published = item.published_at or item.collected_at
        if published is not None:
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if age_cutoff is not None and published.astimezone(timezone.utc) < age_cutoff:
                continue
        reconsidered.append(item)

    return sorted(
        reconsidered,
        key=lambda item: (
            -item.relevance_score,
            -(item.published_at or item.collected_at).timestamp(),
            item.title.casefold(),
        ),
    )

