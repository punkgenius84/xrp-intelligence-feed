from __future__ import annotations

from models import NewsItem


# Keep this list aligned with the conservative correlation entity set. A buried
# signal needs a concrete, high-value subject rather than generic keyword overlap.
_HIGH_VALUE_ENTITIES = {
    "XRP", "Ripple", "RLUSD", "SEC", "CFTC", "Treasury", "Federal Reserve",
    "OCC", "FDIC", "FinCEN", "OFAC", "DOJ", "BIS",
    "Citi", "Circle", "Coinbase", "Mastercard", "Swift", "Visa", "DBS",
    "J.P. Morgan", "BNY",
}

MIN_CORRELATION_SCORE = 75


def detect_buried_signals(items: list[NewsItem], publish_score: int = 35) -> list[NewsItem]:
    """Flag strong, below-threshold cross-source signals without changing relevance.

    This is intentionally stricter than correlation itself. A candidate must be below
    the normal publish threshold, have strong cross-source correlation, come from a
    primary source, and contain a high-value entity. The detector only adds metadata;
    it never changes relevance scores, deduplicates candidates, or treats corroboration
    as factual confirmation.
    """
    buried: list[NewsItem] = []
    for item in items:
        item.buried_signal = False
        item.buried_signal_score = 0
        item.buried_signal_reasons.clear()

        if item.relevance_score >= publish_score:
            continue
        if item.correlation_score < MIN_CORRELATION_SCORE:
            continue
        if item.source_quality != "primary":
            continue
        if not item.correlated_source_ids:
            continue

        high_value = sorted(set(item.detected_entities) & _HIGH_VALUE_ENTITIES)
        if not high_value:
            continue

        score = item.correlation_score + 15 + 10
        if len(item.correlated_source_ids) >= 2:
            score += 5
        item.buried_signal_score = min(100, score)
        item.buried_signal = True
        item.buried_signal_reasons.extend([
            f"below publish threshold ({item.relevance_score} < {publish_score})",
            f"strong cross-source correlation ({item.correlation_score})",
            "primary-source candidate",
            f"high-value entity: {', '.join(high_value)}",
        ])
        if len(item.correlated_source_ids) >= 2:
            item.buried_signal_reasons.append(
                f"correlated with {len(item.correlated_source_ids)} other sources"
            )
        buried.append(item)

    return buried
