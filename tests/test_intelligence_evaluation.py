from types import SimpleNamespace

import pytest

import scripts.evaluate_intelligence as evaluation
from intelligence.llm.base import LLMUnavailable
from scripts.evaluate_intelligence import analyze_with_retry, check_analysis, load_cases


def test_fixed_intelligence_evaluation_set_has_ten_sourced_cases():
    cases = load_cases()
    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == 10
    assert all(case["source_url"].startswith("https://") for case in cases)
    assert all(case["expected_event_types"] for case in cases)


def test_evaluation_detects_wrong_event_type_and_xrp_conflation():
    case = {
        "expected_event_types": ["product_launch"],
        "must_match": ["RLUSD", "stablecoin"],
        "must_not_match": ["XRP is the stablecoin"],
    }
    analysis = SimpleNamespace(
        event_type="partnership",
        event_summary="RLUSD is a stablecoin; XRP is the stablecoin.",
        significance="This is a launch.",
        entities=["RLUSD"],
        claims=[],
        conflicts=[],
        uncertainties=[],
    )
    failures = check_analysis(case, analysis)
    assert any("event_type=" in failure for failure in failures)
    assert any("forbidden pattern" in failure for failure in failures)


def test_evaluation_detects_missing_required_context():
    case = {
        "expected_event_types": ["acquisition"],
        "must_match": ["Hidden Road", "Ripple Prime"],
        "must_not_match": ["XRP is collateral for every product"],
    }
    analysis = SimpleNamespace(
        event_type="acquisition",
        event_summary="Ripple completed an acquisition.",
        significance="The company expanded its business.",
        entities=["Ripple"],
        claims=[],
        conflicts=[],
        uncertainties=[],
    )
    failures = check_analysis(case, analysis)
    assert any("Hidden Road" in failure for failure in failures)
    assert any("Ripple Prime" in failure for failure in failures)


def test_evaluation_retries_one_transient_ollama_failure(monkeypatch):
    expected = SimpleNamespace(event_type="partnership")
    calls = 0

    def flaky_analysis(item, provider):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMUnavailable("Ollama unavailable: ReadTimeout")
        return expected

    monkeypatch.setattr(evaluation, "analyze_item", flaky_analysis)
    result = analyze_with_retry(SimpleNamespace(candidate_id="case-1"), object())

    assert result is expected
    assert calls == 2


def test_evaluation_does_not_retry_after_retry_budget_is_exhausted(monkeypatch):
    calls = 0

    def unavailable(item, provider):
        nonlocal calls
        calls += 1
        raise LLMUnavailable("Ollama unavailable: ReadTimeout")

    monkeypatch.setattr(evaluation, "analyze_item", unavailable)
    with pytest.raises(LLMUnavailable):
        analyze_with_retry(SimpleNamespace(candidate_id="case-1"), object())

    assert calls == 2
