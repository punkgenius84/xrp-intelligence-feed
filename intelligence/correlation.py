from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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


def _card_day(card: dict) -> object:
    value = datetime.fromisoformat(card["published_at"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def _score_fields(
    *,
    left_source_id: str,
    left_entities: set[str],
    left_tokens: set[str],
    left_day: object,
    right_source_id: str,
    right_entities: set[str],
    right_tokens: set[str],
    right_day: object,
) -> tuple[int, set[str]]:
    if left_source_id and left_source_id == right_source_id:
        return 0, set()

    shared_entities = left_entities & right_entities & _CORRELATION_ENTITIES
    if not shared_entities:
        return 0, set()

    if not left_tokens or not right_tokens:
        return 0, set()

    overlap = left_tokens & right_tokens
    jaccard = len(overlap) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(
        None,
        " ".join(sorted(left_tokens)),
        " ".join(sorted(right_tokens)),
    ).ratio()

    # Same-day reporting is the strongest case. Allow one day either side because
    # an event can cross midnight or be reported after an initial announcement.
    day_gap = abs((left_day - right_day).days)
    if day_gap > 1:
        return 0, set()

    # Require meaningful lexical overlap in addition to an important shared entity.
    # This prevents two unrelated XRP/SEC stories from becoming one cluster.
    if len(overlap) < 2:
        return 0, set()
    if jaccard < 0.30 and sequence < 0.72:
        return 0, set()

    score = 60
    if day_gap == 0:
        score += 15
    if jaccard >= 0.45:
        score += 15
    if sequence >= 0.82:
        score += 10
    return min(score, 100), shared_entities


def _candidate_score(left: NewsItem, right: NewsItem) -> int:
    score, _ = _score_fields(
        left_source_id=left.source_id,
        left_entities=set(left.detected_entities),
        left_tokens=_tokens(left),
        left_day=_day(left),
        right_source_id=right.source_id,
        right_entities=set(right.detected_entities),
        right_tokens=_tokens(right),
        right_day=_day(right),
    )
    return score


def _card_candidate_id(item: NewsItem) -> str:
    # Discovery candidates have stable IDs. RSS items may not, so their existing
    # content fingerprint provides a deterministic fallback identity for memory.
    return item.candidate_id or item.fingerprint


def _card_from_item(item: NewsItem) -> dict:
    published = item.published_at or item.collected_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return {
        "candidate_id": _card_candidate_id(item),
        "source_id": item.source_id,
        "published_at": published.astimezone(timezone.utc).isoformat(),
        "high_value_entities": sorted(
            set(item.detected_entities) & _CORRELATION_ENTITIES
        ),
        "title_tokens": sorted(_tokens(item)),
        "content_hash": item.content_hash,
        "last_seen": item.collected_at.astimezone(timezone.utc).isoformat(),
    }


def _apply_match(
    item: NewsItem,
    score: int,
    other_source_id: str,
    other_candidate_id: str,
    shared_entities: set[str],
) -> None:
    if score <= 0:
        return
    item.correlation_score = max(item.correlation_score, score)
    if other_source_id and other_source_id != item.source_id:
        if other_source_id not in item.correlated_source_ids:
            item.correlated_source_ids.append(other_source_id)
    if other_candidate_id and other_candidate_id != item.candidate_id:
        if other_candidate_id not in item.correlated_candidate_ids:
            item.correlated_candidate_ids.append(other_candidate_id)
    reason = f"shared entity: {', '.join(sorted(shared_entities))}"
    if reason not in item.correlation_reasons:
        item.correlation_reasons.append(reason)


def correlate(
    items: list[NewsItem],
    history: list[dict] | None = None,
) -> list[NewsItem]:
    """Attach conservative same-batch and recent cross-run event correlations.

    History is a thin persisted card set. It never changes relevance scores,
    deduplicates items, or treats repeated reporting as proof of a claim.
    """
    history = history or []
    for item in items:
        item.correlation_score = 0
        item.correlated_source_ids.clear()
        item.correlated_candidate_ids.clear()
        item.correlation_reasons.clear()

    buckets: dict[object, list[NewsItem]] = defaultdict(list)
    for item in items:
        buckets[_day(item)].append(item)

    for day, bucket in buckets.items():
        nearby = (
            bucket
            + buckets.get(day.fromordinal(day.toordinal() - 1), [])
            + buckets.get(day.fromordinal(day.toordinal() + 1), [])
        )
        for left in bucket:
            for right in nearby:
                if left is right:
                    continue
                score = _candidate_score(left, right)
                if score <= 0:
                    continue
                shared = (
                    set(left.detected_entities)
                    & set(right.detected_entities)
                    & _CORRELATION_ENTITIES
                )
                _apply_match(
                    left,
                    score,
                    right.source_id,
                    right.candidate_id,
                    shared,
                )

    for item in items:
        left_tokens = _tokens(item)
        left_entities = set(item.detected_entities)
        left_day = _day(item)
        for card in history:
            if card.get("candidate_id") == _card_candidate_id(item):
                continue
            score, shared = _score_fields(
                left_source_id=item.source_id,
                left_entities=left_entities,
                left_tokens=left_tokens,
                left_day=left_day,
                right_source_id=card["source_id"],
                right_entities=set(card["high_value_entities"]),
                right_tokens=set(card["title_tokens"]),
                right_day=_card_day(card),
            )
            _apply_match(
                item,
                score,
                card["source_id"],
                card["candidate_id"],
                shared,
            )

    return items


def build_correlation_card(item: NewsItem) -> dict:
    """Return the bounded memory representation for one enriched candidate."""
    return _card_from_item(item)
