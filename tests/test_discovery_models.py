from datetime import datetime, timezone

from discovery.models import DiscoveryCandidate, stable_candidate_id
from discovery.normalization import normalize_candidate
from models import NewsItem


def test_discovery_candidate_is_compatible_with_existing_news_item_pipeline():
    candidate = DiscoveryCandidate(
        title="  SEC filing &amp; XRP  ", url="https://Example.test/path#fragment",
        source="SEC", source_id="sec-edgar-submissions", authority_tier=1,
        source_native_id="CIK0000000001:0000000001-26-000001",
        discovery_method="sec_submissions", summary="<p>Example&nbsp;text.</p>",
    )
    normalized = normalize_candidate(candidate)
    assert isinstance(normalized, NewsItem)
    assert normalized.canonical_url == "https://example.test/path"
    assert normalized.publisher == "SEC"
    assert normalized.summary_excerpt == "Example text."
    assert normalized.primary_url == normalized.url
    assert normalized.discovered_time.tzinfo == timezone.utc
    assert normalized.candidate_id == "sec-edgar-submissions:CIK0000000001:0000000001-26-000001"


def test_candidate_ids_are_deterministic_and_use_source_native_ids():
    expected = "sec:CIK0000000001:0000000001-26-000001"
    assert stable_candidate_id("sec", "CIK0000000001:0000000001-26-000001") == expected
    assert stable_candidate_id("sec", "CIK0000000001:0000000001-26-000001") == expected
    assert stable_candidate_id("x", canonical_url="https://example.test/a", content_hash="abc") == \
        stable_candidate_id("x", canonical_url="https://example.test/a", content_hash="abc")


def test_discovery_model_preserves_times_and_intelligence_fields():
    stamp = datetime(2026, 9, 23, tzinfo=timezone.utc)
    item = DiscoveryCandidate("Title", "https://example.test/a", "Publisher",
                              published_at=stamp, source_quality="primary",
                              relevance_score=41, score_signals=["category:x"],
                              first_seen_at=stamp, last_seen_at=stamp)
    assert item.publication_time == stamp
    assert item.source_quality == "primary"
    assert item.relevance_score == 41
    assert item.score_signals == ["category:x"]
