"""Regression tests for configured broader-crypto ecosystem entities."""
from datetime import datetime, timezone

from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def make_item(title: str, summary: str) -> NewsItem:
    item = NewsItem(
        title=title,
        summary=summary,
        url="https://example.test/chainlink-update",
        source="Official Chainlink",
        source_id="crypto-entity-regression",
        source_quality="primary",
        authority_tier=1,
        category="technology",
        published_at=STAMP,
    )
    detect_entities(item)
    classify_source_quality(item)
    item.relevance_score, item.score_reasons = score_relevance(item)
    return item


def test_chainlink_action_is_detected_and_relevant_with_ecosystem_context():
    item = make_item(
        "Chainlink launches a new cross-chain protocol integration",
        "Chainlink announced an integration connecting blockchain networks.",
    )

    assert "Chainlink" in item.detected_entities
    assert "crypto_ecosystem_action" in item.relevance_categories
    assert item.relevance_score >= 30


def test_chainlink_name_alone_does_not_make_generic_commentary_relevant():
    item = make_item(
        "Chainlink market outlook and price discussion",
        "Analysts discussed market sentiment and possible future price movements.",
    )

    assert "Chainlink" in item.detected_entities
    assert "crypto_ecosystem_action" not in item.relevance_categories
    assert item.relevance_score < 30
