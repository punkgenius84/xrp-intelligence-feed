from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from discovery.dispatch import (DiscoveryRegistryError, collect_source,
                                load_discovery_sources, validate_discovery_sources)
from discovery.fincen_press_releases import (FINCEN_SOURCE_ID, FINCEN_URL,
                                              FinCENPressReleasesDiscovery,
                                              _article_identity, _parse_page)
from discovery.http import DiscoveryHttpError, HttpResponse
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "fincen_press_releases.html"


def source(**changes):
    result = {
        "source_id": FINCEN_SOURCE_ID,
        "name": "Financial Crimes Enforcement Network",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": "fincen_press_releases",
        "source_url": FINCEN_URL,
        "enabled": True,
        "lookback_days": 60,
        "max_items": 50,
    }
    result.update(changes)
    return result


def row(slug, title, published, category="News"):
    date_value = f'<div class="views-field views-field-field-date"><span>{published}</span></div>'
    return (f'<div class="views-row"><div class="views-field views-field-title">'
            f'<a href="/news/news-releases/{slug}">{title}</a></div>{date_value}'
            f'<div class="views-field views-field-field-news-type">{category}</div></div>')


def listing(*rows):
    return (('<html><body><h1>Press Releases</h1><div class="view view-press-releases '
             'view-id-press_releases"><div class="view-content">')
            + "".join(rows) + "</div></div></body></html>").encode()


def response(content=b"", *, status=200, headers=None):
    return HttpResponse(status, headers or {"content-type": "text/html", "etag": '"etag"'},
                        content, FINCEN_URL)


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def adapter(http, **changes):
    return FinCENPressReleasesDiscovery(source(**changes), http=http, now=lambda: STAMP)


def page_number(url):
    return parse_qs(urlsplit(url).query).get("page", [None])[0]


def progress(boundary_id="saved-release", boundary_date="2026-09-01", hint=1,
             status="active", lookback=60):
    return {"boundary_id": boundary_id, "boundary_date": boundary_date,
            "deep_page_hint": hint, "frontier_status": status, "lookback_days": lookback}


def state(value=None):
    return {"sources": {FINCEN_SOURCE_ID: {"pagination": {
        "fincen_press_releases": value if value is not None else progress()
    }}}}


def candidates(result):
    return {item.source_native_id: item for item in result.candidates}


def fixture_result(http=None, **changes):
    http = http or FakeHttp([response(FIXTURE.read_bytes())])
    return adapter(http, **changes).collect()


def test_registry_config_dispatch_and_enabled_source():
    configured = load_discovery_sources()
    fincen = next(item for item in configured if item["source_id"] == FINCEN_SOURCE_ID)
    assert fincen["lookback_days"] == 60
    assert fincen["max_items"] == 50
    http = FakeHttp([response(FIXTURE.read_bytes())])
    result = collect_source(fincen, http=http, now=lambda: STAMP)
    assert result.status == "success"
    assert page_number(http.calls[0][0]) == "0"


@pytest.mark.parametrize("changes", [
    {"source_url": "https://evil.example/news/press-releases"},
    {"discovery_method": "generic_scraping"},
    {"enabled": 1},
    {"lookback_days": 0},
    {"max_items": 101},
])
def test_registry_rejects_invalid_fincen_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_parses_actual_listing_fields_and_current_fixture_releases():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    by_slug = {item["native_id"]: item for item in rows}
    assert by_slug["fincen-identifies-nearly-13-billion-linked-suspected-digital-asset-scams"]["date"] == date(2026, 9, 3)
    genius = by_slug["fincen-agencies-propose-rule-implement-genius-act-customer-identification"]
    assert genius["date"] == date(2026, 6, 18)
    assert genius["title"] == "FinCEN, Agencies Propose Rule to Implement GENIUS Act Customer Identification Program Requirement"
    assert genius["category"] == "News"


def test_publication_date_comes_from_its_field_not_a_date_in_the_title():
    body = listing(row("date-check", "FinCEN discusses a June 18, 2020 GENIUS Act issue",
                       "09/03/2026"))
    parsed, complete = _parse_page(body)
    assert complete is True
    assert parsed[0]["date"] == date(2026, 9, 3)


@pytest.mark.parametrize(("value", "expected"), [
    ("https://www.fincen.gov/news/news-releases/example-release", "example-release"),
    ("/news/news-releases/example-release/", "example-release"),
    ("https://www.fincen.gov:443/news/news-releases/example-release", "example-release"),
])
def test_validates_and_canonicalizes_official_article_routes(value, expected):
    assert _article_identity(value) == (
        expected, f"https://www.fincen.gov/news/news-releases/{expected}")


