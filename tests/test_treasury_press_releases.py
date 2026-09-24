from datetime import date, datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from discovery.dispatch import (DiscoveryRegistryError, collect_source,
                                load_discovery_sources, validate_discovery_sources)
from discovery.http import (BoundedHttpClient, DiscoveryHttpError, HttpResponse)
from discovery.treasury_press_releases import (TREASURY_SOURCE_ID, TREASURY_URL,
                                               TreasuryPressReleasesDiscovery,
                                               _article_identity, _parse_page)
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from models import NewsItem
from storage.discovery_state import JsonDiscoveryState


STAMP = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "treasury_press_releases.html"


def source(**changes):
    result = {
        "source_id": TREASURY_SOURCE_ID,
        "name": "U.S. Department of the Treasury",
        "authority_tier": 1,
        "category": "government",
        "discovery_method": "treasury_press_releases",
        "source_url": TREASURY_URL,
        "enabled": True,
        "lookback_days": 60,
        "max_items": 50,
    }
    result.update(changes)
    return result


def row(release_id, title, published, category=""):
    try:
        day = datetime.strptime(published, "%B %d, %Y").date()
    except ValueError:
        day = date.fromisoformat(published[:10])
    category_html = (f'<span class="subcategory"><a '
                     f'href="/news/press-releases/statements-remarks">{category}</a></span>'
                     if category else "")
    return (f'<div><span class="date-format"><time datetime="{day.isoformat()}T12:00:00Z" '
            f'class="datetime">{published}</time></span><span>{category_html}</span>'
            f'<h3 class="featured-stories__headline"><a '
            f'href="/news/press-releases/{release_id}/">{title}</a></h3></div>')


def raw_row(href, title, published=None):
    date_html = ""
    if published is not None:
        try:
            day = datetime.strptime(published, "%B %d, %Y").date()
        except ValueError:
            try:
                day = date.fromisoformat(published[:10])
            except ValueError:
                day = None
        datetime_attr = f' datetime="{day.isoformat()}T12:00:00Z"' if day else ""
        date_html = (f'<span class="date-format"><time{datetime_attr} '
                     f'class="datetime">{published}</time></span>')
    return (f'<div>{date_html}<h3 class="featured-stories__headline">'
            f'<a href="{href}">{title}</a></h3></div>')


def listing(*rows):
    return (('<html><body><h2>Press Releases</h2><div class="featured-stories content--2col">'
             '<div class="content--2col__body" data-news-list data-news-search-list '
             'data-news-category="press-releases" data-news-layout="standard" '
             'data-news-manifest="/news-data/press-releases/manifest.json" '
             'data-news-page-size="10" data-news-total="100" '
             'data-news-base-path="/news/press-releases/"/>')
            + "".join(rows) + '<nav class="pager" data-news-pager></nav>'
            + "</div></div></body></html>").encode()


def response(content=b"", *, status=200, headers=None):
    return HttpResponse(status, headers or {"content-type": "text/html", "etag": '"etag"'},
                        content, TREASURY_URL)


def json_response(payload, year=2026, headers=None):
    return HttpResponse(200, headers or {"content-type": "application/json", "etag": '"shard"'},
                        json.dumps(payload).encode(),
                        f"https://home.treasury.gov/news-data/press-releases/search/{year}.json")


def _search_item(html_row):
    parsed, complete = _parse_page(listing(html_row))
    if not complete or len(parsed) != 1:
        raise ValueError("test row must be a valid non-statement listing record")
    item = parsed[0]
    category = "" if item["category"] == "Press Release" else item["category"]
    subcategory = ({"title": category, "slug": "statements-remarks",
                    "url": "/news/press-releases/statements-remarks"} if category else {})
    return {"date": item["date"].isoformat(), "title": item["title"],
            "url": f"/news/press-releases/{item['native_id']}/",
            "subcategory": subcategory}


def _filler(index):
    return {"date": "2026-08-01", "title": f"Remarks filler {index}",
            "url": f"/news/press-releases/sb9{index:03d}/",
            "subcategory": {"title": "Statements & Remarks", "slug": "statements-remarks"}}


