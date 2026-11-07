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


def test_weekly_digest_labels_source_publication_date_separately_from_digest_window():
    end = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)
    item = PostedItem(
        key="discovered-late",
        source="Circle",
        title="Circle stablecoin partnership",
        url="https://example.com/circle-partnership",
        published_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
        posted_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc),
        relevance_score=70,
        score_reason="primary source",
    )

    message = format_weekly_digest([item], end=end)

    # Inclusion is based on when the item posted to Discord; the line label
    # makes its older source publication date explicit rather than implying it
    # was published during the digest week.
    assert "Posted items:** 1" in message
    assert "2026-10-04 → 2026-10-11" in message
    assert "Published 2026-04-29 · Circle — Circle stablecoin partnership" in message


def test_weekly_digest_uses_posted_date_when_publication_date_is_unknown():
    end = datetime(2026, 10, 11, 17, tzinfo=timezone.utc)
    item = PostedItem(
        key="unknown-publication",
        source="Test Source",
        title="Undated source item",
        url="https://example.com/undated",
        published_at=None,
        posted_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc),
        relevance_score=40,
        score_reason="",
    )

    message = format_weekly_digest([item], end=end)

    assert "Posted 2026-10-10 · Test Source — Undated source item" in message


def test_empty_weekly_digest_is_explicit():
    end = datetime(2026, 9, 27, 17, tzinfo=timezone.utc)
    message = format_weekly_digest([], end=end)
    assert "Posted items:** 0" in message
    assert "No publishable items were posted" in message

def test_weekly_digest_includes_strategic_readthrough_for_partnerships():
    end = datetime(2026, 11, 8, 17, tzinfo=timezone.utc)
    events = [
        {
            "event_id": "evt-partnership",
            "event_type": "partnership",
            "summary": "Ripple and a major bank agreed to explore tokenized settlement services.",
            "significance": (
                "Strategic read-through: the bank could gain tokenization infrastructure and "
                "a new institutional distribution channel, while Ripple could gain access to "
                "bank clients. This may signal a future product rollout; no XRP usage or launch "
                "is confirmed."
            ),
            "status": "active",
            "evidence": [
                {
                    "published_at": "2026-11-06T12:00:00+00:00",
                    "title": "Bank and Ripple announce tokenization partnership",
                    "url": "https://example.com/partnership",
                }
            ],
        }
    ]

    message = format_weekly_digest([], end=end, intelligence_events=events)

    assert "Validated intelligence developments:** 1" in message
    assert "Strategic signals & partnership read-through" in message
    assert "Strategic read-through:" in message
    assert "bank could gain tokenization infrastructure" in message
    assert "https://example.com/partnership" in message
    assert "no XRP usage or launch is confirmed" in message


def test_weekly_digest_excludes_stale_or_out_of_window_intelligence():
    end = datetime(2026, 11, 8, 17, tzinfo=timezone.utc)
    events = [
        {
            "event_id": "evt-old",
            "event_type": "partnership",
            "summary": "Old partnership",
            "significance": "Old significance",
            "status": "active",
            "evidence": [
                {
                    "published_at": "2026-10-20T12:00:00+00:00",
                    "title": "Old partnership",
                    "url": "https://example.com/old",
                }
            ],
        },
        {
            "event_id": "evt-superseded",
            "event_type": "partnership",
            "summary": "Superseded partnership",
            "significance": "Superseded significance",
            "status": "superseded",
            "evidence": [
                {
                    "published_at": "2026-11-06T12:00:00+00:00",
                    "title": "Superseded partnership",
                    "url": "https://example.com/superseded",
                }
            ],
        },
    ]

    message = format_weekly_digest([], end=end, intelligence_events=events)

    assert "Validated intelligence developments:** 0" in message
    assert "Old partnership" not in message
    assert "Superseded partnership" not in message

