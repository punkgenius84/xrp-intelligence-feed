from datetime import datetime, timezone

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from scripts.quarantine_stale_intelligence_queue import (
    CONFIRMATION, REASON, evidence_freshness, find_stale_items, quarantine_stale_queue,
)
from storage.intelligence_outbox import JsonIntelligenceOutboxState, QueuedIntelligence
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState


NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
QUEUED_AT = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def event(event_id: str, published_at: str = "2026-10-20T11:00:00+00:00") -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=f"Summary for {event_id}",
        significance="Potentially relevant institutional development",
        evidence=[
            Evidence(
                source_id="official-source",
                source="Official Source",
                url="https://example.com/official-release",
                published_at=published_at,
                title=f"Official announcement for {event_id}",
                source_quality="primary",
            )
        ],
    )


def stores(tmp_path):
    outbox_path = tmp_path / "intelligence_outbox.json"
    quarantine_path = tmp_path / "intelligence_quarantine.json"
    return (
        JsonIntelligenceOutboxState(outbox_path),
        JsonIntelligenceQuarantineState(quarantine_path),
        outbox_path,
        quarantine_path,
    )


def test_preview_identifies_stale_items_without_changing_either_store(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([
        event("evt-stale", "2026-09-01T00:00:00+00:00"),
        event("evt-fresh", "2026-10-10T00:00:00+00:00"),
        event("evt-undated", ""),
    ], queued_at=QUEUED_AT)

    result = quarantine_stale_queue(
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        now=NOW,
    )

    assert result == {
        "pending_before": 3,
        "stale_count": 1,
        "pending_after": 3,
        "quarantine_before": 0,
        "applied": False,
    }
    assert {item.event.event_id for item in outbox.load()} == {
        "evt-stale", "evt-fresh", "evt-undated"
    }
    assert quarantine.load() == []


def test_apply_archives_and_removes_only_stale_events(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([
        event("evt-stale", "2026-09-01T00:00:00+00:00"),
        event("evt-fresh", "2026-10-10T00:00:00+00:00"),
        event("evt-undated", None),
        event("evt-future", "2026-11-22T00:00:00+00:00"),
    ], queued_at=QUEUED_AT)

    result = quarantine_stale_queue(
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        apply=True,
        confirmation=CONFIRMATION,
        now=NOW,
    )

    assert result["pending_before"] == 4
    assert result["stale_count"] == 1
    assert result["pending_after"] == 3
    assert result["quarantine_after"] == 1
    assert {item.event.event_id for item in outbox.load()} == {
        "evt-fresh", "evt-undated", "evt-future"
    }
    archived = quarantine.load()
    assert [item.event.event_id for item in archived] == ["evt-stale"]
    assert archived[0].reason == REASON.format(days=30)
    assert archived[0].queued_at == QUEUED_AT


def test_apply_requires_exact_confirmation_and_changes_nothing(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-stale", "2026-09-01T00:00:00+00:00")], queued_at=QUEUED_AT)

    with pytest.raises(ValueError, match="requires --confirm"):
        quarantine_stale_queue(
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            apply=True,
            confirmation="yes",
            now=NOW,
        )

    assert len(outbox.load()) == 1
    assert quarantine.load() == []


def test_existing_quarantine_collision_refuses_to_remove_active_event(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-stale", "2026-09-01T00:00:00+00:00")], queued_at=QUEUED_AT)
    active = outbox.load()[0]
    changed = event("evt-stale", "2026-09-02T00:00:00+00:00")
    quarantine.quarantine(
        [QueuedIntelligence(key=active.key, event=changed, queued_at=active.queued_at)],
        reason="existing different record",
        quarantined_at=NOW,
    )

    with pytest.raises(ValueError, match="quarantine collision"):
        quarantine_stale_queue(
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            apply=True,
            confirmation=CONFIRMATION,
            now=NOW,
        )

    assert len(outbox.load()) == 1
    assert quarantine.load()[0].event.summary == changed.summary


def test_future_and_undated_evidence_are_not_misclassified_as_stale():
    stale = QueuedIntelligence(
        key="stale", event=event("stale", "2026-09-01T00:00:00+00:00"), queued_at=QUEUED_AT
    )
    future = QueuedIntelligence(
        key="future", event=event("future", "2026-11-22T00:00:00+00:00"), queued_at=QUEUED_AT
    )
    undated = QueuedIntelligence(
        key="undated", event=event("undated", ""), queued_at=QUEUED_AT
    )

    assert evidence_freshness(stale, NOW)[0] == "stale"
    assert evidence_freshness(future, NOW)[0] == "future-dated"
    assert evidence_freshness(undated, NOW)[0] == "undated"
    assert [item.key for item in find_stale_items([stale, future, undated], now=NOW)] == ["stale"]


def test_duplicate_active_keys_fail_closed(tmp_path):
    _, _, outbox_path, quarantine_path = stores(tmp_path)
    item = QueuedIntelligence(
        key="duplicate", event=event("evt", "2026-09-01T00:00:00+00:00"), queued_at=QUEUED_AT
    )
    with pytest.raises(ValueError, match="duplicate keys"):
        find_stale_items([item, item], now=NOW)