def shard_for_pages(page0_rows, **pages):
    page_rows = {0: page0_rows}
    for key, rows in pages.items():
        page_rows[int(key.removeprefix("page"))] = rows
    items = []
    for page_index in range(max(page_rows, default=0) + 1):
        rows = page_rows.get(page_index, ())
        page_items = [_search_item(item) for item in rows]
        items.extend(page_items)
        items.extend(_filler(page_index * 10 + i) for i in range(len(page_items), 10))
    return json_response({"category": "press-releases", "count": len(items),
                          "items": items, "layout": "standard", "range": {}})


def shard_payload(items):
    return {"category": "press-releases", "count": len(items), "items": items,
            "layout": "standard", "range": {}}


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


class NetworkResponse:
    def __init__(self, status=200, headers=None, content=b""):
        self.status_code = status
        self.headers = headers or {"content-type": "application/json"}
        self.content = content
        self.closed = False

    def iter_content(self, chunk_size):
        if self.content:
            yield self.content

    def close(self):
        self.closed = True


class NetworkSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(http, *, at=STAMP, **changes):
    return TreasuryPressReleasesDiscovery(source(**changes), http=http, now=lambda: at)


def page_number(url):
    return parse_qs(urlsplit(url).query).get("page", [None])[0]


def progress(boundary_id="sb0560", boundary_date="2026-09-01", hint=1,
             status="active"):
    return {"boundary_id": boundary_id, "boundary_date": boundary_date,
            "deep_page_hint": hint, "frontier_status": status}


def state(value="default", requests=None):
    if value == "default":
        value = progress()
    source_state = {"pagination": {"treasury_press_releases": value}}
    if requests is not None:
        source_state["requests"] = requests
    return {"sources": {TREASURY_SOURCE_ID: source_state}}


def candidate_map(result):
    return {item.source_native_id: item for item in result.candidates}


def test_registry_config_dispatch_and_enabled_source():
    configured = load_discovery_sources()
    treasury = next(item for item in configured if item["source_id"] == TREASURY_SOURCE_ID)
    assert treasury == source()
    http = FakeHttp([response(FIXTURE.read_bytes())])
    result = collect_source(treasury, http=http, now=lambda: STAMP)
    assert result.status == "success"
    assert page_number(http.calls[0][0]) is None  # first browser page has no query


@pytest.mark.parametrize("changes", [
    {"source_url": "https://evil.example/news/press-releases"},
    {"discovery_method": "generic_scraping"}, {"enabled": 1},
    {"lookback_days": 0}, {"max_items": 101}, {"category": "regulatory"},
])
def test_registry_rejects_invalid_treasury_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version": 1, "sources": [source(**changes)]})


def test_parses_listing_fields_current_material_and_excludes_statements():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    by_id = {item["native_id"]: item for item in rows}
    assert by_id["sb0632"]["date"] == date(2026, 9, 17)
    assert by_id["sb0605"]["date"] == date(2026, 8, 17)
    assert "GENIUS Act" in by_id["sb0605"]["title"]
    assert by_id["sb0631"]["category"] == "Press Release"
    assert "sb0633" not in by_id


def test_publication_date_comes_from_specific_field_not_title_text():
    parsed, complete = _parse_page(listing(row(
        "sb0634", "Treasury discusses June 18, 2020 stablecoin policy", "September 3, 2026")))
    assert complete is True
    assert parsed[0]["date"] == date(2026, 9, 3)


@pytest.mark.parametrize(("value", "expected"), [
    ("https://home.treasury.gov/news/press-releases/sb0560", "sb0560"),
    ("/news/press-releases/sb0560/", "sb0560"),
    ("https://home.treasury.gov:443/news/press-releases/sb0560", "sb0560"),
    ("/news/press-releases/TG993", "TG993"),
])
def test_validates_and_canonicalizes_official_treasury_routes(value, expected):
    assert _article_identity(value) == (
        expected, f"https://home.treasury.gov/news/press-releases/{expected}")


