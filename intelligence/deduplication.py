from datetime import timezone
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
