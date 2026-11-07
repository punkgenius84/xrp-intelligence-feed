from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from intelligence.deduplication import reconsider_newly_qualified


NOW = datetime(2026, 11, 7, 17, 0, tzinfo=timezone.utc)


def item(*, score=35, published_at=None, event_key="event-1"):
    return SimpleNamespace(
        candidate_id="candidate-1",
        fingerprint="fingerprint-1",
        event_fingerprint=event_key,
        relevance_score=score,
        title="New digital asset market structure development",
        published_at=published_at or NOW - timedelta(hours=3),
        collected_at=NOW - timedelta(hours=1),
    )


def card(*, score=0, last_seen=None):
    return {
        "relevance_score": score,
        "last_seen": (last_seen or NOW - timedelta(hours=2)).isoformat(),
    }


def test_recent_seen_candidate_is_reconsidered_when_it_crosses_publish_threshold():
    candidate = item()
    result = reconsider_newly_qualified(
        [candidate], [], {"candidate-1": card(score=0)},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == [candidate]


def test_candidate_already_above_threshold_is_not_replayed():
    candidate = item()
    result = reconsider_newly_qualified(
        [candidate], [], {"candidate-1": card(score=42)},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == []


def test_seen_candidate_older_than_correlation_retention_is_not_replayed():
    candidate = item()
    result = reconsider_newly_qualified(
        [candidate], [],
        {"candidate-1": card(score=0, last_seen=NOW - timedelta(hours=73))},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == []


def test_stale_seen_candidate_is_not_replayed():
    candidate = item(published_at=NOW - timedelta(days=15))
    result = reconsider_newly_qualified(
        [candidate], [], {"candidate-1": card(score=0)},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == []


def test_seen_duplicate_is_not_reconsidered_when_same_event_is_fresh_this_run():
    candidate = item()
    fresh_duplicate = item(event_key="event-1")
    fresh_duplicate.candidate_id = "candidate-2"
    result = reconsider_newly_qualified(
        [candidate], [fresh_duplicate], {"candidate-1": card(score=0)},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == []


def test_candidate_still_below_threshold_is_not_reconsidered():
    candidate = item(score=29)
    result = reconsider_newly_qualified(
        [candidate], [], {"candidate-1": card(score=0)},
        publish_score=30, max_age_days=14, now=NOW,
    )
    assert result == []
