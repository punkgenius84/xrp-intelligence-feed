from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import main

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
        "consecutive_failures": 0,
        "consecutive_empty": 1,
        "status_started_at": STAMP.isoformat(),
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


def test_persistently_unhealthy_source_emits_warning_after_24_hours(capsys):
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    started = (now - timedelta(hours=25)).isoformat()
    result = SimpleNamespace(
        discovery_results=[SimpleNamespace(source_id="sec-edgar")],
        health={"sec-edgar": {
            "last_status": "failed",
            "status_started_at": started,
            "last_attempt": started,
            "last_error": "HTTP timeout",
        }},
    )

    main.print_source_health_warnings(result, now=now)

    output = capsys.readouterr().out
    assert "Source health — sec-edgar is failing" in output
    assert "25.0h" in output
    assert "HTTP timeout" in output


def test_recent_source_failure_does_not_emit_persistent_health_warning(capsys):
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    started = (now - timedelta(hours=2)).isoformat()
    result = SimpleNamespace(
        discovery_results=[SimpleNamespace(source_id="ripple")],
        health={"ripple": {
            "last_status": "empty",
            "status_started_at": started,
            "last_attempt": started,
            "last_error": "",
        }},
    )

    main.print_source_health_warnings(result, now=now)

    assert capsys.readouterr().out == ""



def test_relevance_audit_reports_per_source_yield_including_zero_yield_sources(monkeypatch, capsys):
    from types import SimpleNamespace

    items = [
        SimpleNamespace(
            source_id="low-yield-source", source="Low yield", relevance_score=0,
            source_quality="discovery", title="Low-scoring item", score_reasons=[],
            detected_entities=[], published_at=STAMP, collected_at=STAMP,
        ),
        SimpleNamespace(
            source_id="high-yield-source", source="High yield", relevance_score=0,
            source_quality="primary", title="High-scoring item", score_reasons=[],
            detected_entities=["XRP"], published_at=STAMP, collected_at=STAMP,
        ),
    ]
    scores = {"low-yield-source": 18, "high-yield-source": 76}
    monkeypatch.setattr(main, "detect_entities", lambda item: [])
    monkeypatch.setattr(main, "classify_source_quality", lambda item: item.source_quality)

    def score(item):
        item.relevance_score = scores[item.source_id]
        item.score_reasons = ["fixture relevance reason"]
        return item.relevance_score, item.score_reasons

    monkeypatch.setattr(main, "score_relevance", score)

    main._score_observed_relevance(items, publish_score=50)

    output = capsys.readouterr().out
    assert "Relevance audit source yield" in output
    assert "low-yield-source: observed=1 qualifying=0 best_score=18 primary=0" in output
    assert "high-yield-source: observed=1 qualifying=1 best_score=76 primary=1" in output
