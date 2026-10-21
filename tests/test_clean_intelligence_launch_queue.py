from datetime import datetime, timezone

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from scripts.clean_intelligence_launch_queue import CONFIRMATION, clean_queue
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState


STAMP = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def event(event_id: str) -> IntelligenceEvent:
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
                published_at="2026-10-20T11:00:00+00:00",
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


def test_default_preview_is_read_only(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-old-one"), event("evt-old-two")], queued_at=STAMP)

    result = clean_queue(
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        now=STAMP,
    )

    assert result == {
        "pending_before": 2,
        "quarantine_before": 0,
        "applied": False,
        "pending_after": 2,
    }
    assert len(outbox.load()) == 2
    assert quarantine.load() == []


def test_apply_archives_every_item_then_clears_only_active_queue(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-old-one"), event("evt-old-two")], queued_at=STAMP)
    delivery_history_path = tmp_path / "intelligence_delivery_history.json"
    delivery_history_path.write_text('{"schema_version":1,"items":[]}', encoding="utf-8")

    result = clean_queue(
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        apply=True,
        confirmation=CONFIRMATION,
        now=STAMP,
    )

    assert result["pending_before"] == 2
    assert result["pending_after"] == 0
    assert result["quarantine_after"] == 2
    assert outbox.load() == []
    archived = quarantine.load()
    assert {item.event.event_id for item in archived} == {"evt-old-one", "evt-old-two"}
    assert all(item.reason == "pre-launch queue reset; preserved for audit and intentionally not published" for item in archived)
    assert delivery_history_path.read_text(encoding="utf-8") == '{"schema_version":1,"items":[]}'


def test_apply_requires_exact_confirmation_and_changes_nothing(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-old")], queued_at=STAMP)

    with pytest.raises(ValueError, match="requires --confirm"):
        clean_queue(
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            apply=True,
            confirmation="yes",
            now=STAMP,
        )

    assert len(outbox.load()) == 1
    assert quarantine.load() == []


def test_quarantine_collision_refuses_to_clear_active_queue(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-collision")], queued_at=STAMP)
    different = outbox.enqueue([event("evt-collision")], queued_at=STAMP)[0]
    from storage.intelligence_outbox import QueuedIntelligence

    changed = event("evt-collision")
    changed.summary = "Changed content under the same event ID"
    quarantine.quarantine(
        [QueuedIntelligence(key=different.key, event=changed, queued_at=STAMP)],
        reason="pre-existing different event",
        quarantined_at=STAMP,
    )

    with pytest.raises(ValueError, match="quarantine collision"):
        clean_queue(
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            apply=True,
            confirmation=CONFIRMATION,
            now=STAMP,
        )

    assert len(outbox.load()) == 1
    assert quarantine.load()[0].event.summary == "Changed content under the same event ID"


def test_empty_queue_apply_is_idempotent(tmp_path):
    _, _, outbox_path, quarantine_path = stores(tmp_path)

    result = clean_queue(
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        apply=True,
        confirmation=CONFIRMATION,
        now=STAMP,
    )

    assert result["pending_before"] == 0
    assert result["pending_after"] == 0
    assert result["quarantine_before"] == 0
    assert result["applied"] is True
