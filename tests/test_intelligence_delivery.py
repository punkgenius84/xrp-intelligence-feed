from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import main
import intelligence.configuration as intelligence_configuration
from discord.intelligence import format_intelligence_event
from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from storage.intelligence_outbox import (
    MAX_INTELLIGENCE_OUTBOX_ENTRIES,
    IntelligenceOutboxError,
    JsonIntelligenceOutboxState,
    intelligence_publication_key,
)


def event(event_id="evt-1", summary="Summary"):
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=summary,
        significance="Significance",
        evidence=[
            Evidence(
                source_id="source-1",
                source="Official source",
                url="https://example.com/source",
                published_at="2026-10-01T12:00:00+00:00",
                title="Source title",
                source_quality="primary",
            )
        ],
    )


def test_intelligence_outbox_round_trip_and_deterministic_key(tmp_path):
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    queued = store.enqueue([event()])
    assert queued[0].key == intelligence_publication_key(event())
    loaded = store.load()
    assert loaded[0].event.summary == "Summary"


def test_intelligence_outbox_updates_queued_event_without_duplicate(tmp_path):
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    store.enqueue([event(summary="old")])
    store.enqueue([event(summary="new")])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].event.summary == "new"


def test_intelligence_outbox_remove_is_idempotent(tmp_path):
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    queued = store.enqueue([event()])
    store.remove({queued[0].key})
    store.remove({queued[0].key})
    assert store.load() == []


def test_intelligence_outbox_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "intelligence_outbox.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception):
        JsonIntelligenceOutboxState(path).load()


def test_intelligence_formatter_escapes_markdown_and_mentions():
    value = format_intelligence_event(
        IntelligenceEvent(
            event_id="evt-1",
            event_type="announcement",
            summary="*bold* @everyone _raw_",
            significance="code > quote",
            entities=["@here"],
            evidence=[
                Evidence(
                    source_id="source-1",
                    source="Official *source*",
                    url="https://example.com/source",
                    published_at="2026-10-01T12:00:00+00:00",
                    title="Title @everyone",
                    source_quality="primary",
                )
            ],
        )
    )
    assert "\\*bold\\*" in value
    assert "@\u200b" in value
    assert "\\>" in value


def test_partial_source_health_warning_is_labeled_partial(capsys):
    result = main.PipelineResult(
        discovery_results=[SimpleNamespace(source_id="example")],
        health={
            "example": {
                "last_status": "partial",
                "status_started_at": "2026-09-22T12:00:00+00:00",
                "last_attempt": "2026-09-23T12:00:00+00:00",
                "last_error": "one page failed",
            }
        },
    )
    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )
    output = capsys.readouterr().out
    assert "partially failing" in output
    assert "empty" not in output


def test_score_feed_respects_explicit_empty_publishable(monkeypatch):
    monkeypatch.setattr(
        intelligence_configuration,
        "read_object",
        lambda path, label: {"publish_score": 50, "weights": {
            "high_keyword": 1, "medium_keyword": 1, "context_keyword": 1,
            "primary_source_bonus": 1, "multiple_entity_bonus": 1,
            "title_match_bonus": 1, "institutional_source_bonus": 1,
        }},
    )
    result = main.PipelineResult(
        fresh=[SimpleNamespace(relevance_score=90)],
        publishable=[],
    )
    assert main.score_feed(result) == []


def test_intelligence_publish_uses_outbox_and_removes_successful(tmp_path, monkeypatch):
    queued_event = event()
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    settings = SimpleNamespace(
        dry_run=False,
        webhook_url="https://discord.com/api/webhooks/test/token",
        max_posts=5,
    )
    sent = []

    class FakeWebhook:
        def __init__(self, url):
            self.url = url

        def send(self, message):
            sent.append(message)

    monkeypatch.setattr(main, "DiscordWebhook", FakeWebhook)
    assert main.publish_intelligence_events(
        [queued_event],
        settings,
        publish_enabled=True,
        outbox_store=store,
    ) == 1
    assert len(sent) == 1
    assert store.load() == []


def test_intelligence_outbox_capacity_overflow_refuses_to_drop(tmp_path):
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    events = [event(event_id=f"evt-{index}") for index in range(MAX_INTELLIGENCE_OUTBOX_ENTRIES + 1)]
    with pytest.raises(IntelligenceOutboxError, match="refusing to drop undelivered intelligence events"):
        store.enqueue(events)
    assert not (tmp_path / "intelligence_outbox.json").exists()


def test_intelligence_outbox_accepts_exact_capacity(tmp_path):
    store = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    events = [event(event_id=f"evt-{index}") for index in range(MAX_INTELLIGENCE_OUTBOX_ENTRIES)]
    store.enqueue(events)
    assert len(store.load()) == MAX_INTELLIGENCE_OUTBOX_ENTRIES
