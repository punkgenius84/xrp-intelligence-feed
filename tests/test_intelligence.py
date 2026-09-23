from intelligence.deduplication import deduplicate
from intelligence.configuration import (ConfigurationError, validate_thresholds,
                                        validate_word_groups)
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from main import normalize_item
from models import NewsItem
from datetime import datetime, timezone
import re
import pytest


def test_entity_detection_uses_alias_boundaries_and_title_summary():
    item = NewsItem("Ripple announces RLUSD on XRP Ledger", "https://example.com/a",
                    "Example", summary="Ripple USD is available.")
    assert detect_entities(item) == ["XRP", "RLUSD", "Ripple"]
    false_positive = NewsItem("Xrpnation and Secured Network", "https://example.com/b", "Example")
    assert detect_entities(false_positive) == []


def test_source_quality_classification_is_authority_based():
    item = NewsItem("Official release", "https://example.com", "Agency", authority_tier=1)
    assert classify_source_quality(item) == "primary"
    item.authority_tier = 2
    assert classify_source_quality(item) == "high_signal"
    item.authority_tier = 3
    assert classify_source_quality(item) == "discovery"


def test_relevance_score_has_explainable_reasons():
    item = NewsItem("Ripple and XRP settlement update", "https://example.com/a", "Agency",
                    summary="RLUSD and Ripple Payments move forward.", authority_tier=1,
                    detected_entities=["XRP", "Ripple", "RLUSD"])
    classify_source_quality(item)
    score, reasons = score_relevance(item)
    assert score == 59
    assert reasons == [
        "high keyword(s) XRP, Ripple, RLUSD (+15)",
        "medium keyword(s) settlement, payments (+7)",
        "context keyword(s) Ripple Payments (+4)",
        "primary source authority for relevant topic (+20)",
        "3 detected entities alongside direct XRP/Ripple signal (+8)",
        "title keyword(s) XRP, Ripple, settlement (+5)",
    ]
    assert "source_quality:primary" in item.score_signals


def test_primary_broad_tokenization_does_not_become_direct_xrp_signal():
    item = NewsItem("SEC adopts tokenized securities pilot", "https://example.com/a", "SEC",
                    authority_tier=1, detected_entities=["SEC", "Tokenization"])
    classify_source_quality(item)
    score, _ = score_relevance(item)
    assert score == 0
    assert "broad_tokenization" in item.relevance_categories
    assert "direct_xrp_xrpl" not in item.relevance_categories


def test_regulatory_action_requires_digital_asset_or_payment_context():
    broad = NewsItem("SEC announces new reporting rule for market infrastructure",
                     "https://example.com/b", "SEC", authority_tier=1,
                     detected_entities=["SEC"])
    relevant = NewsItem("SEC announces enforcement action involving crypto asset custody",
                        "https://example.com/r", "SEC", authority_tier=1,
                        detected_entities=["SEC"])
    classify_source_quality(broad)
    classify_source_quality(relevant)
    broad_score, _ = score_relevance(broad)
    relevant_score, reasons = score_relevance(relevant)
    assert broad_score == 0
    assert relevant_score > broad_score
    assert "relevant_regulatory_digital_asset_payments" in relevant.relevance_categories
    assert sum(int(match) for reason in reasons
               for match in re.findall(r"\(\+(\d+)\)", reason)) == relevant_score


def test_ripple_payment_network_signal_is_distinguished():
    item = NewsItem("Ripple Payments expands cross-border settlement",
                    "https://example.com/p", "Ripple", detected_entities=["Ripple", "Payments"])
    score, _ = score_relevance(item)
    assert score > 0
    assert "ripple_payments_network" in item.relevance_categories


def test_syndicated_headline_is_not_independent_confirmation():
    first = NewsItem("Ripple announces major XRP Ledger payment settlement launch",
                     "https://one.example/story", "One", source_id="one")
    copy = NewsItem("Ripple announces major XRP Ledger payment settlement launch",
                    "https://two.example/story", "Two", source_id="two")
    fresh, seen = deduplicate([first, copy])
    assert fresh == [first]
    assert first.duplicate_sources == ["two"]
    assert first.duplicate_urls == ["https://two.example/story"]
    assert first.event_fingerprint in seen


def test_normalized_copy_dedupes_against_saved_event_state():
    item = NewsItem("Ripple announces major XRP Ledger payment settlement launch",
                    "https://another.example/story", "Another")
    fresh, _ = deduplicate([item], {item.event_fingerprint})
    assert fresh == []


def test_near_headline_deduplication_preserves_provenance_within_event_day():
    stamp = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    first = NewsItem("Ripple announces major XRP Ledger payment settlement launch",
                     "https://one.example/story", "One", source_id="one", published_at=stamp)
    copy = NewsItem("Ripple announces major XRP Ledger payment settlement launched",
                    "https://two.example/story", "Two", source_id="two", published_at=stamp)
    fresh, _ = deduplicate([first, copy])
    assert fresh == [first]
    assert first.duplicate_sources == ["two"]


def test_recurring_exact_headline_on_another_day_is_a_new_event():
    title = "Ripple announces major XRP Ledger payment settlement launch"
    first = NewsItem(title, "https://one.example/day1", "One",
                     published_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
    second = NewsItem(title, "https://two.example/day2", "Two",
                      published_at=datetime(2026, 9, 23, 12, tzinfo=timezone.utc))
    fresh, _ = deduplicate([first, second])
    assert fresh == [first, second]


def test_missing_required_config_field_has_clear_error(tmp_path):
    config = tmp_path / "keywords.json"
    config.write_text('{"high": [], "medium": []}', encoding="utf-8")
    item = NewsItem("Ordinary news", "https://example.com", "Example")
    with pytest.raises(ConfigurationError, match="missing required fields: context"):
        score_relevance(item, keywords_path=config)


def test_entity_and_threshold_validation_report_required_fields(tmp_path):
    with pytest.raises(ConfigurationError, match="entities configuration missing required fields: finance"):
        validate_word_groups({"assets": {"XRP": ["XRP"]}, "companies": {"Ripple": ["Ripple"]},
                              "regulators": {"SEC": ["SEC"]}, "government": {"Treasury": ["Treasury"]}},
                             ("assets", "companies", "regulators", "government", "finance"), "entities")
    with pytest.raises(ConfigurationError, match="thresholds.weights missing required fields"):
        validate_thresholds({"publish_score": 35, "weights": {"high_keyword": 15}})



def test_normalization_removes_source_name_suffix():
    item = NewsItem("Ripple files update — U.S. Securities and Exchange Commission",
                    "https://example.com/a", "U.S. Securities and Exchange Commission")
    normalize_item(item)
    assert item.title == "Ripple files update"
