from datetime import datetime, timezone

import main
from discovery.base import DiscoveryResult
from discovery.models import DiscoveryCandidate
from storage.database import JsonState
from storage.discovery_state import JsonDiscoveryState


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def test_discovery_candidate_uses_existing_intelligence_pipeline_and_persists_state(
        tmp_path, monkeypatch):
    candidate = DiscoveryCandidate(
        title="SEC Form 8-K filing: Ripple Labs",
        url="https://www.sec.gov/Archives/edgar/data/9999999999/999999999926000001/filing.htm",
        source="U.S. Securities and Exchange Commission",
        source_id="sec-edgar-submissions", authority_tier=1,
        category="regulatory", discovery_method="sec_submissions",
        source_url="https://data.sec.gov/submissions/CIK9999999999.json",
        source_native_id="CIK9999999999:9999999999-26-000001",
        content_hash="fixture-hash", document_type="8-K", collected_at=STAMP,
        published_at=STAMP,
        source_native_metadata={"cik": "9999999999", "filing_date": "2026-09-22",
                                "accession_number": "9999999999-26-000001"},
    )
    result = DiscoveryResult(
        source_id="sec-edgar-submissions", discovery_method="sec_submissions",
        status="success", candidates=[candidate], fetched_at=STAMP,
        state_updates={"9999999999": {"etag": '"e1"'}},
        pagination={"next_issuer_index": 0},
    )
    monkeypatch.setattr(main, "collect_source", lambda source, state: result)
    discovery_source = {
        "source_id": "sec-edgar-submissions", "name": "SEC", "authority_tier": 1,
        "category": "regulatory", "discovery_method": "sec_submissions",
        "source_url": "https://data.sec.gov/submissions/", "enabled": True,
        "issuer_ciks": ["9999999999"], "filing_forms": ["8-K"],
        "contact_email_env": "SEC_CONTACT_EMAIL", "application_name": "XRPIntelligenceFeed/0.3",
        "max_issuers_per_run": 5,
    }
    seen = JsonState(str(tmp_path / "seen.json"))
    discovery = JsonDiscoveryState(tmp_path / "discovery.json")
    collected, fresh, failures = main.run_pipeline(
        sources=[], state=seen, discovery_sources=[discovery_source], discovery_state=discovery,
    )
    assert collected == [candidate]
    assert fresh == [candidate]
    assert failures == []
    assert candidate.detected_entities == ["Ripple", "SEC"]
    assert candidate.source_quality == "primary"
    state = discovery.load()
    assert state["sources"]["sec-edgar-submissions"]["requests"]["9999999999"]["etag"] == '"e1"'
    assert state["sources"]["sec-edgar-submissions"]["pagination"]["next_issuer_index"] == 0
    assert state["candidates"][candidate.candidate_id]["first_seen_time"] == STAMP.isoformat()


def test_discovery_state_is_not_saved_when_intelligence_processing_fails(tmp_path, monkeypatch):
    candidate = DiscoveryCandidate(
        "XRP filing", "https://www.sec.gov/filing", "SEC",
        source_id="sec-edgar-submissions", authority_tier=1,
        discovery_method="sec_submissions", content_hash="h",
    )
    result = DiscoveryResult("sec-edgar-submissions", "sec_submissions", "success",
                             candidates=[candidate], fetched_at=STAMP)
    monkeypatch.setattr(main, "collect_source", lambda source, state: result)
    monkeypatch.setattr(main, "detect_entities",
                        lambda item: (_ for _ in ()).throw(RuntimeError("enrichment failed")))
    source = {
        "source_id": "sec-edgar-submissions", "name": "SEC", "authority_tier": 1,
        "category": "regulatory", "discovery_method": "sec_submissions",
        "source_url": "https://data.sec.gov/submissions/", "enabled": True,
        "issuer_ciks": ["9999999999"], "filing_forms": ["8-K"],
        "contact_email_env": "SEC_CONTACT_EMAIL", "application_name": "XRPIntelligenceFeed/0.3",
        "max_issuers_per_run": 5,
    }
    discovery = JsonDiscoveryState(tmp_path / "discovery.json")
    try:
        main.run_pipeline(sources=[], state=JsonState(str(tmp_path / "seen.json")),
                          discovery_sources=[source], discovery_state=discovery)
    except RuntimeError as exc:
        assert str(exc) == "enrichment failed"
    else:
        raise AssertionError("expected enrichment failure")
    assert not discovery.path.exists()


