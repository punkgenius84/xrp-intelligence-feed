from types import SimpleNamespace

import pytest

import main


def test_main_catches_pipeline_value_error_like_registry_errors(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings_from_env",
        lambda: SimpleNamespace(dry_run=False),
    )

    def fail():
        raise ValueError("bad discovery configuration")

    monkeypatch.setattr(main, "run_pipeline", fail)

    with pytest.raises(SystemExit, match="Pipeline configuration error: bad discovery configuration"):
        main.main()


def test_main_keeps_discovery_registry_errors_labeled_as_discovery_configuration(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings_from_env",
        lambda: SimpleNamespace(dry_run=False),
    )

    def fail():
        raise main.DiscoveryRegistryError("bad discovery registry")

    monkeypatch.setattr(main, "run_pipeline", fail)

    with pytest.raises(SystemExit, match="Discovery configuration error: bad discovery registry"):
        main.main()


def test_source_health_warning_after_24_hours(capsys):
    from datetime import datetime, timezone
    result = main.PipelineResult(
        discovery_results=[SimpleNamespace(source_id="swift")],
        health={
            "swift": {
                "last_status": "failed",
                "status_started_at": "2026-09-22T12:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "HTTP 403",
            }
        },
    )
    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )
    output = capsys.readouterr().out
    assert "swift is failing" in output
    assert "24.0h" in output
    assert "HTTP 403" in output


def test_source_health_warning_ignores_recent_or_healthy_sources(capsys):
    from datetime import datetime, timezone
    result = main.PipelineResult(
        discovery_results=[
            SimpleNamespace(source_id="recent"),
            SimpleNamespace(source_id="healthy"),
        ],
        health={
            "recent": {
                "last_status": "empty",
                "status_started_at": "2026-09-23T01:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "",
            },
            "healthy": {
                "last_status": "success",
                "status_started_at": "2026-09-22T00:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "",
            },
        },
    )
    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )
    assert capsys.readouterr().out == ""


def test_intelligence_publish_requires_evidence_url():
    from intelligence.events import IntelligenceEvent
    from types import SimpleNamespace
    event = IntelligenceEvent(
        event_id="evt-1",
        event_type="announcement",
        summary="Test event",
        significance="Test significance",
        evidence=[],
    )
    settings = SimpleNamespace(dry_run=False, webhook_url="https://discord.com/api/webhooks/test/token")
    with pytest.raises(SystemExit, match="evidence URL"):
        main.publish_intelligence_events([event], settings, publish_enabled=True)


def test_intelligence_publish_posts_to_existing_channel(monkeypatch):
    from intelligence.events import IntelligenceEvent
    from intelligence.evidence import Evidence
    from types import SimpleNamespace

    sent = []

    class FakeWebhook:
        def __init__(self, url):
            self.url = url
        def send(self, message):
            sent.append(message)

    event = IntelligenceEvent(
        event_id="evt-1",
        event_type="announcement",
        summary="Test event",
        significance="Test significance",
        evidence=[
            Evidence(
                source_id="source-1",
                source="Official source",
                url="https://example.com/source",
                published_at="2026-09-23T12:00:00+00:00",
                title="Source title",
                source_quality="primary",
            )
        ],
    )
    monkeypatch.setattr(main, "DiscordWebhook", FakeWebhook)
    settings = SimpleNamespace(dry_run=False, webhook_url="https://discord.com/api/webhooks/test/token")
    assert main.publish_intelligence_events([event], settings, publish_enabled=True) == 1
    assert sent
    assert "https://example.com/source" in sent[0]


def test_discovery_success_persists_without_validators_or_pagination():
    result = SimpleNamespace(status="success", candidates=[object()], state_updates={}, pagination={})
    assert main._should_persist_discovery_progress(result) is True


def test_discovery_empty_persists_without_validators_or_pagination():
    result = SimpleNamespace(status="empty", candidates=[], state_updates={}, pagination={})
    assert main._should_persist_discovery_progress(result) is True


def test_discovery_failed_does_not_count_as_successful_progress():
    result = SimpleNamespace(status="failed", candidates=[], state_updates={}, pagination={})
    assert main._should_persist_discovery_progress(result) is False


