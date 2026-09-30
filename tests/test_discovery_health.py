from datetime import datetime, timezone

import pytest

from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def test_health_tracks_failures_and_resets_after_success(tmp_path):
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    state = store.load()

    JsonDiscoveryState.record_health(state, "sec", STAMP, "failed", 0, "timeout")
    JsonDiscoveryState.record_health(state, "sec", STAMP, "partial", 2, "one malformed item")

    assert state["sources"]["sec"]["health"] == {
        "last_attempt": STAMP.isoformat(),
        "last_status": "partial",
        "last_candidate_count": 2,
        "last_error": "one malformed item",
        "consecutive_failures": 1,
        "consecutive_empty": 0,
    }

    JsonDiscoveryState.record_health(state, "sec", STAMP, "success", 4)
    assert state["sources"]["sec"]["health"]["consecutive_failures"] == 0
    assert state["sources"]["sec"]["health"]["consecutive_empty"] == 0
    assert state["sources"]["sec"]["health"]["last_candidate_count"] == 4

    store.save(state, now=STAMP)
    assert store.load()["sources"]["sec"]["health"]["last_status"] == "success"


def test_health_tracks_consecutive_empty_results(tmp_path):
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    state = store.load()

    JsonDiscoveryState.record_health(state, "ripple", STAMP, "empty", 0)
    JsonDiscoveryState.record_health(state, "ripple", STAMP, "empty", 0)

    health = state["sources"]["ripple"]["health"]
    assert health["consecutive_empty"] == 2
    assert health["consecutive_failures"] == 0

    JsonDiscoveryState.record_health(state, "ripple", STAMP, "success", 1)
    assert state["sources"]["ripple"]["health"]["consecutive_empty"] == 0


def test_health_rejects_unknown_or_invalid_fields(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(
        '{"schema_version":1,"sources":{"sec":{"requests":{},'
        '"health":{"last_attempt":"bad","last_status":"failed","last_candidate_count":0,'
        '"last_error":"","consecutive_failures":1,"consecutive_empty":0}}},"candidates":{}}',
        encoding="utf-8",
    )
    with pytest.raises(DiscoveryStateError, match="invalid health"):
        JsonDiscoveryState(path).load()


def test_partial_does_not_increment_hard_failure_streak():
    state = {"schema_version": 1, "sources": {}, "candidates": {}}
    JsonDiscoveryState.record_health(state, "federal-register", STAMP, "partial", 3, "one term failed")
    JsonDiscoveryState.record_health(state, "federal-register", STAMP, "partial", 2, "another term failed")
    health = state["sources"]["federal-register"]["health"]
    assert health["consecutive_failures"] == 0
    assert health["consecutive_empty"] == 2
