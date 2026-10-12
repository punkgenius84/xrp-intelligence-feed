from types import SimpleNamespace

from scripts.evaluate_intelligence import check_analysis, load_cases


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
