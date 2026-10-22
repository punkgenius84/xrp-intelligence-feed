from datetime import datetime, timezone

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState
from scripts.prepare_quarantined_intelligence_test import CONFIRMATION, prepare_event

NOW = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)


def _event(event_id: str, published_at: str = "2026-10-15T00:00:00+00:00"):
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary="Official institutional digital-asset announcement.",
        significance="Potentially material institutional development.",
        evidence=[
            Evidence(
                source_id="ripple-press",
                source="Ripple",
                url="https://ripple.com/ripple-press/example/",
                published_at=published_at,
                title="Official announcement",
                source_quality="primary",
            )
        ],
    )


def test_requeues_only_the_exact_fresh_quarantined_event(tmp_path):
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    quarantine = JsonIntelligenceQuarantineState(tmp_path / "quarantine.json")
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    queued = outbox.enqueue([_event("evt-target")], queued_at=NOW)
    quarantine.quarantine(queued, reason="approved for guarded test", quarantined_at=NOW)
    outbox.remove({queued[0].key})

    result = prepare_event(
        "evt-target", CONFIRMATION, outbox=outbox, quarantine=quarantine,
        history=history, now=NOW,
    )

    assert "Requeued exactly one" in result
    assert [item.event.event_id for item in outbox.load()] == ["evt-target"]
    assert [item.event.event_id for item in quarantine.load()] == ["evt-target"]


def test_requeue_refuses_wrong_confirmation_and_stale_evidence(tmp_path):
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    quarantine = JsonIntelligenceQuarantineState(tmp_path / "quarantine.json")
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    queued = outbox.enqueue([_event("evt-stale", "2026-08-01T00:00:00+00:00")], queued_at=NOW)
    quarantine.quarantine(queued, reason="old", quarantined_at=NOW)
    outbox.remove({queued[0].key})

    with pytest.raises(ValueError, match="confirmation"):
        prepare_event("evt-stale", "wrong", outbox=outbox, quarantine=quarantine, history=history, now=NOW)
    with pytest.raises(ValueError, match="fresh"):
        prepare_event("evt-stale", CONFIRMATION, outbox=outbox, quarantine=quarantine, history=history, now=NOW)
    assert outbox.load() == []


def test_requeue_refuses_already_delivered_event(tmp_path):
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    quarantine = JsonIntelligenceQuarantineState(tmp_path / "quarantine.json")
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    queued = outbox.enqueue([_event("evt-delivered")], queued_at=NOW)
    quarantine.quarantine(queued, reason="held", quarantined_at=NOW)
    outbox.remove({queued[0].key})
    history.record({"evt-delivered"}, posted_at=NOW)

    with pytest.raises(ValueError, match="already in delivery history"):
        prepare_event("evt-delivered", CONFIRMATION, outbox=outbox, quarantine=quarantine, history=history, now=NOW)
    assert outbox.load() == []
