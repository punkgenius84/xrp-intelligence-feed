from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from difflib import SequenceMatcher
import re
import unicodedata

from models import NewsItem


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "into",
    "is", "it", "of", "on", "or", "that", "the", "to", "with",
}

# Correlation is deliberately conservative. These are entities whose overlap is
# useful evidence that two otherwise different headlines may describe one event.
_CORRELATION_ENTITIES = {
    "XRP", "Ripple", "RLUSD", "SEC", "CFTC", "Treasury", "Federal Reserve",
    "OCC", "FDIC", "FinCEN", "OFAC", "DOJ", "BIS",
    "Citi", "Circle", "Coinbase", "Mastercard", "Swift", "Visa", "DBS",
    "J.P. Morgan", "BNY",
}


def _tokens(item: NewsItem) -> set[str]:
    text = unicodedata.normalize("NFKC", item.title).casefold()
    return {
        token for token in re.findall(r"[a-z0-9]+", text)
        if token not in _STOPWORDS and len(token) > 2
    }


def _day(item: NewsItem):
    value = item.published_at or item.collected_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def _candidate_score(left: NewsItem, right: NewsItem) -> int:
    if left.source_id and left.source_id == right.source_id:
        return 0

    shared_entities = (
        set(left.detected_entities) & set(right.detected_entities)
        & _CORRELATION_ENTITIES
    )
    if not shared_entities:
        return 0

    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0

    overlap = left_tokens & right_tokens
    jaccard = len(overlap) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(
        None,
        " ".join(sorted(left_tokens)),
        " ".join(sorted(right_tokens)),
    ).ratio()

    # Same-day reporting is the strongest case. Allow one day either side because
    # an event can cross midnight or be reported after an initial announcement.
    day_gap = abs((_day(left) - _day(right)).days)
    if day_gap > 1:
        return 0

    # Require meaningful lexical overlap in addition to an important shared entity.
    # This prevents two unrelated XRP/SEC stories from becoming one cluster.
    if len(overlap) < 2:
        return 0
    if jaccard < 0.30 and sequence < 0.72:
        return 0

    score = 60
    if day_gap == 0:
        score += 15
    if jaccard >= 0.45:
        score += 15
    if sequence >= 0.82:
        score += 10
    return min(score, 100)


def correlate(items: list[NewsItem]) -> list[NewsItem]:
    """Attach conservative cross-source event correlations without deduplicating items.

    Correlation is run only across the current batch. It never changes relevance
    scores and never treats correlation as proof of the underlying claim.
    """
    for item in items:
        item.correlation_score = 0
        item.correlated_source_ids.clear()
        item.correlated_candidate_ids.clear()
        item.correlation_reasons.clear()

    buckets: dict[object, list[NewsItem]] = defaultdict(list)
    for item in items:
        day = _day(item)
        buckets[day].append(item)

    for day, bucket in buckets.items():
        neighbors = list(bucket)
        if day:
            neighbors.extend(buckets.get(day.replace(day=day), []))
        # Compare nearby days explicitly; each item also gets checked against the
        # preceding/following bucket so the algorithm remains bounded by batch size.
        nearby = bucket + buckets.get(day.fromordinal(day.toordinal() - 1), []) + buckets.get(
            day.fromordinal(day.toordinal() + 1), []
        )
        for index, left in enumerate(bucket):
            for right in nearby:
                if left is right:
                    continue
                score = _candidate_score(left, right)
                if score <= 0:
                    continue
                left.correlation_score = max(left.correlation_score, score)
                if right.source_id and right.source_id != left.source_id:
                    if right.source_id not in left.correlated_source_ids:
                        left.correlated_source_ids.append(right.source_id)
                if right.candidate_id and right.candidate_id != left.candidate_id:
                    if right.candidate_id not in left.correlated_candidate_ids:
                        left.correlated_candidate_ids.append(right.candidate_id)
                shared = sorted(
                    set(left.detected_entities) & set(right.detected_entities)
                    & _CORRELATION_ENTITIES
                )
                reason = f"shared entity: {', '.join(shared)}"
                if reason not in left.correlation_reasons:
                    left.correlation_reasons.append(reason)
    return items
