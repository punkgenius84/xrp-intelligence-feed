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

    monkeypatch.setattr(JsonIntelligenceQuarantineState, "quarantine", fail)
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

    monkeypatch.setattr(JsonIntelligenceOutboxState, "remove", fail)
    with pytest.raises(RuntimeError, match="simulated outbox write failure"):
        quarantine_event(
            "evt-generic",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )

    assert [item.event.event_id for item in outbox.load()] == ["evt-generic"]
    assert [item.event.event_id for item in quarantine.load()] == ["evt-generic"]


def test_maintenance_workflow_is_manual_nonpublishing_and_saves_audit_first():
    from pathlib import Path

    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "quarantine_intelligence_event.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "group: xrp-intelligence-feed" in workflow
    assert "scripts/quarantine_generic_intelligence_event.py" in workflow
    assert "python main.py" not in workflow
    assert "DISCORD_WEBHOOK_URL" not in workflow
    assert "publish_intelligence" not in workflow
    assert workflow.index("name: Save intelligence quarantine") < workflow.index(
        "name: Save persistent state"
    )
    assert "id: quarantine" in workflow
    assert "steps.quarantine.outcome == 'success' || steps.quarantine.outcome == 'failure'" in workflow
    state_save_block = workflow.split("name: Save persistent state", 1)[1].split(
        "name: Save intelligence quarantine", 1
    )[0]
    assert "if: success()" in state_save_block
    assert "actions/cache/save@55cc8345863c7cc4c66a329aec7e433d2d1c52a9" in workflow
    assert "key: xrp-state-${{ github.ref_name }}-v1-restore" in workflow
    assert "key: xrp-state-${{ github.ref_name }}-v1-${{ hashFiles('state/seen.json', 'state/discovery.json', 'state/correlation.json', 'state/intelligence.json', 'state/intelligence_outbox.json', 'state/outbox.json', 'state/delivery_history.json') }}" in workflow
    assert "key: xrp-intelligence-quarantine-${{ github.ref_name }}-v1-restore" in workflow
    assert "key: xrp-intelligence-quarantine-${{ github.ref_name }}-v1-${{ github.run_id }}-${{ hashFiles('state/intelligence_quarantine.json') }}" in workflow
    assert "xrp-state-main-v1-maintenance-" not in workflow


def test_refuses_same_key_with_changed_event_content(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    queued = outbox.enqueue([event("evt-collision", "Read More")], queued_at=STAMP)
    quarantine.quarantine(
        queued,
        reason="earlier malformed version",
        quarantined_at=STAMP,
    )
    outbox.enqueue([event("evt-collision", "Learn More")], queued_at=STAMP)

    with pytest.raises(ValueError, match="different content"):
        quarantine_event(
            "evt-collision",
            outbox_path=outbox_path,
            quarantine_path=quarantine_path,
            now=STAMP,
        )

    assert [item.event.evidence[0].title for item in outbox.load()] == ["Learn More"]
    assert [item.event.evidence[0].title for item in quarantine.load()] == ["Read More"]


def test_identical_existing_quarantine_record_allows_safe_retry(tmp_path):
    outbox, quarantine, outbox_path, quarantine_path = stores(tmp_path)
    queued = outbox.enqueue([event("evt-retry", "Read More")], queued_at=STAMP)
    quarantine.quarantine(
        queued,
        reason="earlier malformed version",
        quarantined_at=STAMP,
    )

    quarantine_event(
        "evt-retry",
        outbox_path=outbox_path,
        quarantine_path=quarantine_path,
        now=STAMP,
    )

    assert outbox.load() == []
    assert len(quarantine.load()) == 1
    assert quarantine.load()[0].reason == "earlier malformed version"
