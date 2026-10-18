from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState
from scripts.preview_intelligence_quarantine import render_preview


STAMP = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def _event(event_id: str) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=f"Historical summary for {event_id}",
        significance="Evidence is older than the live publication window",
        evidence=[
            Evidence(
                source_id="official-source",
                source="Official source",
                url="https://example.com/old-release",
                published_at="2026-08-01T12:00:00+00:00",
                title="Official old release headline",
                source_quality="primary",
            )
        ],
    )


def test_preview_shows_quarantine_reason_and_evidence_without_mutation(tmp_path):
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    quarantine_path = tmp_path / "quarantine.json"
    quarantine = JsonIntelligenceQuarantineState(quarantine_path)
    queued = outbox.enqueue([_event("evt-old")], queued_at=STAMP)
    quarantine.quarantine(
        queued, reason="latest evidence older than 30 days", quarantined_at=STAMP
    )
    before = quarantine_path.read_bytes()

    output = render_preview(quarantine_path, limit=1)

    assert "total=1 showing=1" in output
    assert "evt-old" in output
    assert "latest evidence older than 30 days" in output
    assert "Historical summary for evt-old" in output
    assert "Official old release headline" in output
    assert "https://example.com/old-release" in output
    assert "Read-only preview" in output
    assert quarantine_path.read_bytes() == before


def test_preview_cli_runs_outside_repository_root(tmp_path):
    quarantine_path = tmp_path / "quarantine.json"
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    queued = outbox.enqueue([_event("evt-cli")], queued_at=STAMP)
    JsonIntelligenceQuarantineState(quarantine_path).quarantine(
        queued, reason="stale evidence", quarantined_at=STAMP
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "preview_intelligence_quarantine.py"
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(quarantine_path), "--limit", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "evt-cli" in result.stdout
    assert "Read-only preview" in result.stdout


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
def test_preview_rejects_invalid_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        render_preview(tmp_path / "missing.json", limit=limit)