@pytest.mark.parametrize("value", [
    "http://home.treasury.gov/news/press-releases/sb0560",
    "https://evil.example/news/press-releases/sb0560",
    "https://home.treasury.gov.evil.example/news/press-releases/sb0560",
    "//evil.example/news/press-releases/sb0560",
    "https://user@home.treasury.gov/news/press-releases/sb0560",
    "https://home.treasury.gov:444/news/press-releases/sb0560",
    "https://home.treasury.gov/news/releases/sb0560",
    "https://home.treasury.gov/news/press-releases/../other",
    "https://home.treasury.gov/news/press-releases/!!!",
    "",
    None,
])
def test_rejects_downgraded_external_or_malformed_article_routes(value):
    assert _article_identity(value) is None


@pytest.mark.parametrize("bad_row", [
    raw_row("/news/press-releases/sb0001", "No date"),
    raw_row("/news/press-releases/sb0001", "Bad date", "yesterday"),
    raw_row("/news/press-releases/sb0001", "", "September 3, 2026"),
    raw_row("https://evil.example/news/press-releases/sb0001", "External", "September 3, 2026"),
    raw_row("/news/press-releases/!!!", "Bad identity", "September 3, 2026"),
])
def test_malformed_rows_are_skipped_and_mark_page_incomplete(bad_row):
    parsed, complete = _parse_page(listing(bad_row))
    assert parsed == []
    assert complete is False


def test_wrong_page_structure_fails_and_empty_listing_is_valid():
    with pytest.raises(ValueError, match="listing structure"):
        _parse_page(b"<html><body><div>unrelated page</div></body></html>")
    parsed, complete = _parse_page(listing())
    assert parsed == []
    assert complete is True


def test_first_run_initializes_oldest_valid_boundary_on_page_zero_only():
    http = FakeHttp([response(FIXTURE.read_bytes())])
    result = adapter(http, lookback_days=180).collect()
    assert len(http.calls) == 1
    assert page_number(http.calls[0][0]) is None
    assert result.pagination["treasury_press_releases"] == progress(
        "sb0560", "2026-07-14", hint=1)


def test_configured_lookback_filters_old_items_without_rebasing_frontier():
    result = adapter(FakeHttp([response(FIXTURE.read_bytes())])).collect()
    found = candidate_map(result)
    assert "sb0632" in found and "sb0605" in found
    assert "sb0560" not in found


def test_fifty_item_limit_bounds_processing_and_frontier_validator_advance():
    body = listing(*(row(f"sb{i:04d}", f"Treasury release {i}", "September 22, 2026")
                     for i in range(51)))
    http = FakeHttp([response(body)])
    result = adapter(http).collect()
    assert len(result.candidates) == 50
    assert result.status == "partial"
    assert "page_0" not in result.state_updates
    assert "treasury_press_releases" not in result.pagination


def test_page_zero_refresh_and_boundary_found_shallow_advances_frontier():
    page0 = listing(row("sb0632", "New digital asset release", "September 22, 2026"),
                    row("sb0560", "Saved boundary", "September 1, 2026"))
    page1 = [row("sb0559", "Older release", "August 31, 2026")]
    http = FakeHttp([response(page0), shard_for_pages(
        [row("sb0632", "New digital asset release", "September 22, 2026"),
         row("sb0560", "Saved boundary", "September 1, 2026")], page1=page1)])
    result = adapter(http).collect(state())
    assert len(http.calls) == 2
    assert page_number(adapter(FakeHttp([]))._page_url(0)) is None
    assert page_number(adapter(FakeHttp([]))._page_url(1)) == "2"
    assert result.pagination["treasury_press_releases"]["boundary_id"] == "sb0559"
    assert result.pagination["treasury_press_releases"]["deep_page_hint"] == 2