@pytest.mark.parametrize("value", [
    "http://www.fincen.gov/news/news-releases/example-release",
    "https://evil.example/news/news-releases/example-release",
    "https://www.fincen.gov.evil.example/news/news-releases/example-release",
    "//evil.example/news/news-releases/example-release",
    "https://user@www.fincen.gov/news/news-releases/example-release",
    "https://www.fincen.gov:444/news/news-releases/example-release",
    "https://www.fincen.gov/news/press-releases/example-release",
    "https://www.fincen.gov/news/news-releases/../other",
    "https://www.fincen.gov/news/news-releases/Bad-Slug",
])
def test_rejects_external_downgraded_or_malformed_article_urls(value):
    assert _article_identity(value) is None


@pytest.mark.parametrize("bad_row", [
    '<div class="views-row"><div class="views-field views-field-title"><a href="/news/news-releases/example-release">No date</a></div></div>',
    '<div class="views-row"><div class="views-field views-field-title"><a href="/news/news-releases/example-release">Bad date</a></div><div class="views-field-field-date">yesterday</div></div>',
    '<div class="views-row"><div class="views-field views-field-title"><a href="/news/news-releases/example-release"></a></div><div class="views-field-field-date">09/03/2026</div></div>',
    '<div class="views-row"><div class="views-field views-field-title"><a href="https://evil.example/news/news-releases/example-release">External</a></div><div class="views-field-field-date">09/03/2026</div></div>',
    '<div class="views-row"><div class="views-field views-field-title"><a href="/news/news-releases/example-release">No category</a></div><div class="views-field-field-date">09/03/2026</div></div>',
])
def test_malformed_row_is_not_fabricated_and_marks_page_incomplete(bad_row):
    parsed, complete = _parse_page(listing(bad_row))
    assert parsed == []
    assert complete is False


def test_wrong_page_structure_fails_and_well_formed_empty_listing_succeeds():
    with pytest.raises(ValueError, match="listing structure"):
        _parse_page(b"<html><body><div>No listing here</div></body></html>")
    parsed, complete = _parse_page(listing())
    assert parsed == []
    assert complete is True


def test_first_run_initializes_oldest_valid_boundary_without_crawling_history():
    http = FakeHttp([response(FIXTURE.read_bytes())])
    result = adapter(http, lookback_days=180).collect()
    assert len(http.calls) == 1
    assert page_number(http.calls[0][0]) == "0"
    state_value = result.pagination["fincen_press_releases"]
    assert state_value == progress(
        "fincen-agencies-propose-rule-implement-genius-act-customer-identification",
        "2026-06-18", hint=1, lookback=180,
    )


def test_configured_lookback_filters_out_old_listing_rows():
    result = fixture_result()
    found = candidates(result)
    assert "fincen-identifies-nearly-13-billion-linked-suspected-digital-asset-scams" in found
    assert "fincen-agencies-propose-rule-implement-genius-act-customer-identification" not in found


def test_item_limit_bounds_candidates_and_prevents_validator_or_progress_advance():
    body = listing(*(row(f"release-{i}", f"FinCEN release {i}", "09/22/2026")
                     for i in range(4)))
    result = adapter(FakeHttp([response(body)]), max_items=2).collect()
    assert len(result.candidates) == 2
    assert result.status == "partial"
    assert "page_0" not in result.state_updates
    assert "fincen_press_releases" not in result.pagination


def test_page_zero_is_always_requested_and_boundary_found_shallow():
    page0 = listing(row("newest", "FinCEN digital asset action", "09/22/2026"),
                    row("saved-release", "Saved boundary", "09/01/2026"))
    http = FakeHttp([response(page0), response(listing(row("older", "Older release", "08/31/2026"))),
                     response(listing())])
    result = adapter(http).collect(state())
    assert [page_number(call[0]) for call in http.calls] == ["0", "1", "2"]
    assert result.pagination["fincen_press_releases"]["boundary_id"] == "older"
    assert result.pagination["fincen_press_releases"]["deep_page_hint"] == 2