def test_publish_feed_passes_intelligence_outbox_for_retry_delivery(monkeypatch):
    settings = SimpleNamespace(dry_run=False, webhook_url="https://discord.com/api/webhooks/test/token")
    sentinel_outbox = object()
    captured = {}

    monkeypatch.setattr(main, "publish", lambda items, settings: SimpleNamespace(posted_keys=[]))
    monkeypatch.setattr(
        main,
        "load_intelligence_config",
        lambda: SimpleNamespace(enabled=True, publish_enabled=True),
    )

    def capture(events, settings, *, publish_enabled, outbox_store=None):
        captured["events"] = events
        captured["publish_enabled"] = publish_enabled
        captured["outbox_store"] = outbox_store
        return 0

    monkeypatch.setattr(main, "publish_intelligence_events", capture)
    main.publish_feed([], [], settings, None, sentinel_outbox)

    assert captured == {
        "events": [],
        "publish_enabled": True,
        "outbox_store": sentinel_outbox,
    }


def test_source_health_warning_uses_github_actions_annotation(capsys):
    from datetime import datetime, timezone

    result = main.PipelineResult(
        discovery_results=[SimpleNamespace(source_id="swift")],
        health={
            "swift": {
                "last_status": "failed",
                "status_started_at": "2026-09-22T12:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "HTTP 403",
            }
        },
    )

    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )

    assert "::warning::Source health — swift is failing" in capsys.readouterr().out


def test_unexpected_rss_source_exception_isolated_from_other_pipeline_work(monkeypatch, tmp_path):
    from storage.correlation_state import JsonCorrelationState
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    source = {
        "source_id": "rss-boom",
        "name": "RSS boom",
        "url": "https://example.test/feed",
        "authority_tier": 1,
        "category": "regulatory",
        "entity_coverage": ["XRP"],
        "collection_type": "rss",
    }

    def explode(self):
        raise RuntimeError("feedparser internals exploded")

    monkeypatch.setattr(main.RSSCollector, "collect", explode)
    monkeypatch.setattr(
        main,
        "load_intelligence_config",
        lambda: SimpleNamespace(enabled=False),
    )

    result = main.run_pipeline(
        sources=[source],
        discovery_sources=[],
        state=main._NoSaveState(JsonState(tmp_path / "seen.json")),
        correlation_state=main._NoSaveState(JsonCorrelationState(tmp_path / "correlation.json")),
        discovery_state=main._NoSaveState(JsonDiscoveryState(tmp_path / "discovery.json")),
    )

    assert result.collected == []
    assert result.failures == ["rss-boom: unexpected collection error: feedparser internals exploded"]


def test_score_feed_reports_shadow_intelligence_eligibility(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "load_intelligence_config",
        lambda: SimpleNamespace(
            enabled=True,
            min_relevance_score=35,
            max_items_per_run=5,
        ),
    )
    result = main.PipelineResult()
    main.score_feed(result)
    output = capsys.readouterr().out
    assert "Intelligence eligibility: 0 fresh candidates; 0 meet minimum relevance score 35; 0 selected (limit 5)" in output
    assert "no fresh candidates were available for enrichment" in output


def test_score_feed_shows_fresh_candidates_below_intelligence_minimum(monkeypatch, capsys):
    item = SimpleNamespace(
        relevance_score=20,
        title="Fresh low-score candidate",
        source="Example source",
        score_reasons=["no high-value entity match", "context keyword only"],
    )
    monkeypatch.setattr(
        main,
        "load_intelligence_config",
        lambda: SimpleNamespace(
            enabled=True,
            min_relevance_score=35,
            max_items_per_run=5,
        ),
    )
    result = main.PipelineResult(
        collected=[item],
        fresh=[item],
        publishable=[],
    )

    main.score_feed(result)
    output = capsys.readouterr().out
    assert "Intelligence below-minimum candidates (showing 1 of 1; capped at 5):" in output
    assert "[20] Fresh low-score candidate — Example source" in output
    assert "no high-value entity match" in output
    assert "context keyword only" in output

