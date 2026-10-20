from datetime import datetime, timezone

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from scripts.quarantine_generic_intelligence_event import quarantine_event
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import (
    IntelligenceQuarantineError,
    JsonIntelligenceQuarantineState,
)


STAMP = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def event(event_id: str, title: str) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=f"Summary for {event_id}",
        significance="Potentially useful historical intelligence",
        evidence=[
            Evidence(
                source_id="xrpl-blog",
                source="XRP Ledger Community",
                url="https://xrpl.org/blog/2026/example/",
                published_at="2026-09-25T00:00:00+00:00",
                title=title,
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


def test_quarantines_only_exact_generic_title_event_without_publishing(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue(
        [event("evt-generic", "Read More"), event("evt-valid", "Official release headline")],
        queued_at=STAMP,
    )

    result = quarantine_event(
        "evt-generic",
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        now=STAMP,
    )

    assert result == "evt-generic"
    assert [item.event.event_id for item in outbox.load()] == ["evt-valid"]
    held = quarantine.load()
    assert [item.event.event_id for item in held] == ["evt-generic"]
    assert held[0].event.evidence[0].title == "Read More"
    assert held[0].queued_at == STAMP
    assert held[0].reason == "all evidence titles are empty or generic navigation text"


def test_refuses_descriptive_event_without_mutating_either_store(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-valid", "Introducing XRP Ledger version 3.4.1")])

    with pytest.raises(ValueError, match="descriptive evidence"):
        quarantine_event(
            "evt-valid",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )

    assert [item.event.event_id for item in outbox.load()] == ["evt-valid"]
    assert quarantine.load() == []


def test_refuses_missing_or_ambiguous_event_id_without_mutation(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-one", "Read More")])

    with pytest.raises(ValueError, match="found 0"):
        quarantine_event(
            "evt-missing",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )
    assert [item.event.event_id for item in outbox.load()] == ["evt-one"]
    assert quarantine.load() == []


def test_quarantine_write_failure_leaves_active_queue_unchanged(tmp_path, monkeypatch):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-generic", "Read More")])

    def fail(*args, **kwargs):
        raise IntelligenceQuarantineError("simulated disk failure")

    monkeypatch.setattr(quarantine, "quarantine", fail)
    with pytest.raises(IntelligenceQuarantineError, match="simulated disk failure"):
        quarantine_event(
            "evt-generic",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )

    assert [item.event.event_id for item in outbox.load()] == ["evt-generic"]
    assert quarantine.load() == []


def test_outbox_removal_failure_keeps_audited_copy_and_active_event(tmp_path, monkeypatch):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    outbox.enqueue([event("evt-generic", "Read More")])

    def fail(*args, **kwargs):
        raise RuntimeError("simulated outbox write failure")

    monkeypatch.setattr(outbox, "remove", fail)
    with pytest.raises(RuntimeError, match="simulated outbox write failure"):
        quarantine_event(
            "evt-generic",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )

    assert [item.event.event_id for item in outbox.load()] == ["evt-generic"]
    assert [item.event.event_id for item in quarantine.load()] == ["evt-generic"]
