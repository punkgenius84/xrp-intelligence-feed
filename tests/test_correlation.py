from datetime import datetime, timezone

from intelligence.correlation import correlate, suppress_correlated_secondary_duplicates
from models import NewsItem


def item(title, source_id, candidate_id, entities, day="2026-09-30"):
    return NewsItem(
        title=title,
        url=f"https://example.com/{candidate_id}",
        source=source_id,
        source_id=source_id,
        candidate_id=candidate_id,
        detected_entities=entities,
        published_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
    )


def test_correlates_different_official_sources_reporting_same_event():
    first = item(
        "Ripple and DBS announce tokenized deposit payment collaboration",
        "ripple-press-center",
        "ripple:1",
        ["Ripple", "DBS"],
    )
    second = item(
        "DBS expands tokenized deposit payments with Ripple collaboration",
        "dbs-newsroom",
        "dbs:1",
        ["DBS", "Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score >= 60
    assert "dbs-newsroom" in first.correlated_source_ids
    assert "dbs:1" in first.correlated_candidate_ids
    assert first.correlation_reasons


def test_does_not_correlate_unrelated_stories_sharing_an_entity():
    first = item(
        "Ripple launches new institutional payment corridor",
        "ripple-press-center",
        "ripple:2",
        ["Ripple"],
    )
    second = item(
        "Ripple reports engineering update for validator tooling",
        "xrpl-community-blog",
        "xrpl:2",
        ["Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score == 0
    assert first.correlated_source_ids == []
    assert first.correlated_candidate_ids == []


def test_does_not_correlate_items_from_the_same_source():
    first = item(
        "SEC announces digital asset enforcement action involving Ripple",
        "sec-press-releases",
        "sec:1",
        ["SEC", "Ripple"],
    )
    second = item(
        "SEC announces Ripple digital asset enforcement action",
        "sec-press-releases",
        "sec:2",
        ["SEC", "Ripple"],
    )

    correlate([first, second])

    assert first.correlation_score == 0
    assert second.correlation_score == 0


def test_correlates_against_recent_prior_run_card():
    current = item(
        "Treasury and Ripple announce digital asset payment framework",
        "treasury-press-releases",
        "treasury:2",
        ["Treasury", "Ripple"],
        day="2026-09-30",
    )
    history = [{
        "candidate_id": "ripple:prior",
        "source_id": "ripple-press-center",
        "published_at": "2026-09-29T12:00:00+00:00",
        "high_value_entities": ["Ripple", "Treasury"],
        "title_tokens": ["treasury", "ripple", "announce", "digital", "asset", "payment", "framework"],
        "content_hash": "prior-hash",
        "last_seen": "2026-09-29T12:00:00+00:00",
    }]
    correlate([current], history=history)
    assert current.correlation_score >= 75
    assert current.correlated_source_ids == ["ripple-press-center"]
    assert current.correlated_candidate_ids == ["ripple:prior"]


def test_does_not_correlate_history_outside_one_day_window():
    current = item(
        "Treasury and Ripple announce digital asset payment framework",
        "treasury-press-releases",
        "treasury:3",
        ["Treasury", "Ripple"],
        day="2026-09-30",
    )
    history = [{
        "candidate_id": "ripple:old",
        "source_id": "ripple-press-center",
        "published_at": "2026-09-28T12:00:00+00:00",
        "high_value_entities": ["Ripple", "Treasury"],
        "title_tokens": ["treasury", "ripple", "announce", "digital", "asset", "payment", "framework"],
        "content_hash": "old-hash",
        "last_seen": "2026-09-28T12:00:00+00:00",
    }]
    correlate([current], history=history)
    assert current.correlation_score == 0
    assert current.correlated_source_ids == []


def test_does_not_correlate_same_source_history_card():
    current = item(
        "Treasury and Ripple announce digital asset payment framework",
        "treasury-press-releases",
        "treasury:4",
        ["Treasury", "Ripple"],
    )
    history = [{
        "candidate_id": "treasury:prior",
        "source_id": "treasury-press-releases",
        "published_at": "2026-09-30T11:00:00+00:00",
        "high_value_entities": ["Ripple", "Treasury"],
        "title_tokens": ["treasury", "ripple", "announce", "digital", "asset", "payment", "framework"],
        "content_hash": "same-source",
        "last_seen": "2026-09-30T11:00:00+00:00",
    }]
    correlate([current], history=history)
    assert current.correlation_score == 0


def test_history_match_preserves_existing_relevance_score():
    current = item(
        "Treasury and Ripple announce digital asset payment framework",
        "treasury-press-releases",
        "treasury:5",
        ["Treasury", "Ripple"],
    )
    current.relevance_score = 17
    history = [{
        "candidate_id": "ripple:prior-2",
        "source_id": "ripple-press-center",
        "published_at": "2026-09-30T11:00:00+00:00",
        "high_value_entities": ["Ripple", "Treasury"],
        "title_tokens": ["treasury", "ripple", "announce", "digital", "asset", "payment", "framework"],
        "content_hash": "prior-hash",
        "last_seen": "2026-09-30T11:00:00+00:00",
    }]
    correlate([current], history=history)
    assert current.relevance_score == 17



def test_suppresses_secondary_bloom_report_when_qualifying_primary_exists():
    primary = item(
        "MAS, Bank Negara Malaysia and Bank of Thailand partner to advance cross-border settlement in digital money through BLOOM",
        "mas-google-news-discovery",
        "mas:bloom",
        ["Monetary Authority of Singapore", "Bank Negara Malaysia", "Bank of Thailand"],
        day="2026-10-23",
    )
    secondary = item(
        "Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        "theblock-google-news-discovery",
        "theblock:bloom",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-23",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 32
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 55

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [primary, secondary], publish_score=30
    )

    assert kept == [primary]
    assert suppressed == [secondary]
    assert secondary.source_id in primary.duplicate_sources
    assert secondary.url in primary.duplicate_urls
    assert primary.relevance_score == 32
    assert secondary.relevance_score == 55


def test_does_not_suppress_secondary_when_primary_is_below_publish_threshold():
    primary = item(
        "Ripple and Mastercard expand cross-border stablecoin payment initiative",
        "mas-google-news-discovery",
        "mas:old",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-23",
    )
    secondary = item(
        "Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        "theblock-google-news-discovery",
        "theblock:new",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-23",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 20
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 55

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [primary, secondary], publish_score=30
    )

    assert kept == [primary, secondary]
    assert suppressed == []


def test_does_not_suppress_unrelated_secondary_story_sharing_ripple():
    primary = item(
        "Ripple and DBS announce tokenized deposit payment collaboration",
        "dbs-newsroom",
        "dbs:payment",
        ["Ripple", "DBS"],
        day="2026-10-23",
    )
    secondary = item(
        "XRP Ledger bug fix moved to November after validator support dipped for two hours",
        "coindesk-google-news-discovery",
        "coindesk:bug",
        ["Ripple", "XRP"],
        day="2026-10-23",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 40
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 40

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [primary, secondary], publish_score=30
    )

    assert kept == [primary, secondary]
    assert suppressed == []



def test_does_not_suppress_regional_cross_border_headlines_without_financial_context():
    primary = item(
        "Singapore, Malaysia and Thailand expand cross-border rail freight corridor",
        "primary-news",
        "primary:rail",
        [],
        day="2026-10-23",
    )
    secondary = item(
        "Singapore, Malaysia and Thailand announce cross-border rail schedule changes",
        "secondary-news",
        "secondary:rail",
        [],
        day="2026-10-23",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 40
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 40

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [primary, secondary], publish_score=30
    )

    assert kept == [primary, secondary]
    assert suppressed == []



def test_suppresses_secondary_when_primary_was_seen_in_a_previous_run():
    from intelligence.correlation import build_correlation_card

    primary = item(
        "MAS, Bank Negara Malaysia and Bank of Thailand partner to advance cross-border settlement in digital money through BLOOM",
        "mas-google-news-discovery",
        "mas:bloom-prior",
        ["Monetary Authority of Singapore", "Bank Negara Malaysia", "Bank of Thailand"],
        day="2026-10-23",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 32
    card = build_correlation_card(primary)

    secondary = item(
        "Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        "theblock-google-news-discovery",
        "theblock:bloom-later",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-24",
    )
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 55

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [secondary], publish_score=30, history=[card]
    )

    assert kept == []
    assert suppressed == [secondary]
    assert secondary.relevance_score == 55


def test_does_not_suppress_secondary_from_legacy_history_without_quality_metadata():
    secondary = item(
        "Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        "theblock-google-news-discovery",
        "theblock:bloom-legacy",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-24",
    )
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 55
    legacy_card = {
        "candidate_id": "mas:bloom-legacy",
        "source_id": "mas-google-news-discovery",
        "published_at": "2026-10-23T00:00:00+00:00",
        "high_value_entities": ["Monetary Authority of Singapore", "Bank Negara Malaysia", "Bank of Thailand"],
        "title_tokens": ["mas", "bank", "negara", "malaysia", "thailand", "partner", "advance", "cross", "border", "settlement", "digital", "money", "bloom"],
        "content_hash": "legacy",
        "last_seen": "2026-10-23T00:00:00+00:00",
    }

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [secondary], publish_score=30, history=[legacy_card]
    )

    assert kept == [secondary]
    assert suppressed == []


def test_does_not_suppress_secondary_for_prior_primary_outside_one_day():
    from intelligence.correlation import build_correlation_card

    primary = item(
        "MAS, Bank Negara Malaysia and Bank of Thailand partner to advance cross-border settlement in digital money through BLOOM",
        "mas-google-news-discovery",
        "mas:bloom-old",
        ["Monetary Authority of Singapore", "Bank Negara Malaysia", "Bank of Thailand"],
        day="2026-10-21",
    )
    primary.source_quality = "primary"
    primary.relevance_score = 32
    card = build_correlation_card(primary)

    secondary = item(
        "Singapore, Malaysia and Thailand central banks expand cross-border stablecoin initiative involving Ripple, Visa and Mastercard",
        "theblock-google-news-discovery",
        "theblock:bloom-new",
        ["Ripple", "Visa", "Mastercard"],
        day="2026-10-24",
    )
    secondary.source_quality = "high_signal"
    secondary.relevance_score = 55

    kept, suppressed = suppress_correlated_secondary_duplicates(
        [secondary], publish_score=30, history=[card]
    )

    assert kept == [secondary]
    assert suppressed == []
