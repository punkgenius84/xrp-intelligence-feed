from datetime import datetime, timezone

import pytest

from intelligence.configuration import load_intelligence_config
from intelligence.events import event_from_dict, event_to_dict
from intelligence.llm.schemas import Claim
from intelligence.pipeline import cluster_event_id, enrich_clusters, event_id_for, select_items
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
        conflicts=["Source A says planned; Source B says launched."],
        uncertainties=["The article does not establish implementation timing."],
        member_ids=["a", "b"],
        supersedes=["evt-a", "evt-b"],
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


def test_run_pipeline_enriches_only_when_enabled(monkeypatch, tmp_path):
    import main
    from intelligence.llm.base import LLMResponse
    from storage.correlation_state import JsonCorrelationState
    from storage.database import JsonState

    class FakeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def generate(self, **kwargs):
            return LLMResponse(
                '{"event_type":"announcement","event_summary":"Source-backed event",'
                '"significance":"Relevant to the monitored domain.",'
                '"entities":["Ripple"],"claims":[{"text":"The source reports an announcement.",'
                '"certainty":"high","evidence":["source-1 summary"]}]}',
                model="test-model",
            )

    class FakeCollector:
        def __init__(self, source):
            self.last_report = None

        def collect(self):
            return [NewsItem(
                title="Ripple announces institutional payments partnership",
                url="https://example.test/ripple-partnership",
                source="Example",
                source_id="example",
                summary="Ripple announced a partnership with an institutional payments provider.",
                published_at=datetime.now(timezone.utc),
            )]

    monkeypatch.setenv("INTELLIGENCE_ENABLED", "true")
    monkeypatch.setenv("INTELLIGENCE_MIN_SCORE", "0")
    monkeypatch.setenv("INTELLIGENCE_MAX_ITEMS", "1")
    monkeypatch.setattr(main, "OllamaProvider", FakeProvider)
    monkeypatch.setattr(main, "RSSCollector", FakeCollector)

    intelligence_store = JsonIntelligenceState(tmp_path / "intelligence.json")
    correlation_store = JsonCorrelationState(tmp_path / "correlation.json")
    result = main.run_pipeline(
        sources=[{"source_id": "example", "collection_type": "rss"}],
        state=JsonState(tmp_path / "seen.json"),
        correlation_state=correlation_store,
        intelligence_state=intelligence_store,
    )
    assert len(result.intelligence_events) == 1
    assert result.intelligence_events[0].summary == "Source-backed event"
    persisted = intelligence_store.load()
    assert len(persisted["events"]) == 1


def test_run_pipeline_keeps_intelligence_disabled_by_default(monkeypatch, tmp_path):
    import main
    from storage.correlation_state import JsonCorrelationState
    from storage.database import JsonState

    monkeypatch.delenv("INTELLIGENCE_ENABLED", raising=False)
    monkeypatch.setattr(
        main,
        "OllamaProvider",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Ollama must not be constructed when intelligence is disabled")
        ),
    )

    result = main.run_pipeline(
        sources=[],
        state=JsonState(tmp_path / "seen.json"),
        correlation_state=JsonCorrelationState(tmp_path / "correlation.json"),
        intelligence_state=JsonIntelligenceState(tmp_path / "intelligence.json"),
    )
    assert result.intelligence_events == []
    assert result.intelligence_failures == []


def test_cluster_enrichment_creates_one_event_with_all_evidence():
    config = load_intelligence_config().__class__(
        enabled=True,
        model="test-model",
        max_items_per_run=5,
        min_relevance_score=50,
        timeout_seconds=5,
    )
    first = item(90, "a")
    second = item(80, "b")
    first.source_id = "source-a"
    second.source_id = "source-b"
    first.source = "Source A"
    second.source = "Source B"
    first.detected_entities = ["Ripple", "DBS"]
    second.detected_entities = ["Ripple", "DBS"]
    first.correlated_candidate_ids = ["b"]
    second.correlated_candidate_ids = ["a"]

    class Provider:
        def generate(self, **kwargs):
            from intelligence.llm.base import LLMResponse

            return LLMResponse(
                '{"event_type":"partnership","event_summary":"Shared event",'
                '"significance":"Two independent sources report the same event.",'
                '"entities":["Ripple","DBS"],"claims":[],"uncertainties":[]}',
                model="test-model",
            )

    events, failures = enrich_clusters([first, second], Provider(), config)

    assert failures == []
    assert len(events) == 1
    assert events[0].event_id == cluster_event_id(
        __import__("intelligence.event_clustering", fromlist=["EventCluster"]).EventCluster(
            cluster_id="ignored",
            members=(first, second),
        )
    )
    assert events[0].member_ids == ["a", "b"]
    assert events[0].supersedes == [event_id_for(first), event_id_for(second)]
    assert {e.source_id for e in events[0].evidence} == {"source-a", "source-b"}


