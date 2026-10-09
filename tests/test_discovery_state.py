from datetime import datetime, timedelta, timezone
import json

import pytest

from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def test_missing_discovery_state_starts_empty(tmp_path):
    assert JsonDiscoveryState(tmp_path / "discovery.json").load() == {
        "schema_version": 1, "sources": {}, "candidates": {}
    }


def test_valid_state_round_trips_validators_watermark_and_seen_times(tmp_path):
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    state = store.load()
    JsonDiscoveryState.record_success(state, "sec", STAMP,
                                      {"0000000001": {"etag": '"v1"',
                                                       "last_modified": "Wed, 23 Sep 2026 12:00:00 GMT"}},
                                      watermark={"0000000001": "2026-09-23:accession"},
                                      pagination={"next_issuer_index": 2})
    JsonDiscoveryState.observe_candidate(state, "sec:acc", "hash", STAMP)
    store.save(state, now=STAMP)
    loaded = store.load()
    assert JsonDiscoveryState.request_validators(loaded, "sec", "0000000001") == {
        "etag": '"v1"', "last_modified": "Wed, 23 Sep 2026 12:00:00 GMT"
    }
    assert loaded["sources"]["sec"]["watermark"]["0000000001"].endswith("accession")
    assert loaded["sources"]["sec"]["pagination"]["next_issuer_index"] == 2
    assert loaded["candidates"]["sec:acc"]["first_seen_time"] == STAMP.isoformat()


@pytest.mark.parametrize("content", ["{bad", "[]", '{"schema_version":1,"sources":[],"candidates":[]}'])
def test_corrupt_state_fails_closed_and_save_does_not_replace_it(tmp_path, content):
    path = tmp_path / "discovery.json"
    path.write_text(content, encoding="utf-8")
    store = JsonDiscoveryState(path)
    with pytest.raises(DiscoveryStateError):
        store.load()
    with pytest.raises(DiscoveryStateError):
        store.save(store.load())
    assert path.read_text(encoding="utf-8") == content


def test_save_is_atomic_deterministic_and_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "nested" / "discovery.json"
    store = JsonDiscoveryState(path)
    state = store.load()
    JsonDiscoveryState.observe_candidate(state, "z", "z-hash", STAMP)
    JsonDiscoveryState.observe_candidate(state, "a", "a-hash", STAMP)
    store.save(state, now=STAMP)
    saved = path.read_text(encoding="utf-8")
    assert saved.endswith("\n")
    assert list(json.loads(saved)["candidates"]) == ["a", "z"]
    assert not path.with_suffix(".json.tmp").exists()


def test_retention_uses_age_then_deterministic_maximum(tmp_path):
    store = JsonDiscoveryState(tmp_path / "discovery.json", max_candidates=2, retention_days=30)
    state = store.load()
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for candidate_id, stamp in (("old", old), ("b", STAMP), ("a", STAMP), ("c", STAMP)):
        JsonDiscoveryState.observe_candidate(state, candidate_id, candidate_id, stamp)
    store.save(state, now=STAMP)
    assert list(store.load()["candidates"]) == ["b", "c"]


def test_corrupt_file_is_preserved_when_valid_value_is_saved(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(DiscoveryStateError, match="malformed JSON"):
        JsonDiscoveryState(path).save({"schema_version": 1, "sources": {}, "candidates": {}})
    assert path.read_text(encoding="utf-8") == "not-json"


def test_health_tracks_status_duration_and_resets_on_recovery(tmp_path):
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    state = store.load()
    failed_at = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    retry_at = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    JsonDiscoveryState.record_health(state, "swift", failed_at, "failed", 0, "HTTP 403")
    JsonDiscoveryState.record_health(state, "swift", retry_at, "failed", 0, "HTTP 403")
    assert state["sources"]["swift"]["health"]["status_started_at"] == failed_at.isoformat()
    JsonDiscoveryState.record_health(state, "swift", retry_at, "success", 3)
    assert state["sources"]["swift"]["health"]["status_started_at"] == retry_at.isoformat()
    assert state["sources"]["swift"]["health"]["consecutive_failures"] == 0


def test_legacy_health_derives_status_started_at(tmp_path):
    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {
            "swift": {
                "requests": {},
                "health": {
                    "last_attempt": "2026-09-23T12:00:00+00:00",
                    "last_status": "failed",
                    "last_candidate_count": 0,
                    "last_error": "HTTP 403",
                    "consecutive_failures": 4,
                    "consecutive_empty": 0,
                },
            }
        },
        "candidates": {},
    }), encoding="utf-8")
    loaded = JsonDiscoveryState(path).load()
    assert loaded["sources"]["swift"]["health"]["status_started_at"] == "2026-09-23T12:00:00+00:00"



def test_partial_with_candidates_resets_empty_streak_but_remains_partial(tmp_path):
    state = JsonDiscoveryState(tmp_path / "discovery.json").load()
    empty_at = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    partial_at = empty_at + timedelta(hours=1)
    retry_at = partial_at + timedelta(hours=1)

    JsonDiscoveryState.record_health(state, "visa", empty_at, "empty", 0, "no dated articles")
    assert state["sources"]["visa"]["health"]["consecutive_empty"] == 1

    JsonDiscoveryState.record_health(state, "visa", partial_at, "partial", 18, "one malformed article")
    health = state["sources"]["visa"]["health"]
    assert health["last_status"] == "partial"
    assert health["last_candidate_count"] == 18
    assert health["consecutive_empty"] == 0
    assert health["consecutive_failures"] == 0
    assert health["status_started_at"] == partial_at.isoformat()

    JsonDiscoveryState.record_health(state, "visa", retry_at, "partial", 15, "one malformed article")
    health = state["sources"]["visa"]["health"]
    assert health["last_status"] == "partial"
    assert health["consecutive_empty"] == 0
    assert health["status_started_at"] == partial_at.isoformat()


def test_partial_without_candidates_counts_as_empty(tmp_path):
    state = JsonDiscoveryState(tmp_path / "discovery.json").load()
    JsonDiscoveryState.record_health(
        state, "mastercard", STAMP, "partial", 0, "article dates unavailable"
    )
    health = state["sources"]["mastercard"]["health"]
    assert health["last_status"] == "partial"
    assert health["consecutive_empty"] == 1
