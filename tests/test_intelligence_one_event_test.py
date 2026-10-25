from datetime import datetime, timedelta, timezone

import pytest

import main
from discord.publisher import DiscordSettings
from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState


NOW = datetime.now(timezone.utc)


def _event(event_id: str, *, published_at: str | None = None) -> IntelligenceEvent:
    stamp = (NOW - timedelta(days=1)).isoformat() if published_at is None else published_at
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=f"Verified event {event_id}",
        significance="A material institutional digital-asset development.",
        evidence=[
            Evidence(
                source_id="official-source",
                source="Official source",
                url=f"https://example.com/{event_id}",
                published_at=stamp,
                title=f"Official announcement {event_id}",
                source_quality="primary",
            )
        ],
    )


class _FakeWebhook:
    messages: list[str] = []

    def __init__(self, _url: str):
        pass

    def send(self, message: str) -> None:
        self.messages.append(message)


def _stores(tmp_path, monkeypatch):
    outbox = JsonIntelligenceOutboxState(tmp_path / "intelligence_outbox.json")
    history = JsonIntelligenceDeliveryHistory(tmp_path / "intelligence_delivery_history.json")
    monkeypatch.setattr(main, "DiscordWebhook", _FakeWebhook)
    # The production workflow uses the default state path; bind it to the isolated
    # test ledger so the real publisher and the postcondition assertions share state.
    monkeypatch.setattr(main, "JsonIntelligenceDeliveryHistory", lambda: history)
    _FakeWebhook.messages = []
    return outbox, history


def test_one_event_test_posts_only_selected_event_and_records_postconditions(tmp_path, monkeypatch):
    outbox, history = _stores(tmp_path, monkeypatch)
    outbox.enqueue([_event("evt-selected"), _event("evt-other")])
    settings = DiscordSettings(
        webhook_url="https://discord.com/api/webhooks/test/token",
        max_posts=5,
        dry_run=True,
    )

    posted = main.publish_one_intelligence_test_event(
        "evt-selected", settings, outbox_store=outbox, history_store=history
    )

    assert posted == 1
    assert len(_FakeWebhook.messages) == 1
    assert "evt-selected" not in _FakeWebhook.messages[0] or "Verified event evt-selected" in _FakeWebhook.messages[0]
    assert "Verified event evt-other" not in _FakeWebhook.messages[0]
    assert "evt-selected" in history.load()
    assert [item.event.event_id for item in outbox.load()] == ["evt-other"]


@pytest.mark.parametrize(
    "event_id,published_at,expected",
    [
        ("evt-stale", (NOW - timedelta(days=45)).isoformat(), "freshness window"),
        ("evt-undated", "", "freshness window"),
    ],
)
def test_one_event_test_refuses_stale_or_undated_evidence(
    tmp_path, monkeypatch, event_id, published_at, expected
):
    outbox, history = _stores(tmp_path, monkeypatch)
    outbox.enqueue([_event(event_id, published_at=published_at)])
    settings = DiscordSettings(
        webhook_url="https://discord.com/api/webhooks/test/token",
        dry_run=True,
    )

    with pytest.raises(SystemExit, match=expected):
        main.publish_one_intelligence_test_event(
            event_id, settings, outbox_store=outbox, history_store=history
        )

    assert _FakeWebhook.messages == []
    assert history.load() == set()
    assert [item.event.event_id for item in outbox.load()] == [event_id]


def test_one_event_test_refuses_ambiguous_or_already_delivered_target(tmp_path, monkeypatch):
    outbox, history = _stores(tmp_path, monkeypatch)
    event = _event("evt-duplicate")
    outbox.enqueue([event])
    # Simulate malformed ambiguous state without bypassing the outbox store's
    # uniqueness rules by substituting a queue double for the runtime read.
    class DuplicateQueue:
        def load(self):
            from storage.intelligence_outbox import QueuedIntelligence
            now = datetime.now(timezone.utc)
            return [
                QueuedIntelligence("intelligence|evt-duplicate", event, now),
                QueuedIntelligence("intelligence|evt-duplicate-2", event, now),
            ]

    settings = DiscordSettings(
        webhook_url="https://discord.com/api/webhooks/test/token",
        dry_run=True,
    )
    with pytest.raises(SystemExit, match="matches=2"):
        main.publish_one_intelligence_test_event(
            "evt-duplicate", settings, outbox_store=DuplicateQueue(), history_store=history
        )
    assert _FakeWebhook.messages == []

    history.record({"evt-duplicate"})
    with pytest.raises(SystemExit, match="already in delivery history"):
        main.publish_one_intelligence_test_event(
            "evt-duplicate", settings, outbox_store=outbox, history_store=history
        )
    assert _FakeWebhook.messages == []
