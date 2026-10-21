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


def _event(
    event_id: str,
    summary: str,
    *,
    published_at: datetime | str = STAMP,
    title: str = "Official release headline",
) -> IntelligenceEvent:
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
                published_at=published_at.isoformat() if isinstance(published_at, datetime) else published_at,
                title=title,
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


def test_preview_labels_stale_undated_and_generic_title_evidence(tmp_path):
    path = tmp_path / "intelligence_outbox.json"
    store = JsonIntelligenceOutboxState(path)
    store.enqueue(
        [
            _event(
                "evt-stale-generic",
                "Old event with generic evidence",
                published_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                title="Read More",
            ),
            _event(
                "evt-undated",
                "Event with no parseable evidence date",
                published_at="not-a-date",
            ),
            _event(
                "evt-near-future",
                "Event with an allowed near-future timestamp",
                published_at=datetime(2026, 10, 21, 0, tzinfo=timezone.utc),
            ),
        ],
        queued_at=STAMP,
    )

    output = render_preview(
        path,
        limit=5,
        now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
        stale_after_days=30,
    )

    assert "evidence_freshness=stale latest_evidence_date=2026-08-01 age_days=80" in output
    assert "generic_evidence_titles_only=true" in output
    assert "recommended_action=quarantine_stale_before_publish" in output
    assert "evidence_freshness=undated latest_evidence_date=none" in output
    assert "recommended_action=hold_missing_or_invalid_date" in output
    assert "evidence_freshness=fresh latest_evidence_date=2026-10-21 age_days=0" in output
    assert "recommended_action=editorial_review_before_launch" in output
    assert "generic_evidence_titles_only=false" in output


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
