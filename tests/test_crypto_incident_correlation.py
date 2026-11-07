from datetime import datetime, timezone

from intelligence.correlation import correlate
from models import NewsItem


def make_item(candidate_id, title, day, entities):
    published_at = datetime(2026, 11, day, 12, 0, tzinfo=timezone.utc)
    return NewsItem(
        candidate_id=candidate_id,
        title=title,
        summary=title,
        url=f"https://example.test/{candidate_id}",
        source=candidate_id,
        source_id=candidate_id,
        detected_entities=entities,
        published_at=published_at,
        collected_at=published_at,
    )


def test_binance_hack_follow_up_updates_correlate_across_two_days():
    reopening = make_item(
        "reopening",
        "Binance opens crypto withdrawals in phases starting with bitcoin following a $6.4 billion hack",
        1,
        ["Binance"],
    )
    attribution = make_item(
        "attribution",
        "North Korea denies role in $6.4 billion Binance hack after FBI points to TraderTraitor",
        3,
        ["Binance"],
    )

    correlate([reopening, attribution])

    assert "attribution" in reopening.correlated_candidate_ids
    assert "reopening" in attribution.correlated_candidate_ids
    assert reopening.correlation_score >= 60
    assert attribution.correlation_score >= 60


def test_different_binance_security_incident_does_not_correlate_across_days():
    first = make_item(
        "first",
        "Binance reports a $6.4 billion hack and opens phased withdrawals",
        1,
        ["Binance"],
    )
    unrelated = make_item(
        "unrelated",
        "Binance investigates a separate $2.1 billion exchange breach",
        3,
        ["Binance"],
    )

    correlate([first, unrelated])

    assert unrelated.correlated_candidate_ids == []
    assert first.correlated_candidate_ids == []


def test_ordinary_binance_updates_keep_the_one_day_correlation_window():
    first = make_item(
        "first",
        "Binance launches new bitcoin trading feature",
        1,
        ["Binance"],
    )
    second = make_item(
        "second",
        "Binance launches new bitcoin trading feature for customers",
        4,
        ["Binance"],
    )

    correlate([first, second])

    assert second.correlated_candidate_ids == []
    assert first.correlated_candidate_ids == []


def test_binance_follow_up_correlates_when_reported_amounts_differ_slightly():
    first = make_item(
        "first",
        "Binance says withdrawals normalised after $6.4 billion hack",
        1,
        ["Binance"],
    )
    second = make_item(
        "second",
        "Coinbase says cold storage protects assets after Binance $6.3 billion breach",
        3,
        ["Binance", "Coinbase"],
    )

    correlate([first, second])

    assert "second" in first.correlated_candidate_ids
    assert "first" in second.correlated_candidate_ids


def test_binance_follow_up_does_not_merge_a_materially_different_incident_amount():
    first = make_item(
        "first",
        "Binance reports a $6.4 billion hack and opens phased withdrawals",
        1,
        ["Binance"],
    )
    second = make_item(
        "second",
        "Binance investigates a separate $2.1 billion exchange breach",
        3,
        ["Binance"],
    )

    correlate([first, second])

    assert second.correlated_candidate_ids == []
    assert first.correlated_candidate_ids == []


def test_incident_amount_correlation_survives_a_prior_run():
    from intelligence.correlation import build_correlation_card

    first = make_item(
        "first",
        "Binance reports a $6.4 billion hack and opens phased withdrawals",
        1,
        ["Binance"],
    )
    history_card = build_correlation_card(first)
    follow_up = make_item(
        "follow-up",
        "Coinbase says cold storage protects assets after Binance $6.3 billion breach",
        3,
        ["Binance", "Coinbase"],
    )

    correlate([follow_up], history=[history_card])

    assert "first" in follow_up.correlated_candidate_ids
    assert follow_up.correlation_score >= 60
