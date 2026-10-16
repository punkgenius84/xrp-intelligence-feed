from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from scripts.preview_intelligence_outbox import render_preview
from storage.intelligence_outbox import JsonIntelligenceOutboxState


STAMP = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def _event(event_id: str, summary: str) -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary=summary,
        significance="Material change for institutional payments",
        evidence=[
            Evidence(
                source_id="official-source",
                source="Official source",
                url="https://example.com/official-release",
                published_at=STAMP.isoformat(),
                title="Official release headline",
                source_quality="primary",
            )
        ],
    )


def test_preview_reports_queued_event_evidence_without_mutating_state(tmp_path):
    path = tmp_path / "intelligence_outbox.json"
    store = JsonIntelligenceOutboxState(path)
    store.enqueue(
        [
            _event("evt-first", "First queued intelligence summary"),
            _event("evt-second", "Second queued intelligence summary"),
        ],
        queued_at=STAMP,
    )
    before = path.read_bytes()

    output = render_preview(path, limit=1)

    assert "pending=2 showing=1" in output
    assert "First queued intelligence summary" in output
    assert "Official release headline" in output
    assert "https://example.com/official-release" in output
    assert "Second queued intelligence summary" not in output
    assert "Read-only preview" in output
    assert path.read_bytes() == before


def test_preview_cli_runs_from_outside_repository_root(tmp_path):
    path = tmp_path / "intelligence_outbox.json"
    JsonIntelligenceOutboxState(path).enqueue(
        [_event("evt-cli", "CLI preview summary")], queued_at=STAMP
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "preview_intelligence_outbox.py"
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(path), "--limit", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CLI preview summary" in result.stdout
    assert "Read-only preview" in result.stdout


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
def test_preview_rejects_invalid_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="between 1 and 100"):
        render_preview(tmp_path / "missing.json", limit=limit)