def test_boundary_found_on_deep_page_advances_after_intervening_pages():
    page0 = [row("sb0632", "New release", "September 22, 2026")]
    page1 = [row("sb0561", "Between", "September 10, 2026"),
             row("sb0560", "Saved boundary", "September 1, 2026")]
    page2 = [row("sb0559", "Older release", "August 31, 2026")]
    http = FakeHttp([response(listing(*page0)),
                     shard_for_pages(page0, page1=page1, page2=page2)])
    result = adapter(http).collect(state(progress(hint=1)))
    assert result.pagination["treasury_press_releases"] == progress(
        "sb0559", "2026-08-31", hint=3)


def test_boundary_not_found_moves_only_search_hint_after_successful_probes():
    page0 = [row("sb0632", "New", "September 22, 2026")]
    http = FakeHttp([response(listing(*page0)),
                     shard_for_pages(page0,
                                     page3=[row("sb0558", "Older one", "August 30, 2026")],
                                     page4=[row("sb0557", "Older two", "August 29, 2026")])])
    result = adapter(http).collect(state(progress(hint=3)))
    assert result.pagination["treasury_press_releases"] == progress(hint=5)
    assert len(http.calls) == 2


@pytest.mark.parametrize("failure", [
    DiscoveryHttpError("timeout", kind="timeout"),
    response(b"<html><div>changed markup</div></html>"),
])
def test_failed_or_malformed_deep_page_retains_candidates_but_not_frontier(failure):
    first = listing(row("sb0632", "New release", "September 22, 2026"),
                    row("sb0560", "Saved boundary", "September 1, 2026"))
    result = adapter(FakeHttp([response(first), failure])).collect(state())
    assert result.status == "partial"
    assert "sb0632" in candidate_map(result)
    assert "treasury_press_releases" not in result.pagination


def test_page_zero_validator_is_transactional_and_failed_shard_is_retried_next_run():
    saved = state(requests={"page_0": {"etag": '"old-page"'}})
    page0 = listing(row("sb0632", "New release", "September 22, 2026"))
    failed = adapter(FakeHttp([
        response(page0, headers={"content-type": "text/html", "etag": '"new-page"'}),
        DiscoveryHttpError("shard unavailable", kind="http_error", status_code=503),
    ])).collect(saved)
    assert failed.status == "partial"
    assert "page_0" not in failed.state_updates
    assert "treasury_press_releases" not in failed.pagination
    assert JsonDiscoveryState.request_validators(saved, TREASURY_SOURCE_ID, "page_0") == {
        "etag": '"old-page"'}

    complete_shard = shard_for_pages(
        [row("sb0632", "New release", "September 22, 2026")],
        page1=[row("sb0560", "Saved boundary", "September 1, 2026")],
        page2=[row("sb0559", "Older release", "August 31, 2026")],
    )
    retry_http = FakeHttp([
        response(page0, headers={"content-type": "text/html", "etag": '"new-page"'}),
        complete_shard,
    ])
    retried = adapter(retry_http).collect(saved)
    assert retry_http.calls[0][1]["etag"] == '"old-page"'
    assert len(retry_http.calls) == 2
    assert retried.pagination["treasury_press_releases"]["boundary_id"] == "sb0559"
    assert retried.state_updates["page_0"]["etag"] == '"new-page"'
    assert retried.state_updates["search_2026"]["etag"] == '"shard"'


def test_shard_retry_exhaustion_does_not_persist_validators_or_frontier():
    page0 = NetworkResponse(
        headers={"content-type": "text/html", "etag": '"page0-new"'},
        content=listing(row("sb0632", "New release", "September 22, 2026")),
    )
    session = NetworkSession([page0, NetworkResponse(503), NetworkResponse(503),
                              NetworkResponse(503)])
    http = BoundedHttpClient(user_agent="TreasuryTest/1", session=session,
                             sleep=lambda _: None)
    saved = state(requests={"page_0": {"etag": '"page0-old"'}})
    result = adapter(http).collect(saved)
    assert result.status == "partial"
    assert len(session.calls) == 4  # one page-0 send plus three exhausted shard attempts
    assert "page_0" not in result.state_updates
    assert "search_2026" not in result.state_updates
    assert "treasury_press_releases" not in result.pagination


