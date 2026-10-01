from datetime import datetime, timezone

from intelligence.buried_signals import detect_buried_signals
from models import NewsItem


def item(score=20, correlation=85, quality="primary", entities=None, sources=None):
    return NewsItem(
        title="Ripple and DBS expand tokenized payment collaboration",
        url="https://example.com/story",
        source="Ripple",
        source_id="ripple-press-center",
        candidate_id="ripple:buried-1",
        published_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        detected_entities=entities or ["Ripple", "DBS"],
        source_quality=quality,
        relevance_score=score,
        correlation_score=correlation,
        correlated_source_ids=sources or ["dbs-newsroom"],
        correlated_candidate_ids=["dbs:buried-1"],
    )


def test_flags_strong_below_threshold_primary_signal_without_changing_relevance():
    candidate = item()
    original = candidate.relevance_score

    result = detect_buried_signals([candidate])

    assert result == [candidate]
    assert candidate.buried_signal is True
    assert candidate.buried_signal_score == 100
    assert candidate.relevance_score == original
    assert any("strong cross-source correlation" in r for r in candidate.buried_signal_reasons)


def test_does_not_flag_item_already_above_publish_threshold():
    candidate = item(score=35)

    assert detect_buried_signals([candidate]) == []
    assert candidate.buried_signal is False


def test_does_not_flag_weak_correlation():
    candidate = item(correlation=74)

    assert detect_buried_signals([candidate]) == []
    assert candidate.buried_signal is False


def test_does_not_flag_non_primary_source():
    candidate = item(quality="high_signal")

    assert detect_buried_signals([candidate]) == []
    assert candidate.buried_signal is False


def test_does_not_flag_without_high_value_entity():
    candidate = item(entities=["Tokenization"])

    assert detect_buried_signals([candidate]) == []
    assert candidate.buried_signal is False
