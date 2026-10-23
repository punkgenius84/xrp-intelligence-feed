from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from scripts.verify_intelligence_test_result import verify_intelligence_test_result
from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState


def make_event(event_id: str = "evt-selected") -> IntelligenceEvent:
    return IntelligenceEvent(
        event_id=event_id,
        event_type="announcement",
        summary="A selected test event",
        significance="A test significance",
        evidence=[
            Evidence(
                source_id="official-source",
                source="Official source",
                url="https://example.com/official",
                published_at=datetime.now(timezone.utc).isoformat(),
                title="Official announcement",
                source_quality="primary",
            )
        ],
    )


def test_verifier_accepts_delivered_event_removed_from_outbox(tmp_path, capsys):
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    outbox.enqueue([make_event()])
    history.record({"evt-selected"})
    outbox.remove({"intelligence|evt-selected"})

    verify_intelligence_test_result(
        "evt-selected",
        history_store=history,
        outbox_store=outbox,
    )

    assert "Guarded intelligence delivery verified" in capsys.readouterr().out


def test_verifier_rejects_event_missing_from_delivery_history(tmp_path):
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    outbox.enqueue([make_event()])

    with pytest.raises(RuntimeError, match="missing from intelligence delivery history"):
        verify_intelligence_test_result(
            "evt-selected",
            history_store=history,
            outbox_store=outbox,
        )


def test_verifier_rejects_event_still_in_active_outbox(tmp_path):
    history = JsonIntelligenceDeliveryHistory(tmp_path / "history.json")
    outbox = JsonIntelligenceOutboxState(tmp_path / "outbox.json")
    outbox.enqueue([make_event()])
    history.record({"evt-selected"})

    with pytest.raises(RuntimeError, match="remains in the active intelligence outbox"):
        verify_intelligence_test_result(
            "evt-selected",
            history_store=history,
            outbox_store=outbox,
        )


def test_verifier_rejects_blank_event_id(tmp_path):
    with pytest.raises(ValueError, match="exact intelligence event ID"):
        verify_intelligence_test_result(
            " ",
            history_store=JsonIntelligenceDeliveryHistory(tmp_path / "history.json"),
            outbox_store=JsonIntelligenceOutboxState(tmp_path / "outbox.json"),
        )



def test_verifier_cli_can_import_project_packages(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_intelligence_test_result.py",
            "--event-id",
            "evt-cli-smoke",
            "--history-path",
            str(tmp_path / "missing-history.json"),
            "--outbox-path",
            str(tmp_path / "missing-outbox.json"),
        ],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "selected event evt-cli-smoke is missing from intelligence delivery history" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