def test_actual_outbound_budget_counts_retries_and_redirects_across_shards():
    published = listing(row("sb0632", "New release", "January 15, 2026"))
    redirects_and_retries = [
        NetworkResponse(302, {"Location": "/r1"}),
        NetworkResponse(302, {"Location": "/r2"}),
        NetworkResponse(302, {"Location": "/r3"}),
        NetworkResponse(503), NetworkResponse(503),
        NetworkResponse(headers={"content-type": "text/html"}, content=published),
        NetworkResponse(headers={"content-type": "application/json"},
                        content=json.dumps(shard_payload([])).encode()),
        NetworkResponse(302, {"Location": "/s1"}),
        NetworkResponse(302, {"Location": "/s2"}),
    ]
    session = NetworkSession(redirects_and_retries)
    http = BoundedHttpClient(user_agent="TreasuryTest/1", session=session,
                             sleep=lambda _: None)
    at = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    saved = state(progress("sb0560", "2026-01-01", hint=1))
    result = adapter(http, at=at, lookback_days=60).collect(saved)
    assert len(session.calls) == 9
    assert result.status == "partial"
    assert result.pagination["page_fetches"] == 2
    assert any("attempt_budget_exhausted" in error for error in result.errors)
    assert "treasury_press_releases" not in result.pagination
    # The second annual shard's next redirect target is refused before a tenth send.


def test_shard_validators_are_independent_by_year_and_frontier_persists():
    at = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    page0_rows = [row(f"sb{i:04d}", f"Current release {i}", "January 15, 2026")
                  for i in range(10)]
    older_rows = [row("sb0560", "Saved boundary", "December 31, 2025"),
                  row("sb0559", "Older release", "December 30, 2025")]
    payload_2026 = shard_payload([_search_item(item) for item in page0_rows])
    payload_2025 = shard_payload([_search_item(item) for item in older_rows])
    old_state = state(progress("sb0560", "2025-12-31", hint=1), requests={
        "page_0": {"etag": '"page-old"'},
        "search_2026": {"etag": '"2026-old"', "last_modified": "Tue, 01 Jan 2026 00:00:00 GMT"},
        "search_2025": {"etag": '"2025-old"', "last_modified": "Wed, 31 Dec 2025 00:00:00 GMT"},
    })
    http = FakeHttp([
        response(listing(*page0_rows), headers={"content-type": "text/html", "etag": '"page-new"'}),
        json_response(payload_2026, 2026, {"content-type": "application/json", "etag": '"2026-new"'}),
        json_response(payload_2025, 2025, {"content-type": "application/json", "etag": '"2025-new"'}),
    ])
    result = adapter(http, at=at, lookback_days=180).collect(old_state)
    assert http.calls[1][1]["etag"] == '"2026-old"'
    assert http.calls[1][1]["last_modified"].startswith("Tue,")
    assert http.calls[2][1]["etag"] == '"2025-old"'
    assert http.calls[2][1]["last_modified"].startswith("Wed,")
    assert result.state_updates["search_2026"]["etag"] == '"2026-new"'
    assert result.state_updates["search_2025"]["etag"] == '"2025-new"'
    assert result.pagination["treasury_press_releases"]["boundary_id"] == "sb0559"
    JsonDiscoveryState.record_success(old_state, TREASURY_SOURCE_ID, at,
                                      result.state_updates, pagination=result.pagination)
    assert JsonDiscoveryState.request_validators(old_state, TREASURY_SOURCE_ID,
                                                  "search_2025")["etag"] == '"2025-new"'
    assert JsonDiscoveryState.request_validators(old_state, TREASURY_SOURCE_ID,
                                                  "search_2026")["etag"] == '"2026-new"'


