from datetime import datetime, timezone

import pytest

from intelligence.configuration import load_intelligence_config
from intelligence.events import event_from_dict, event_to_dict
from intelligence.llm.schemas import Claim
from intelligence.pipeline import event_id_for, select_items
from models import NewsItem
from storage.intelligence_state import JsonIntelligenceState, IntelligenceStateError


def item(score, candidate):
    return NewsItem(
        title=candidate,
        url=f"https://example.test/{candidate}",
        source="Example",
        relevance_score=score,
        candidate_id=candidate,
        published_at=datetime.now(timezone.utc),
    )


def test_select_items_is_bounded_and_score_ordered():
    config = load_intelligence_config()
    config = config.__class__(
        enabled=False,
        model=config.model,
        max_items_per_run=2,
        min_relevance_score=50,
        timeout_seconds=45,
    )
    selected = select_items(
        [item(55, "a"), item(90, "b"), item(70, "c"), item(20, "d")],
        config,
    )
    assert [x.candidate_id for x in selected] == ["b", "c"]


def test_event_id_is_stable():
    assert event_id_for(item(80, "abc")) == event_id_for(item(20, "abc"))


def test_event_round_trip_serialization():
    from intelligence.evidence import Evidence
    from intelligence.events import IntelligenceEvent

    event = IntelligenceEvent(
        event_id="evt-1",
        event_type="partnership",
        summary="A partnership was announced.",
        significance="Potentially relevant.",
        entities=["Ripple"],
        claims=[
            Claim(
                text="The source reports a partnership.",
                claim_type="reported_fact",
                certainty="high",
                evidence=["summary"],
            )
        ],
        uncertainties=["The article does not establish implementation timing."],
        evidence=[
            Evidence(
                source_id="example",
                source="Example",
                url="https://example.test/article",
                published_at="2026-10-01T00:00:00+00:00",
                title="Example",
                source_quality="primary",
            )
        ],
        model="test-model",
    )
    assert event_from_dict(event_to_dict(event)) == event


def test_event_round_trip_rejects_malformed_evidence():
    with pytest.raises(ValueError, match="evidence fields"):
        event_from_dict(
            {
                "event_id": "evt-1",
                "event_type": "other",
                "summary": "x",
                "significance": "y",
                "claims": [],
                "entities": [],
                "uncertainties": [],
                "evidence": [{"source_id": "example"}],
                "model": "test",
            }
        )


def test_intelligence_state_round_trip_and_bound(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()
    state["events"] = {
        str(i): {"updated_at": f"2026-10-{i:02d}"}
        for i in range(1, 502)
    }
    store.save(state)
    loaded = store.load()
    assert len(loaded["events"]) == 500


def test_corrupt_intelligence_state_fails_closed(tmp_path):
    path = tmp_path / "intelligence.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(IntelligenceStateError):
        JsonIntelligenceState(path).load()


def test_intelligence_state_rejects_missing_updated_at(tmp_path):
    path = tmp_path / "intelligence.json"
    path.write_text(
        '{"schema_version":1,"events":{"evt-1":{"summary":"x"}}}',
        encoding="utf-8",
    )
    with pytest.raises(IntelligenceStateError):
        JsonIntelligenceState(path).load()


def test_intelligence_state_upsert_replaces_same_event(tmp_path):
    store = JsonIntelligenceState(tmp_path / "intelligence.json")
    state = store.load()
    JsonIntelligenceState.upsert(
        state,
        "evt-1",
        {"summary": "first"},
        "2026-10-01T00:00:00Z",
    )
    JsonIntelligenceState.upsert(
        state,
        "evt-1",
        {"summary": "updated"},
        "2026-10-01T01:00:00Z",
    )
    store.save(state)
    loaded = store.load()
    assert len(loaded["events"]) == 1
    assert loaded["events"]["evt-1"]["summary"] == "updated"


def test_enrichment_failure_does_not_abort_other_items():
    from intelligence.pipeline import enrich_items

    config = load_intelligence_config().__class__(
        enabled=True,
        model="test-model",
        max_items_per_run=2,
        min_relevance_score=50,
        timeout_seconds=5,
    )

    class Provider:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            from intelligence.llm.base import LLMResponse, LLMUnavailable

            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    '{"event_type":"announcement","event_summary":"First","significance":"Source-backed","claims":[]}',
                    model="test-model",
                )
            raise LLMUnavailable("offline")

    events, failures = enrich_items(
        [item(90, "first"), item(80, "second")],
        Provider(),
        config,
    )
    assert len(events) == 1
    assert events[0].summary == "First"
    assert len(failures) == 1
