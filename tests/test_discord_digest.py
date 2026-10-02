from datetime import datetime, timezone

from discord.digest import format_weekly_digest
from storage.delivery_history import PostedItem


def test_weekly_digest_contains_only_the_requested_week():
    end = datetime(2026, 9, 27, 17, tzinfo=timezone.utc)
    items = [
        PostedItem(
            key="old",
            source="Old Source",
            title="Old item",
            url="https://example.com/old",
            published_at=end,
            posted_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            relevance_score=90,
            score_reason="old reason",
        ),
        PostedItem(
            key="current",
            source="SEC",
            title="Current item",
            url="https://example.com/current",
            published_at=end,
            posted_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
            relevance_score=72,
            score_reason="primary source",
        ),
    ]
    message = format_weekly_digest(items, end=end)
    assert "Current item" in message
    assert "Old item" not in message
    assert "https://example.com/current" in message
    assert "Posted items:** 1" in message


def test_empty_weekly_digest_is_explicit():
    end = datetime(2026, 9, 27, 17, tzinfo=timezone.utc)
    message = format_weekly_digest([], end=end)
    assert "Posted items:** 0" in message
    assert "No publishable items were posted" in message
