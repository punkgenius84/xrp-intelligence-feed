from datetime import datetime, timedelta, timezone
import json

import pytest

from storage.correlation_state import CorrelationStateError, JsonCorrelationState


STAMP = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def _card(candidate_id, published_at=STAMP, last_seen=STAMP, source_id="sec"):
    return {
        "candidate_id": candidate_id,
        "source_id": source_id,
        "published_at": published_at.isoformat(),
        "high_value_entities": ["Ripple", "SEC"],
        "title_tokens": ["digital", "asset", "ripple", "sec"],
        "content_hash": f"hash-{candidate_id}",
        "last_seen": last_seen.isoformat(),
    }


def test_missing_correlation_state_starts_empty(tmp_path):
    assert JsonCorrelationState(tmp_path / "correlation.json").load() == {
        "schema_version": 1,
        "cards": {},
    }


def test_card_round_trips_as_thin_bounded_memory(tmp_path):
    store = JsonCorrelationState(tmp_path / "correlation.json")
    state = store.load()
    JsonCorrelationState.upsert_card(
        state,
        candidate_id="sec:1",
        source_id="sec",
        published_at=STAMP,
        high_value_entities=["SEC", "Ripple"],
        title_tokens=["ripple", "sec", "digital", "asset"],
        content_hash="hash-1",
        last_seen=STAMP,
    )
    store.save(state, now=STAMP)

    loaded = store.load()
    assert loaded["cards"]["sec:1"] == _card("sec:1")
    assert "summary" not in loaded["cards"]["sec:1"]


def test_upsert_same_candidate_replaces_card_instead_of_duplicating(tmp_path):
    store = JsonCorrelationState(tmp_path / "correlation.json")
    state = store.load()
    JsonCorrelationState.upsert_card(
        state,
        candidate_id="sec:1",
        source_id="sec",
        published_at=STAMP,
        high_value_entities=["SEC"],
        title_tokens=["old", "title"],
        content_hash="old-hash",
        last_seen=STAMP,
    )
    updated = STAMP + timedelta(hours=1)
    JsonCorrelationState.upsert_card(
        state,
        candidate_id="sec:1",
        source_id="sec",
        published_at=STAMP,
        high_value_entities=["SEC", "Ripple"],
        title_tokens=["new", "title"],
        content_hash="new-hash",
        last_seen=updated,
    )
    assert list(state["cards"]) == ["sec:1"]
    assert state["cards"]["sec:1"]["content_hash"] == "new-hash"
    assert state["cards"]["sec:1"]["title_tokens"] == ["new", "title"]


def test_retention_uses_published_at_and_then_oldest_last_seen(tmp_path):
    store = JsonCorrelationState(tmp_path / "correlation.json", max_cards=2, retention_hours=72)
    state = store.load()
    old = STAMP - timedelta(hours=73)
    JsonCorrelationState.upsert_card(
        state, candidate_id="old", source_id="sec", published_at=old,
        high_value_entities=["SEC"], title_tokens=["old"], content_hash="old", last_seen=old,
    )
    for candidate_id in ("b", "a", "c"):
        JsonCorrelationState.upsert_card(
            state, candidate_id=candidate_id, source_id="sec", published_at=STAMP,
            high_value_entities=["SEC"], title_tokens=[candidate_id],
            content_hash=candidate_id, last_seen=STAMP,
        )
    store.save(state, now=STAMP)
    assert list(store.load()["cards"]) == ["b", "c"]


@pytest.mark.parametrize("content", [
    "{bad",
    "[]",
    '{"schema_version":1,"cards":[]}',
])
def test_corrupt_state_fails_closed_and_save_does_not_replace_it(tmp_path, content):
    path = tmp_path / "correlation.json"
    path.write_text(content, encoding="utf-8")
    store = JsonCorrelationState(path)
    with pytest.raises(CorrelationStateError):
        store.load()
    assert path.read_text(encoding="utf-8") == content


def test_save_is_atomic_and_deterministic(tmp_path):
    path = tmp_path / "nested" / "correlation.json"
    store = JsonCorrelationState(path)
    state = store.load()
    state["cards"] = {"z": _card("z"), "a": _card("a")}
    store.save(state, now=STAMP)
    saved = path.read_text(encoding="utf-8")
    assert saved.endswith("\n")
    assert list(json.loads(saved)["cards"]) == ["a", "z"]
    assert not path.with_suffix(".json.tmp").exists()
