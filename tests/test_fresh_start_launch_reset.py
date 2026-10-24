from datetime import datetime, timezone
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from intelligence.llm.schemas import Claim
from scripts.fresh_start_launch_reset import CONFIRMATION, fresh_start_reset, inspect_state
from scripts import fresh_start_launch_reset as reset_module
from models import NewsItem
from storage.correlation_state import JsonCorrelationState, empty_correlation_state
from storage.database import JsonState
from storage.delivery_history import JsonDeliveryHistory
from storage.discovery_state import JsonDiscoveryState, empty_discovery_state
from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState
from storage.intelligence_state import JsonIntelligenceState
from storage.outbox import JsonOutboxState, publication_key


def seed_state(root: Path):
    now = datetime(2026, 10, 24, 5, tzinfo=timezone.utc)
    JsonState(root / "seen.json").save({"seen-fingerprint"})

    discovery = empty_discovery_state()
    discovery["sources"]["example"] = {"requests": {}}
    JsonDiscoveryState(root / "discovery.json").save(discovery, now=now)

    correlation = empty_correlation_state()
    JsonCorrelationState.upsert_card(
        correlation,
        candidate_id="example:1",
        source_id="example",
        published_at=now,
        high_value_entities=["Ripple"],
        title_tokens=["payments"],
        content_hash="hash",
        last_seen=now,
    )
    JsonCorrelationState(root / "correlation.json").save(correlation, now=now)

    intelligence = JsonIntelligenceState(root / "intelligence.json").load()
    JsonIntelligenceState.upsert(
        intelligence, "evt-old", {"summary": "old", "evidence": []}, now.isoformat()
    )
    JsonIntelligenceState(root / "intelligence.json").save(intelligence)

    news = NewsItem(
        title="Example crypto payments update",
        url="https://example.test/news",
        source="Example",
        source_id="example",
        candidate_id="example:news",
        content_hash="hash-news",
        published_at=now,
        relevance_score=50,
        detected_entities=["Crypto"],
        score_reasons=["payment infrastructure"],
    )
    JsonOutboxState(root / "outbox.json").enqueue([news])
    JsonDeliveryHistory(root / "delivery_history.json").record(
        [news], {publication_key(news)}, posted_at=now
    )

    event = IntelligenceEvent(
        event_id="evt-old",
        event_type="announcement",
        summary="Old event",
        significance="Historical context",
        claims=[Claim("The source reports an event.", evidence=["source-1 title"])],
        evidence=[Evidence(
            source_id="example",
            source="Example",
            url="https://example.test/event",
            published_at=now.isoformat(),
            title="Example event",
            source_quality="primary",
        )],
    )
    queued = JsonIntelligenceOutboxState(root / "intelligence_outbox.json").enqueue(
        [event], queued_at=now
    )
    JsonIntelligenceDeliveryHistory(root / "intelligence_delivery_history.json").record(
        {"evt-prior"}, posted_at=now
    )
    JsonIntelligenceQuarantineState(root / "intelligence_quarantine.json").quarantine(
        queued, reason="prior audit record", quarantined_at=now
    )


def test_preview_does_not_mutate_state_or_create_archive(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir)
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    archive = tmp_path / "archive.zip"

    result = fresh_start_reset(state_dir=state_dir, archive_path=archive)

    assert result["applied"] is False
    assert not archive.exists()
    assert {p.name: p.read_bytes() for p in state_dir.iterdir()} == before


def test_apply_requires_exact_confirmation_and_changes_nothing(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir)
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    archive = tmp_path / "archive.zip"

    with pytest.raises(ValueError, match="exact confirmation"):
        fresh_start_reset(
            state_dir=state_dir,
            archive_path=archive,
            apply=True,
            confirmation="wrong",
        )

    assert not archive.exists()
    assert {p.name: p.read_bytes() for p in state_dir.iterdir()} == before


def test_corrupt_archive_refuses_to_reset_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir)
    before = {p.name: p.read_bytes() for p in state_dir.iterdir()}
    archive = tmp_path / "archive.zip"

    def write_corrupt_archive(state_root, archive_path, *, now):
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(b"not a zip archive")
        return {}

    monkeypatch.setattr(reset_module, "_archive_state", write_corrupt_archive)

    with pytest.raises(RuntimeError, match="pre-reset archive verification failed"):
        fresh_start_reset(
            state_dir=state_dir,
            archive_path=archive,
            apply=True,
            confirmation=CONFIRMATION,
            now=datetime(2026, 10, 24, 6, tzinfo=timezone.utc),
        )

    assert archive.exists()
    assert {p.name: p.read_bytes() for p in state_dir.iterdir()} == before


def test_apply_archives_then_clears_operational_state_and_preserves_quarantine(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir)
    archive = tmp_path / "archive.zip"

    result = fresh_start_reset(
        state_dir=state_dir,
        archive_path=archive,
        apply=True,
        confirmation=CONFIRMATION,
        now=datetime(2026, 10, 24, 6, tzinfo=timezone.utc),
    )

    assert result["applied"] is True
    assert archive.exists()
    reset_module._verify_archive(archive)
    with ZipFile(archive) as saved:
        assert "manifest.json" in saved.namelist()
        assert "state/seen.json" in saved.namelist()
        assert "state/intelligence_quarantine.json" in saved.namelist()
        manifest = json.loads(saved.read("manifest.json"))
        assert {entry["path"] for entry in manifest["files"]} >= {
            "state/seen.json",
            "state/outbox.json",
            "state/delivery_history.json",
            "state/intelligence_delivery_history.json",
            "state/intelligence_quarantine.json",
        }

    after = inspect_state(state_dir)
    assert after["seen_fingerprints"] == 0
    assert after["discovery_sources"] == 0
    assert after["discovery_candidates"] == 0
    assert after["correlation_cards"] == 0
    assert after["intelligence_events"] == 0
    assert after["normal_outbox"] == 0
    assert after["normal_delivery_history"] == 0
    assert after["intelligence_outbox"] == 0
    assert after["intelligence_delivery_history"] == 0
    assert after["quarantine_audit_records"] == 1