def test_shard_304_is_non_progress_and_clears_validator_for_unconditional_retry():
    page0 = listing(row("sb0632", "New release", "September 22, 2026"))
    saved = state(requests={"page_0": {"etag": '"page-old"'},
                            "search_2026": {"etag": '"shard-old"'}})
    not_modified = response(status=304, headers={"etag": '"shard-old"'})
    result = adapter(FakeHttp([
        response(page0, headers={"content-type": "text/html", "etag": '"page-new"'}),
        not_modified,
    ])).collect(saved)
    assert result.status == "partial"
    assert "treasury_press_releases" not in result.pagination
    assert "page_0" not in result.state_updates
    assert result.state_updates["search_2026"] == {}

    JsonDiscoveryState.record_success(saved, TREASURY_SOURCE_ID, STAMP,
                                      result.state_updates)
    retry_http = FakeHttp([response(page0), shard_for_pages(
        [row("sb0632", "New release", "September 22, 2026")],
        page1=[row("sb0560", "Saved boundary", "September 1, 2026")])])
    adapter(retry_http).collect(saved)
    assert retry_http.calls[1][1]["etag"] == ""


def test_partial_page_keeps_valid_candidates_but_not_its_validator():
    body = listing(row("sb0632", "Good release", "September 22, 2026"),
                   raw_row("/news/press-releases/sb0631", "Malformed row", "yesterday"))
    result = adapter(FakeHttp([response(body)])).collect()
    assert result.status == "partial"
    assert set(candidate_map(result)) == {"sb0632"}
    assert "page_0" not in result.state_updates
    assert "treasury_press_releases" not in result.pagination


def test_expired_boundary_is_preserved_and_never_rebased():
    result = adapter(FakeHttp([response(FIXTURE.read_bytes())])).collect(
        state(progress("sb0001", "2026-06-01", hint=4)))
    assert result.pagination["treasury_press_releases"] == progress(
        "sb0001", "2026-06-01", hint=4, status="boundary_expired")
    assert len(result.candidates) > 0


@pytest.mark.parametrize("bad", [
    [],
    {"boundary_id": "BAD ID", "boundary_date": "2026-09-01", "deep_page_hint": 1,
     "frontier_status": "active"},
    {"boundary_id": "sb0560", "boundary_date": "not-a-date", "deep_page_hint": 1,
     "frontier_status": "active"},
    {"boundary_id": "sb0560", "boundary_date": "2026-09-01", "deep_page_hint": 0,
     "frontier_status": "active"},
])
def test_malformed_saved_pagination_fails_closed_without_request(bad):
    http = FakeHttp([])
    result = adapter(http).collect(state(bad))
    assert result.status == "failed"
    assert "refusing to reset" in result.errors[0]
    assert http.calls == []


