from datetime import datetime, timezone
from pathlib import Path

import pytest

from discovery.dispatch import DiscoveryRegistryError, collect_source, load_discovery_sources, validate_discovery_sources
from discovery.fdic_press_releases import FDIC_SOURCE_ID, FDIC_URL, FDICPressReleasesDiscovery, _article_identity, _parse_page
from discovery.http import HttpResponse

STAMP = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "fdic_press_releases.html"


def source(**changes):
    result = {
        "source_id": FDIC_SOURCE_ID, "name": "Federal Deposit Insurance Corporation",
        "authority_tier": 1, "category": "government", "discovery_method": "fdic_press_releases",
        "source_url": FDIC_URL, "enabled": True, "max_items": 50,
    }
    result.update(changes)
    return result


def listing(*items):
    return ("<html><body><main>" + "".join(
        f'<a href="{href}">{title}</a>' for href, title in items
    ) + "</main></body></html>").encode()


def response(content, status=200):
    return HttpResponse(status, {"content-type":"text/html", "etag":'"fdic"'}, content, FDIC_URL)


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
    return FDICPressReleasesDiscovery(source(**changes), http=http, now=lambda: STAMP)


def state(boundary="2026/fdic-publishes-july-enforcement-actions", hint=1):
    return {"sources": {FDIC_SOURCE_ID: {"pagination": {
        "fdic_press_releases": {"boundary_id": boundary, "deep_page_hint": hint, "frontier_status":"active"}
    }}}}


def test_registry_dispatch_and_enabled_source():
    configured = load_discovery_sources()
    fdic = next(item for item in configured if item["source_id"] == FDIC_SOURCE_ID)
    assert fdic == source()
    result = collect_source(fdic, http=FakeHttp([response(FIXTURE.read_bytes())]), now=lambda: STAMP)
    assert result.status == "success"
    assert result.candidates[0].published_at is None


@pytest.mark.parametrize("changes", [
    {"source_url":"https://evil.example/news/press-releases"},
    {"discovery_method":"generic_scraping"}, {"enabled":1},
    {"max_items":0}, {"max_items":51}, {"category":"regulatory"},
])
def test_registry_rejects_invalid_configuration(changes):
    with pytest.raises(DiscoveryRegistryError):
        validate_discovery_sources({"schema_version":1,"sources":[source(**changes)]})


def test_parse_listing_uses_official_route_and_native_year_slug_identity():
    rows, complete = _parse_page(FIXTURE.read_bytes())
    assert complete is True
    assert [row["native_id"] for row in rows] == [
        "2026/fdic-releases-results-summary-deposits-annual-survey",
        "2026/fdic-board-directors-approves-proposed-rule-state-bank-parity",
        "2026/fdic-publishes-july-enforcement-actions",
    ]


@pytest.mark.parametrize("value,expected", [
    ("https://www.fdic.gov/news/press-releases/2026/example", "2026/example"),
    ("/news/press-releases/2026/example/", "2026/example"),
    ("https://www.fdic.gov:443/news/press-releases/2026/example", "2026/example"),
])
def test_article_identity_accepts_only_canonical_official_routes(value, expected):
    assert _article_identity(value)[0] == expected


@pytest.mark.parametrize("value", [
    "http://www.fdic.gov/news/press-releases/2026/example",
    "https://evil.example/news/press-releases/2026/example",
    "//evil.example/news/press-releases/2026/example",
    "https://user@www.fdic.gov/news/press-releases/2026/example",
    "https://www.fdic.gov:444/news/press-releases/2026/example",
    "https://www.fdic.gov/news/press-releases/2026/../example",
    "https://www.fdic.gov/news/press-releases/example",
])
def test_article_identity_rejects_unsafe_or_non_press_release_routes(value):
    assert _article_identity(value) is None


def test_first_run_seeds_boundary_without_calendar_date():
    rows = listing(
        ("/news/press-releases/2026/newest", "Newest"),
        ("/news/press-releases/2026/older", "Older"),
    )
    result = adapter(FakeHttp([response(rows)])).collect({})
    progress = result.pagination["fdic_press_releases"]
    assert result.status == "success"
    assert result.candidates[0].source_native_id == "2026/newest"
    assert result.candidates[0].published_at is None
    assert progress == {"boundary_id":"2026/older", "deep_page_hint":1, "frontier_status":"active"}


def test_boundary_on_page_zero_emits_only_newer_prefix_and_advances_frontier():
    rows = listing(
        ("/news/press-releases/2026/newer", "Newer"),
        ("/news/press-releases/2026/fdic-publishes-july-enforcement-actions", "Boundary"),
        ("/news/press-releases/2026/older", "Older"),
    )
    result = adapter(FakeHttp([response(rows)])).collect(state())
    assert [item.source_native_id for item in result.candidates] == ["2026/newer"]
    assert result.pagination["fdic_press_releases"]["boundary_id"] == "2026/newer"


def test_missing_boundary_recovers_on_deep_page_and_emits_newer_pages():
    page0 = listing(("/news/press-releases/2026/new1", "New 1"))
    page1 = listing(("/news/press-releases/2026/new2", "New 2"))
    page2 = listing(("/news/press-releases/2026/fdic-publishes-july-enforcement-actions", "Boundary"))
    http = FakeHttp([response(page0), response(page1), response(page2)])
    result = adapter(http).collect(state(hint=1))
    assert [item.source_native_id for item in result.candidates] == ["2026/new1", "2026/new2"]
    assert [call[0] for call in http.calls] == [FDIC_URL, FDIC_URL+"?page=1", FDIC_URL+"?page=2"]
    assert result.pagination["fdic_press_releases"]["boundary_id"] == "2026/new2"


def test_missing_boundary_does_not_claim_page_zero_rows_as_new():
    http = FakeHttp([response(listing(("/news/press-releases/2026/new", "New"))),
                     response(listing(("/news/press-releases/2026/other", "Other"))),
                     response(b"<html><body>No results found.</body></html>")])
    result = adapter(http).collect(state(hint=1))
    assert result.candidates == []
    assert result.status == "empty"
    assert result.pagination["fdic_press_releases"]["boundary_id"] == state()["sources"][FDIC_SOURCE_ID]["pagination"]["fdic_press_releases"]["boundary_id"]


def test_empty_deep_page_is_not_boundary_expiration():
    http = FakeHttp([response(listing(("/news/press-releases/2026/new", "New"))),
                     response(b"<html><body>No results found.</body></html>")])
    result = adapter(http).collect(state(hint=1))
    assert result.pagination["fdic_press_releases"]["frontier_status"] == "active"
    assert result.pagination["fdic_press_releases"]["boundary_id"] == "2026/fdic-publishes-july-enforcement-actions"


def test_http_attempt_budget_is_shared_across_page_requests():
    from discovery.http import DiscoveryHttpError
    http = FakeHttp([DiscoveryHttpError("budget", kind="attempt_budget_exhausted")])
    result = adapter(http).collect({})
    assert result.status == "failed"