def test_boundary_found_on_deep_page_and_frontier_advances_after_complete_intervening_pages():
    page0 = listing(row("newest", "New release", "09/22/2026"))
    page1 = listing(row("between", "Between releases", "09/10/2026"),
                    row("saved-release", "Saved boundary", "09/01/2026"))
    page2 = listing(row("older", "Older release", "08/31/2026"))
    http = FakeHttp([response(page0), response(page1), response(page2)])
    result = adapter(http).collect(state(progress(hint=1)))
    assert [page_number(call[0]) for call in http.calls] == ["0", "1", "2"]
    progress_after = result.pagination["fincen_press_releases"]
    assert progress_after["boundary_id"] == "older"
    assert progress_after["deep_page_hint"] == 3


def test_boundary_recovery_advances_only_page_hint_when_boundary_not_found():
    http = FakeHttp([response(listing(row("newest", "New release", "09/22/2026"))),
                     response(listing(row("one", "One", "09/10/2026"))),
                     response(listing(row("two", "Two", "09/09/2026")))])
    result = adapter(http).collect(state(progress(hint=3)))
    updated = result.pagination["fincen_press_releases"]
    assert updated["boundary_id"] == "saved-release"
    assert updated["deep_page_hint"] == 5
    assert len(http.calls) == 3


@pytest.mark.parametrize("failure", [
    DiscoveryHttpError("timeout", kind="timeout"),
    response(b"<html><div>changed structure</div></html>"),
])
def test_failed_or_malformed_deep_page_does_not_advance_frontier(failure):
    page0 = listing(row("newest", "New release", "09/22/2026"),
                    row("saved-release", "Saved boundary", "09/01/2026"))
    http = FakeHttp([response(page0), failure])
    result = adapter(http).collect(state())
    assert result.status == "partial"
    assert "fincen_press_releases" not in result.pagination
    assert result.candidates


def test_partial_page_keeps_valid_candidates_but_not_its_validator_or_frontier():
    malformed = listing(row("good-release", "FinCEN XRP release", "09/22/2026"),
                        '<div class="views-row"><div class="views-field views-field-title"><a href="/news/news-releases/bad-release">Bad date</a></div><div class="views-field-field-date">unknown</div></div>')
    http = FakeHttp([response(malformed)])
    result = adapter(http).collect()
    assert result.status == "partial"
    assert set(candidates(result)) == {"good-release"}
    assert "page_0" not in result.state_updates
    assert "fincen_press_releases" not in result.pagination


def test_expired_boundary_is_preserved_and_not_rebased():
    http = FakeHttp([response(FIXTURE.read_bytes())])
    result = adapter(http).collect(state(progress("old", "2026-06-01", hint=4)))
    updated = result.pagination["fincen_press_releases"]
    assert updated["boundary_id"] == "old"
    assert updated["frontier_status"] == "boundary_expired"
    assert updated["deep_page_hint"] == 4
    assert len(http.calls) == 1


@pytest.mark.parametrize("bad", [
    [], {"boundary_id": "BAD SLUG", "boundary_date": "2026-09-01", "deep_page_hint": 1,
         "frontier_status": "active", "lookback_days": 60},
    {"boundary_id": "saved-release", "boundary_date": "not-a-date", "deep_page_hint": 1,
     "frontier_status": "active", "lookback_days": 60},
    {"boundary_id": "saved-release", "boundary_date": "2026-09-01", "deep_page_hint": 0,
     "frontier_status": "active", "lookback_days": 60},
])
def test_malformed_saved_pagination_fails_closed_without_request(bad):
    http = FakeHttp([])
    result = adapter(http).collect(state(bad))
    assert result.status == "failed"
    assert "refusing to reset" in result.errors[0]
    assert http.calls == []


