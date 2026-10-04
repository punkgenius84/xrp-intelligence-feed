from datetime import datetime, timezone

import pytest

from models import NewsItem
from storage.outbox import (
    MAX_OUTBOX_ENTRIES,
    JsonOutboxState,
    OutboxError,
    from_record,
    publication_key,
)


def item(title="Story", score=50, candidate_id="source:1", content_hash="hash-1"):
    return NewsItem(
        title=title,
        url="https://example.test/story",
        source="Source",
        published_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        relevance_score=score,
        detected_entities=["XRP"],
        score_reasons=["signal"],
        candidate_id=candidate_id,
        content_hash=content_hash,
    )


def test_round_trip_and_deterministic_key(tmp_path):
    store = JsonOutboxState(tmp_path / "outbox.json")
    queued = store.enqueue([item()])
    assert len(queued) == 1
    assert queued[0].key == publication_key(item())
    loaded = store.load()
    assert loaded[0].title == "Story"
    assert loaded[0].relevance_score == 50


def test_same_publication_is_updated_not_duplicated(tmp_path):
    store = JsonOutboxState(tmp_path / "outbox.json")
    store.enqueue([item(score=40)])
    store.enqueue([item(score=80)])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].relevance_score == 80


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "outbox.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(OutboxError):
        JsonOutboxState(path).load()


def test_capacity_overflow_refuses_to_drop_undelivered_entries(tmp_path):
    store = JsonOutboxState(tmp_path / "outbox.json")
    values = [
        item(title=f"Story {n}", candidate_id=f"source:{n}", content_hash=f"hash-{n}")
        for n in range(MAX_OUTBOX_ENTRIES + 1)
    ]
    with pytest.raises(OutboxError, match="refusing to drop undelivered publications"):
        store.save([from_record({
            "key": publication_key(value),
            "candidate_id": value.candidate_id,
            "content_hash": value.content_hash,
            "title": value.title,
            "url": value.url,
            "source": value.source,
            "published_at": value.published_at.isoformat(),
            "collected_at": value.collected_at.isoformat(),
            "relevance_score": value.relevance_score,
            "detected_entities": value.detected_entities,
            "score_reasons": value.score_reasons,
        }) for value in values])
    assert not (tmp_path / "outbox.json").exists()

def test_capacity_limit_still_accepts_exact_bound(tmp_path):
    store = JsonOutboxState(tmp_path / "outbox.json")
    values = [
        item(title=f"Story {n}", candidate_id=f"source:{n}", content_hash=f"hash-{n}")
        for n in range(MAX_OUTBOX_ENTRIES)
    ]
    store.save([from_record({
        "key": publication_key(value),
        "candidate_id": value.candidate_id,
        "content_hash": value.content_hash,
        "title": value.title,
        "url": value.url,
        "source": value.source,
        "published_at": value.published_at.isoformat(),
        "collected_at": value.collected_at.isoformat(),
        "relevance_score": value.relevance_score,
        "detected_entities": value.detected_entities,
        "score_reasons": value.score_reasons,
    }) for value in values])
    assert len(store.load()) == MAX_OUTBOX_ENTRIES


def test_remove_is_idempotent(tmp_path):
    store = JsonOutboxState(tmp_path / "outbox.json")
    queued = store.enqueue([item()])
    store.remove({queued[0].key})
    assert store.load() == []
    store.remove({queued[0].key})
    assert store.load() == []