def test_validators_persist_and_are_sent_per_page():
    headers = {"content-type": "text/html", "etag": '"page0"',
               "last-modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    first = adapter(FakeHttp([response(listing(row("sb0632", "New", "September 22, 2026")),
                                       headers=headers)]))
    result = first.collect()
    assert result.state_updates["page_0"] == {
        "etag": '"page0"', "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}
    saved = state(result.pagination["treasury_press_releases"])
    saved["sources"][TREASURY_SOURCE_ID]["requests"] = {
        "page_0": {"etag": '"page0"', "last_modified": "Tue, 22 Sep 2026 12:00:00 GMT"}}
    http = FakeHttp([response(status=304, headers={"etag": '"page0"'})])
    unchanged = adapter(http).collect(saved)
    assert http.calls[0][1]["etag"] == '"page0"'
    assert unchanged.status == "not_modified"


def test_304_preserves_existing_frontier():
    result = adapter(FakeHttp([response(status=304, headers={})])).collect(state())
    assert result.status == "not_modified"
    assert result.pagination["treasury_press_releases"] == progress()


def test_overlapping_pages_deduplicate_identity_and_keep_provenance():
    duplicate = row("sb0560", "Saved boundary", "September 1, 2026")
    page0 = [row("sb0632", "New", "September 22, 2026"), duplicate]
    page1 = [duplicate, row("sb0559", "Older", "August 31, 2026")]
    http = FakeHttp([response(listing(*page0)), shard_for_pages(page0, page1=page1)])
    result = adapter(http).collect(state())
    matches = [item for item in result.candidates if item.source_native_id == "sb0560"]
    assert len(matches) == 1
    assert len(matches[0].provenance) == 3
    assert matches[0].candidate_id == "treasury-press-releases:sb0560"


def test_three_logical_page_fetch_bound_and_article_pages_never_fetched():
    page0 = [row("sb0632", "New", "September 22, 2026"),
             row("sb0560", "Boundary", "September 1, 2026")]
    http = FakeHttp([response(listing(*page0)), shard_for_pages(
        page0, page1=[row("sb0559", "Older one", "August 31, 2026")],
        page2=[row("sb0558", "Older two", "August 30, 2026")])])
    result = adapter(http).collect(state())
    assert result.pagination["page_fetches"] == 3
    assert len(http.calls) <= 3
    assert urlsplit(http.calls[0][0]).path == "/news/press-releases"
    assert all("/news/press-releases/sb" not in call[0] for call in http.calls)


def test_candidate_identity_is_deterministic_and_ofac_material_stays_treasury():
    body = listing(row("sb0632", "Treasury OFAC designates digital asset exchange", "September 17, 2026"))
    ids = []
    for _ in range(2):
        item = adapter(FakeHttp([response(body)])).collect().candidates[0]
        ids.append((item.candidate_id, item.source_native_id, item.source_id, item.url))
    assert ids[0] == ids[1]
    assert ids[0] == ("treasury-press-releases:sb0632", "sb0632", TREASURY_SOURCE_ID,
                      "https://home.treasury.gov/news/press-releases/sb0632")


def test_treasury_authority_alone_does_not_make_generic_release_relevant():
    item = NewsItem(title="Treasury announces an office renovation", url=TREASURY_URL,
                    source="Treasury", source_id=TREASURY_SOURCE_ID, authority_tier=1,
                    category="government", summary="U.S. Treasury Press Release - Press Release")
    detect_entities(item)
    classify_source_quality(item)
    score_relevance(item)
    assert "Treasury" in item.detected_entities
    assert item.relevance_score == 0


@pytest.mark.parametrize("title", [
    "Treasury proposes GENIUS Act payment stablecoin rulemaking",
    "Treasury targets a digital asset exchange for sanctions evasion",
    "Treasury announces blockchain-based payment innovation",
])
def test_existing_relevance_pipeline_recognizes_digital_asset_treasury_content(title):
    item = NewsItem(title=title, url="https://home.treasury.gov/news/press-releases/sb0605",
                    source="U.S. Department of the Treasury", source_id=TREASURY_SOURCE_ID,
                    authority_tier=1, category="government",
                    summary="U.S. Treasury Press Release - Press Release")
    detect_entities(item)
    classify_source_quality(item)
    score_relevance(item)
    assert "Treasury" in item.detected_entities
    assert item.relevance_score > 0


def test_disabled_source_makes_no_requests():
    http = FakeHttp([])
    result = adapter(http, enabled=False).collect()
    assert result.status == "not_configured"
    assert http.calls == []


def test_pagination_only_state_persists_through_main_pipeline(tmp_path, monkeypatch):
    import main
    from discovery.base import DiscoveryResult
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    saved_progress = progress("sb0559", "2026-08-31", hint=2)
    result = DiscoveryResult(TREASURY_SOURCE_ID, "treasury_press_releases", "success",
                             fetched_at=STAMP,
                             pagination={"page_fetches": 1,
                                         "treasury_press_releases": saved_progress})
    monkeypatch.setattr(main, "collect_source", lambda configured, state: result)
    store = JsonDiscoveryState(tmp_path / "discovery.json")
    main.run_pipeline(sources=[], state=JsonState(str(tmp_path / "seen.json")),
                      discovery_sources=[source()], discovery_state=store)
    persisted = store.load()["sources"][TREASURY_SOURCE_ID]["pagination"]
    assert persisted["treasury_press_releases"] == saved_progress
