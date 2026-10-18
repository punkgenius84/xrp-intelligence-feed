from datetime import datetime, timezone

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import (
    IntelligenceQuarantineError,
    JsonIntelligenceQuarantineState,
)


def _event(event_id: str) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=f"Summary for {event_id}",
        significance="Potentially useful historical intelligence",
        evidence=[
            Evidence(
                source_id="official",
                source="Official source",
                url="https://example.com/release",
                published_at="2026-08-01T12:00:00+00:00",
                title="Official release headline",
                source_quality="primary",
            )
        ],
    )


def test_quarantine_preserves_stale_event_and_is_idempotent(tmp_path):
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    quarantine = JsonIntelligenceQuarantineState(tmp_path / "quarantine.json")
    queued_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    queued = outbox.enqueue([_event("evt-stale")], queued_at=queued_at)

    first = quarantine.quarantine(
        queued, reason="latest evidence older than 30 days",
        quarantined_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )
    second = quarantine.quarantine(
        queued, reason="duplicate retry reason",
        quarantined_at=datetime(2026, 10, 19, tzinfo=timezone.utc),
    )

    assert len(first) == len(second) == 1
    assert second[0].reason == "latest evidence older than 30 days"
    assert second[0].queued_at == queued_at
    assert second[0].event.summary == "Summary for evt-stale"
    assert len(quarantine.load()) == 1
    # Quarantine itself does not mutate the active outbox; caller removes only after save.
    assert [item.event.event_id for item in outbox.load()] == ["evt-stale"]


def test_quarantine_fails_closed_on_corrupt_state(tmp_path):
    path = tmp_path / "quarantine.json"
    path.write_text("{broken", encoding="utf-8")
    store = JsonIntelligenceQuarantineState(path)
    with pytest.raises(IntelligenceQuarantineError, match="malformed JSON"):
        store.quarantine([], reason="stale")


def test_quarantine_refuses_to_drop_records_at_capacity(tmp_path, monkeypatch):
    from storage import intelligence_quarantine as module

    store = JsonIntelligenceQuarantineState(tmp_path / "quarantine.json")
    monkeypatch.setattr(module, "MAX_QUARANTINE_ENTRIES", 1)
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    queued = outbox.enqueue([_event("evt-one"), _event("evt-two")])
    with pytest.raises(IntelligenceQuarantineError, match="refusing to discard records"):
        store.quarantine(queued, reason="stale")
    assert not (tmp_path / "quarantine.json").exists()