def test_intelligence_state_stales_from_evidence_not_updated_at(tmp_path):
    from datetime import timedelta

    state = JsonIntelligenceState(tmp_path / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        state,
        "evt-old",
        {"summary": "old", "evidence": [{
            "source_id": "example", "source": "Example",
            "url": "https://example.test/old",
            "published_at": "2026-09-01T00:00:00+00:00",
            "title": "Old", "source_quality": "primary"
        }]},
        "2026-10-01T00:00:00+00:00",
    )
    found = JsonIntelligenceState.mark_stale_older_than(
        state, now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        max_age=timedelta(days=14),
    )
    assert found == ["evt-old"]
    assert state["events"]["evt-old"]["status"] == "stale"


def test_intelligence_state_keeps_recent_evidence_active(tmp_path):
    from datetime import timedelta

    state = JsonIntelligenceState(tmp_path / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        state,
        "evt-recent",
        {"summary": "recent", "evidence": [{
            "source_id": "example", "source": "Example",
            "url": "https://example.test/recent",
            "published_at": "2026-10-01T00:00:00+00:00",
            "title": "Recent", "source_quality": "primary"
        }]},
        "2026-10-01T12:00:00+00:00",
    )
    assert JsonIntelligenceState.mark_stale_older_than(
        state, now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        max_age=timedelta(days=14),
    ) == []
    assert state["events"]["evt-recent"]["status"] == "active"


def test_intelligence_state_leaves_superseded_events_alone(tmp_path):
    from datetime import timedelta

    state = JsonIntelligenceState(tmp_path / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        state,
        "evt-old",
        {"summary": "old", "evidence": [{
            "source_id": "example", "source": "Example",
            "url": "https://example.test/old",
            "published_at": "2026-09-01T00:00:00+00:00",
            "title": "Old", "source_quality": "primary"
        }]},
        "2026-09-01T00:00:00+00:00",
    )
    JsonIntelligenceState.mark_superseded(
        state, "evt-old", "evt-new", "2026-10-01T00:00:00+00:00"
    )
    assert JsonIntelligenceState.mark_stale_older_than(
        state, now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        max_age=timedelta(days=14),
    ) == []
    assert state["events"]["evt-old"]["status"] == "superseded"


def test_intelligence_state_skips_malformed_evidence_dates(tmp_path):
    from datetime import timedelta

    state = JsonIntelligenceState(tmp_path / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        state,
        "evt-unknown-age",
        {"summary": "unknown", "evidence": [{
            "source_id": "example", "source": "Example",
            "url": "https://example.test/unknown",
            "published_at": "not-a-date",
            "title": "Unknown", "source_quality": "primary"
        }]},
        "2026-09-01T00:00:00+00:00",
    )
    assert JsonIntelligenceState.mark_stale_older_than(
        state, now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        max_age=timedelta(days=14),
    ) == []
    assert state["events"]["evt-unknown-age"]["status"] == "active"


def test_intelligence_state_stale_sweep_is_idempotent(tmp_path):
    from datetime import timedelta

    state = JsonIntelligenceState(tmp_path / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        state,
        "evt-old",
        {"summary": "old", "evidence": [{
            "source_id": "example", "source": "Example",
            "url": "https://example.test/old",
            "published_at": "2026-09-01T00:00:00+00:00",
            "title": "Old", "source_quality": "primary"
        }]},
        "2026-09-01T00:00:00+00:00",
    )
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    assert JsonIntelligenceState.mark_stale_older_than(
        state, now=now, max_age=timedelta(days=14)
    ) == ["evt-old"]
    assert JsonIntelligenceState.mark_stale_older_than(
        state, now=now, max_age=timedelta(days=14)
    ) == []