def test_discovery_state_save_failure_does_not_permanently_mark_candidate_seen(
        tmp_path, monkeypatch):
    candidate = DiscoveryCandidate(
        title="SEC Form 8-K filing: Ripple Labs",
        url="https://www.sec.gov/Archives/edgar/data/9999999999/filing.htm",
        source="SEC", source_id="sec-edgar-submissions", authority_tier=1,
        category="regulatory", discovery_method="sec_submissions",
        content_hash="fixture-hash", published_at=STAMP,
    )
    result = DiscoveryResult("sec-edgar-submissions", "sec_submissions", "success",
                             candidates=[candidate], fetched_at=STAMP)
    monkeypatch.setattr(main, "collect_source", lambda source, state: result)
    source = {
        "source_id": "sec-edgar-submissions", "name": "SEC", "authority_tier": 1,
        "category": "regulatory", "discovery_method": "sec_submissions",
        "source_url": "https://data.sec.gov/submissions/", "enabled": True,
        "issuer_ciks": ["9999999999"], "filing_forms": ["8-K"],
        "contact_email_env": "SEC_CONTACT_EMAIL", "application_name": "XRPIntelligenceFeed/0.3",
        "max_issuers_per_run": 5,
    }

    class FailOnceDiscoveryState(JsonDiscoveryState):
        fail = True

        def save(self, value, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("simulated discovery-state write failure")
            return super().save(value, **kwargs)

    seen = JsonState(str(tmp_path / "seen.json"))
    discovery = FailOnceDiscoveryState(tmp_path / "discovery.json")
    args = dict(sources=[], state=seen, discovery_sources=[source], discovery_state=discovery)
    try:
        main.run_pipeline(**args)
    except RuntimeError as exc:
        assert str(exc) == "simulated discovery-state write failure"
    else:
        raise AssertionError("expected discovery-state write failure")

    assert seen.load() == set()
    _, fresh, _ = main.run_pipeline(**args)
    assert fresh == [candidate]
    assert seen.load()


def test_federal_register_pagination_only_update_persists_through_pipeline(tmp_path, monkeypatch):
    progress = {
        "digital assets": {
            "boundary_document_number": "2026-00100",
            "deep_page_hint": 5,
            "lookback_days": 7,
            "frontier_status": "active",
        }
    }
    result = DiscoveryResult(
        "federal-register-api", "federal_register_api", "success", fetched_at=STAMP,
        pagination={"requests_made": 4, "federal_register_terms": progress},
    )
    monkeypatch.setattr(main, "collect_source", lambda source, state: result)
    discovery = JsonDiscoveryState(tmp_path / "discovery.json")
    main.run_pipeline(
        sources=[], state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[{"source_id": "federal-register-api"}], discovery_state=discovery,
    )
    saved = discovery.load()["sources"]["federal-register-api"]["pagination"]
    assert saved["federal_register_terms"] == progress


def test_partial_federal_register_result_without_completed_progress_does_not_persist_cursor(
        tmp_path, monkeypatch):
    result = DiscoveryResult(
        "federal-register-api", "federal_register_api", "partial", fetched_at=STAMP,
        errors=["partial response"], pagination={"requests_made": 4},
    )
    monkeypatch.setattr(main, "collect_source", lambda source, state: result)
    discovery = JsonDiscoveryState(tmp_path / "discovery.json")
    main.run_pipeline(
        sources=[], state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[{"source_id": "federal-register-api"}], discovery_state=discovery,
    )
    assert "federal-register-api" not in discovery.load()["sources"]
