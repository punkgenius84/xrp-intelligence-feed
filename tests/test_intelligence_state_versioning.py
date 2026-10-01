import json

from storage.intelligence_state import JsonIntelligenceState


def test_state_migrates_schema_v1_records(tmp_path):
    path = tmp_path / "intelligence.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "events": {
                    "evt-1": {
                        "event_id": "evt-1",
                        "summary": "Original",
                        "updated_at": "2026-10-01T00:00:00Z",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    state = JsonIntelligenceState(path).load()

    assert state["schema_version"] == 2
    assert state["events"]["evt-1"]["status"] == "active"
    assert state["events"]["evt-1"]["revision"] == 1
    assert state["events"]["evt-1"]["history"] == []


def test_upsert_records_bounded_revision_history(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()

    JsonIntelligenceState.upsert(
        state,
        "evt-1",
        {"event_id": "evt-1", "summary": "First"},
        "2026-10-01T00:00:00Z",
    )
    JsonIntelligenceState.upsert(
        state,
        "evt-1",
        {"event_id": "evt-1", "summary": "Second"},
        "2026-10-01T01:00:00Z",
    )

    record = state["events"]["evt-1"]
    assert record["revision"] == 2
    assert record["status"] == "active"
    assert record["history"][0]["event"]["summary"] == "First"
    assert record["history"][0]["revision"] == 1


def test_identical_upsert_does_not_create_fake_revision(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()

    event = {"event_id": "evt-1", "summary": "Same"}
    JsonIntelligenceState.upsert(state, "evt-1", event, "2026-10-01T00:00:00Z")
    JsonIntelligenceState.upsert(state, "evt-1", event, "2026-10-01T01:00:00Z")

    record = state["events"]["evt-1"]
    assert record["revision"] == 1
    assert record["history"] == []
    assert record["updated_at"] == "2026-10-01T01:00:00Z"


def test_mark_superseded_preserves_old_event(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()

    JsonIntelligenceState.upsert(
        state,
        "evt-old",
        {"event_id": "evt-old", "summary": "Single-source event"},
        "2026-10-01T00:00:00Z",
    )
    JsonIntelligenceState.mark_superseded(
        state,
        "evt-old",
        "evt-cluster-new",
        "2026-10-01T02:00:00Z",
    )

    record = state["events"]["evt-old"]
    assert record["status"] == "superseded"
    assert record["superseded_by"] == ["evt-cluster-new"]
    assert record["revision"] == 2
    assert record["history"][0]["event"]["summary"] == "Single-source event"


def test_mark_superseded_is_idempotent_for_same_replacement(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()

    JsonIntelligenceState.upsert(
        state,
        "evt-old",
        {"event_id": "evt-old", "summary": "Event"},
        "2026-10-01T00:00:00Z",
    )
    JsonIntelligenceState.mark_superseded(
        state, "evt-old", "evt-new", "2026-10-01T01:00:00Z"
    )
    JsonIntelligenceState.mark_superseded(
        state, "evt-old", "evt-new", "2026-10-01T02:00:00Z"
    )

    record = state["events"]["evt-old"]
    assert record["superseded_by"] == ["evt-new"]
    assert record["revision"] == 2
