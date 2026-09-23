from datetime import datetime, timezone
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