def test_validator_headers_persist_separately_per_page_and_are_sent_on_later_run():
    first = FakeHttp([response(listing(row("newest", "New", "09/22/2026")),
                               headers={"content-type": "text/html", "etag": '"page0"',
                                        "last-modified": "Tue, 22 Sep 2026 12:00:00 GMT"})])
    result = adapter(first).collect()
    assert result.state_updates["page_0"] == {"etag": '"page0"',
                                               "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    saved = state(result.pagination["fincen_press_releases"])
    saved["sources"][FINCEN_SOURCE_ID]["requests"] = {
        "page_0": {"etag": '"page0"', "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    }
    next_http = FakeHttp([response(status=304, headers={"etag": '"page0"'})])
    unchanged = adapter(next_http).collect(saved)
    assert next_http.calls[0][1]["etag"] == '"page0"'
    assert unchanged.status == "not_modified"


def test_304_does_not_rebase_or_advance_existing_frontier():
    http = FakeHttp([response(status=304, headers={})])
    result = adapter(http).collect(state())
    assert result.status == "not_modified"
    assert result.pagination["fincen_press_releases"] == progress()
    assert page_number(http.calls[0][0]) == "0"


def test_candidates_from_successful_page_survive_later_deep_request_failure():
    page0 = listing(row("newest", "FinCEN announces digital asset guidance", "09/22/2026"),
                    row("saved-release", "Saved boundary", "09/01/2026"))
    http = FakeHttp([response(page0), DiscoveryHttpError("timeout", kind="timeout")])
    result = adapter(http).collect(state())
    assert result.status == "partial"
    assert "newest" in candidates(result)
    assert "fincen_press_releases" not in result.pagination


def test_overlapping_pages_deduplicate_article_and_retain_listing_provenance():
    duplicate = row("saved-release", "Saved boundary", "09/01/2026")
    page0 = listing(row("newest", "New release", "09/22/2026"), duplicate)
    page1 = listing(duplicate, row("older", "Older release", "08/31/2026"))
    http = FakeHttp([response(page0), response(page1), response(listing())])
    result = adapter(http).collect(state())
    same = [item for item in result.candidates if item.source_native_id == "saved-release"]
    assert len(same) == 1
    assert len(same[0].provenance) >= 2
    assert same[0].candidate_id == "fincen-press-releases:saved-release"


def test_maximum_three_logical_page_fetches_and_never_fetches_article_links():
    pages = [listing(row("new", "New release", "09/22/2026"),
                     row("saved-release", "Saved boundary", "09/01/2026")),
             listing(row("older-1", "Older one", "08/31/2026")),
             listing(row("older-2", "Older two", "08/30/2026"))]
    http = FakeHttp([response(page) for page in pages])
    result = adapter(http).collect(state())
    assert result.pagination["page_fetches"] == 3
    assert len(http.calls) == 3
    assert all(urlsplit(call[0]).path == "/news/press-releases" for call in http.calls)


def test_candidate_identity_is_canonical_and_stable_across_runs():
    body = listing(row("stable-release", "FinCEN XRP release", "09/22/2026"))
    ids = []
    for _ in range(2):
        result = adapter(FakeHttp([response(body)])).collect()
        item = candidates(result)["stable-release"]
        ids.append((item.candidate_id, item.source_native_id, item.url))
    assert ids[0] == ids[1]
    assert ids[0][0] == "fincen-press-releases:stable-release"


def test_fincen_authority_alone_does_not_make_generic_release_relevant():
    item = NewsItem(title="FinCEN announces an office renovation", url="https://www.fincen.gov/",
                    source="FinCEN", source_id=FINCEN_SOURCE_ID, authority_tier=1,
                    category="regulatory", summary="FinCEN Press Release — News")
    detect_entities(item)
    classify_source_quality(item)
    score_relevance(item)
    assert item.detected_entities == ["FinCEN"]
    assert item.relevance_score == 0


@pytest.mark.parametrize("title", [
    "FinCEN proposes rule to implement GENIUS Act payment stablecoin requirements",
    "FinCEN identifies digital asset scams involving XRP",
    "FinCEN issues guidance on virtual currency and digital asset service providers",
])
def test_configured_fincen_digital_asset_signals_reach_existing_relevance_pipeline(title):
    item = NewsItem(title=title, url="https://www.fincen.gov/news/news-releases/example",
                    source="FinCEN", source_id=FINCEN_SOURCE_ID, authority_tier=1,
                    category="regulatory", summary="FinCEN Press Release — News")
    detect_entities(item)
    classify_source_quality(item)
    score_relevance(item)
    assert "FinCEN" in item.detected_entities
    assert item.relevance_score > 0


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = adapter(http, enabled=False).collect()
    assert result.status == "not_configured"
    assert http.calls == []


def test_pagination_only_updates_persist_through_main_pipeline(tmp_path, monkeypatch):
    import main
    from discovery.base import DiscoveryResult
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    saved_progress = progress("boundary", "2026-09-01", hint=2)
    result = DiscoveryResult(
        FINCEN_SOURCE_ID, "fincen_press_releases", "success", fetched_at=STAMP,
        pagination={"page_fetches": 1, "fincen_press_releases": saved_progress},
    )
    monkeypatch.setattr(main, "collect_source", lambda configured, state: result)
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    main.run_pipeline(
        sources=[], state=JsonState(str(tmp_path / "seen.json")),
        discovery_sources=[source()], discovery_state=store,
    )
    persisted = store.load()["sources"][FINCEN_SOURCE_ID]["pagination"]
    assert persisted["fincen_press_releases"] == saved_progress
