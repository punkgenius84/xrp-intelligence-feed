from datetime import datetime, timezone

from intelligence.correlation import correlate
from models import NewsItem


def item(title, source_id, candidate_id, entities, day="2026-09-30"):
    return NewsItem(
        title=title,
        url=f"https://example.com/{candidate_id}",
        source=source_id,
        source_id=source_id,
        candidate_id=candidate_id,
        detected_entities=entities,
        published_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
    )


def test_correlates_different_official_sources_reporting_same_event():
    first = item(
        "Ripple and DBS announce tokenized deposit payment collaboration",
        "ripple-press-center",
        "ripple:1",
        ["Ripple", "DBS"],
    )
    second = item(
        "DBS expands tokenized deposit payments with Ripple collaboration",
        "dbs-newsroom",
        "dbs:1",
        ["DBS", "Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score >= 60
    assert "dbs-newsroom" in first.correlated_source_ids
    assert "dbs:1" in first.correlated_candidate_ids
    assert first.correlation_reasons


def test_does_not_correlate_unrelated_stories_sharing_an_entity():
    first = item(
        "Ripple launches new institutional payment corridor",
        "ripple-press-center",
        "ripple:2",
        ["Ripple"],
    )
    second = item(
        "Ripple reports engineering update for validator tooling",
        "xrpl-community-blog",
        "xrpl:2",
        ["Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score == 0
    assert first.correlated_source_ids == []
    assert first.correlated_candidate_ids == []


def test_does_not_correlate_items_from_the_same_source():
    first = item(
        "SEC announces digital asset enforcement action involving Ripple",
        "sec-press-releases",
        "sec:1",
        ["SEC", "Ripple"],
    )
    second = item(
        "SEC announces Ripple digital asset enforcement action",
        "sec-press-releases",
        "sec:2",
        ["SEC", "Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score == 0
    assert second.correlation_score == 0
