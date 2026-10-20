import json

from discovery.http import HttpResponse
from scripts.probe_citi_global_news_index import (
    NEWS_URL,
    _article_detail_h1_candidate,
    _embedded_official_article_records,
    _embedded_official_article_urls,
    _embedded_record_shape_diagnostics,
    _jsonld_article_metadata,
    _jsonld_shape_diagnostics,
    _next_data_candidates,
    _next_data_request_urls,
    probe_citi_global_news_index,
)


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def response(body, *, status=200, content_type="text/html", url=NEWS_URL):
    return HttpResponse(status, {"content-type": content_type}, body, url)


def test_citi_broader_news_index_parses_dated_press_release_without_activation():
    html = b"""<html><body><main><article>
      <time datetime="2026-09-28T12:00:00Z">September 28, 2026</time>
      <a href="/global/news/press-release/2026/citi-and-coinbase-expand-collaboration">
        Citi and Coinbase Expand Collaboration to Connect Digital and Fiat Payments
      </a>
    </article></main></body></html>"""
    http = FakeHttp(response(html))

    result = probe_citi_global_news_index(http)

    assert "status=valid_index" in result
    assert "parsed_items=1" in result
    assert "2026-09-28T12:00:00+00:00" in result
    assert "Citi and Coinbase Expand Collaboration" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == NEWS_URL
    assert http.calls[0][1]["expected_content_types"] == ("text/html", "application/xhtml+xml")


def test_citi_broader_news_index_reports_403_without_raising():
    assert probe_citi_global_news_index(
        FakeHttp(response(b"Forbidden", status=403, content_type="text/plain"))
    ) == (
        "Citi broader news-index diagnostic: status=unavailable "
        "reason=unexpected_http_status http_status=403"
    )


def test_citi_broader_news_index_rejects_non_official_redirect_and_wrong_path():
    assert probe_citi_global_news_index(
        FakeHttp(response(b"<html></html>", url="https://attacker.example/global/news"))
    ) == "Citi broader news-index diagnostic: status=rejected reason=non_official_final_url"

    assert probe_citi_global_news_index(
        FakeHttp(response(b"<html></html>", url="https://www.citigroup.com/global/news/press-release"))
    ) == "Citi broader news-index diagnostic: status=rejected reason=unexpected_final_path"


def test_citi_broader_news_index_rejects_wrong_content_type():
    assert probe_citi_global_news_index(
        FakeHttp(response(b'{"not":"html"}', content_type="application/json"))
    ) == (
        "Citi broader news-index diagnostic: status=rejected reason=unexpected_content_type "
        "content_type=application/json"
    )



def test_next_data_probe_extracts_only_records_with_same_object_title_url_and_date():
    source = {
        "source_url": NEWS_URL,
        "allowed_hosts": ["www.citigroup.com", "citigroup.com"],
        "article_path_regex": r"/global/news/press-release/\d{4}/[^/?#]+",
        "numeric_date_order": "mdy",
    }
    payload = {
        "props": {
            "pageProps": {
                "items": [
                    {
                        "title": "Citi and Coinbase Expand Collaboration",
                        "url": "/global/news",
                        "link": "/global/news/press-release/2026/citi-and-coinbase-expand-collaboration",
                        "publishDate": "2026-09-28T12:00:00Z",
                    },
                    {
                        "title": "Undated official release should not borrow another date",
                        "link": "/global/news/press-release/2026/undated-release",
                    },
                    {"publishDate": "2026-10-01T12:00:00Z"},
                    {
                        "title": "External page must be rejected",
                        "url": "https://attacker.example/global/news/press-release/2026/attack",
                        "publishDate": "2026-10-01T12:00:00Z",
                    },
                    {
                        "title": "Ambiguous official routes must be rejected",
                        "url": "/global/news/press-release/2026/route-one",
                        "link": "/global/news/press-release/2026/route-two",
                        "publishDate": "2026-10-01T12:00:00Z",
                    },
                    {
                        "title": "Malformed URL must be rejected without crashing",
                        "url": "https://[invalid",
                        "publishDate": "2026-10-01T12:00:00Z",
                    },
                ]
            }
        }
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    rows = _next_data_candidates(source, html)

    assert len(rows) == 1
    assert rows[0]["title"] == "Citi and Coinbase Expand Collaboration"
    assert rows[0]["date"].isoformat() == "2026-09-28T12:00:00+00:00"
    assert rows[0]["url"] == (
        "https://www.citigroup.com/global/news/press-release/2026/"
        "citi-and-coinbase-expand-collaboration/"
    )


def test_next_data_probe_ignores_malformed_json_and_oversized_payload():
    source = {
        "source_url": NEWS_URL,
        "allowed_hosts": ["www.citigroup.com", "citigroup.com"],
        "article_path_regex": r"/global/news/press-release/\d{4}/[^/?#]+",
    }
    malformed = b'<script id="__NEXT_DATA__" type="application/json">{broken</script>'
    oversized = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + (" " * 300001)
        + "</script>"
    ).encode()

    assert _next_data_candidates(source, malformed) == []
    assert _next_data_candidates(source, oversized) == []



def test_next_data_probe_accepts_full_official_route_in_slug_field():
    source = {
        "source_url": NEWS_URL,
        "allowed_hosts": ["www.citigroup.com", "citigroup.com"],
        "article_path_regex": r"/global/news/press-release/\d{4}/[^/?#]+",
        "numeric_date_order": "mdy",
    }
    payload = {
        "items": [
            {
                "title": "Citi Token Services expands digital asset infrastructure",
                "slug": "/global/news/press-release/2026/citi-token-services-expansion",
                "publishDate": "2026-09-28",
            }
        ]
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    rows = _next_data_candidates(source, html)

    assert len(rows) == 1
    assert rows[0]["title"] == "Citi Token Services expands digital asset infrastructure"
    assert rows[0]["date"].isoformat() == "2026-09-28T00:00:00+00:00"


def test_citi_embedded_record_shape_diagnostics_report_structure_not_values():
    payload = {
        "releases": [
            {
                "url": "/global/news/press-release/2026/citi-example-release",
                "title": "Sensitive headline should not be logged",
                "publishDate": "2026-10-18",
            },
            {
                "slug": "/global/news/press-release/2026/citi-second-release",
                "title": "Another headline should not be logged",
            },
        ]
    }
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></html>"
    ).encode()

    result = _embedded_record_shape_diagnostics({}, html)

    assert "route_objects=2" in result
    assert "route_with_title=2" in result
    assert "route_with_date=1" in result
    assert "route_with_title_and_date=1" in result
    assert "publishdate" in result
    assert "Sensitive headline should not be logged" not in result
    assert "Another headline should not be logged" not in result
    assert len(result) < 3000


def test_citi_embedded_record_shape_diagnostics_bounds_oversized_scripts():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"releases": [{"url": "/global/news/press-release/2026/example", "title": "x"}], "large": "x" * 1_600_000})
        + "</script>"
    ).encode()

    result = _embedded_record_shape_diagnostics({}, html)

    assert "route_objects=0" in result
    assert "route_with_title_and_date=0" in result


def test_citi_record_shape_diagnostics_report_nested_keys_and_types_only():
    payload = {
        "items": [{
            "url": "/global/news/press-release/2026/example",
            "publishDate": "2026-10-19",
            "info": {"headline": "Do not log this headline", "description": "Do not log this body"},
            "subtitle": "Do not log this subtitle",
        }]
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    result = _embedded_record_shape_diagnostics({}, html)

    assert "info_nested_shapes': [{'depth': 0, 'keys': ['description', 'headline']}]" in result
    assert "'info': 'dict'" in result
    assert "Do not log this headline" not in result
    assert "Do not log this body" not in result
    assert "Do not log this subtitle" not in result


def test_citi_embedded_official_routes_are_bounded_and_allowlisted():
    payload = {
        "items": [
            {"url": "/global/news/press-release/2026/first-release"},
            {"url": "https://attacker.example/global/news/press-release/2026/unsafe"},
            {"url": "/global/news/press-release/2026/second-release"},
            {"url": "/global/news/press-release/2026/third-release"},
        ]
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()
    from discovery.dispatch import load_discovery_sources
    from pathlib import Path

    from scripts.probe_citi_global_news_index import ROOT

    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    source = {**source, "source_url": NEWS_URL}

    result = _embedded_official_article_urls(source, html, limit=2)

    assert result == [
        "https://www.citigroup.com/global/news/press-release/2026/first-release/",
        "https://www.citigroup.com/global/news/press-release/2026/second-release/",
    ]


def test_citi_jsonld_article_metadata_requires_explicit_date_and_matching_official_url():
    from discovery.dispatch import load_discovery_sources
    from scripts.probe_citi_global_news_index import ROOT

    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    source = {**source, "source_url": NEWS_URL}
    article_url = "https://www.citigroup.com/global/news/press-release/2026/citi-example-release/"

    payload = {
        "@type": "NewsArticle",
        "headline": "Citi Announces Example Release",
        "datePublished": "2026-10-18T09:30:00-04:00",
        "url": article_url,
    }
    html = (
        '<script type="application/ld+json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    rows = _jsonld_article_metadata(source, article_url, html)

    assert len(rows) == 1
    assert rows[0]["title"] == "Citi Announces Example Release"
    assert rows[0]["date"].isoformat() == "2026-10-18T13:30:00+00:00"

    wrong_url = {
        **payload,
        "url": "https://www.citigroup.com/global/news/press-release/2026/a-different-release/",
    }
    wrong_html = (
        '<script type="application/ld+json">'
        + json.dumps(wrong_url)
        + "</script>"
    ).encode()
    assert _jsonld_article_metadata(source, article_url, wrong_html) == []

    missing_date = {key: value for key, value in payload.items() if key != "datePublished"}
    missing_html = (
        '<script type="application/ld+json">'
        + json.dumps(missing_date)
        + "</script>"
    ).encode()
    assert _jsonld_article_metadata(source, article_url, missing_html) == []


def test_citi_jsonld_shape_diagnostic_reports_schema_and_types_without_values():
    payload = {
        "@type": "NewsArticle",
        "headline": "Private headline value must not be logged",
        "datePublished": "2026-10-18",
        "url": "https://www.citigroup.com/global/news/press-release/2026/example/",
    }
    html = (
        '<script type="application/ld+json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    result = _jsonld_shape_diagnostics(html)

    assert "NewsArticle" in result
    assert "'headline': 'str'" in result
    assert "Private headline value must not be logged" not in result
    assert "2026-10-18" not in result


def test_citi_index_date_and_detail_h1_must_be_present_and_validated():
    from discovery.dispatch import load_discovery_sources
    from scripts.probe_citi_global_news_index import ROOT

    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    source = {**source, "source_url": NEWS_URL}
    index_payload = {
        "items": [
            {
                "url": "/global/news/press-release/2026/citi-example-release",
                "publishDate": "2026-10-18",
            },
            {
                "url": "/global/news/press-release/2026/no-date",
            },
        ]
    }
    index_html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(index_payload)
        + "</script>"
    ).encode()

    records = _embedded_official_article_records(source, index_html, limit=2)

    assert len(records) == 1
    assert records[0]["date"].isoformat() == "2026-10-18T00:00:00+00:00"
    page = b"<html><h1>Citi Announces Example Release</h1></html>"
    candidate = _article_detail_h1_candidate(records[0], page)
    assert candidate is not None
    assert candidate["title"] == "Citi Announces Example Release"

    assert _article_detail_h1_candidate(records[0], b"<h1>Read More</h1>") is None
    assert _article_detail_h1_candidate({**records[0], "date": None}, page) is None
    assert _embedded_official_article_records(
        source,
        b'<script id="__NEXT_DATA__" type="application/json">{"items":[{"url":"https://attacker.example/global/news/press-release/2026/nope","publishDate":"2026-10-18"}]}</script>',
    ) == []


def test_citi_collector_probe_uses_temporary_enable_and_never_persists_state():
    from datetime import datetime, timezone
    from discovery.http import HttpResponse
    from scripts.probe_citi_discovery_collector import HOME_URL, probe_citi_discovery_collector

    index = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({
            "items": [{
                "url": "/global/news/press-release/2026/citi-example-release",
                "publishDate": "2026-10-18",
            }]
        })
        + "</script></html>"
    ).encode()
    detail = b"<html><h1>Citi Announces Example Release</h1><p>October 18, 2026</p></html>"

    class RouteAwareHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            if url in {NEWS_URL, HOME_URL}:
                return HttpResponse(200, {"content-type": "text/html"}, index, url)
            return HttpResponse(200, {"content-type": "text/html"}, detail, url)

    http = RouteAwareHttp()
    result = probe_citi_discovery_collector(http)

    assert "index=global_news: status=partial candidates=1" in result
    assert "index=home: status=partial candidates=1" in result
    assert result.count("state_updates=0 observations=0") == 2
    assert result.count("published_at=2026-10-18T00:00:00+00:00") == 2
    assert len(http.calls) == 4


def test_citi_next_data_urls_are_derived_from_bounded_official_bootstrap():
    payload = {
        "buildId": "build-2026_10",
        "assetPrefix": "/global/citigroup-ui/akpublic",
        "page": "/[...params]",
    }
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    ).encode()

    urls = _next_data_request_urls(html, NEWS_URL)

    assert urls == [
        "https://www.citigroup.com/global/citigroup-ui/akpublic/_next/data/build-2026_10/global/news.json",
        "https://www.citigroup.com/_next/data/build-2026_10/global/news.json",
    ]
    assert _next_data_request_urls(html, "https://attacker.example/global/news") == []


def test_citi_config_uses_broader_official_index_but_stays_disabled():
    from pathlib import Path
    from discovery.dispatch import load_discovery_sources

    root = Path(__file__).resolve().parents[1]
    sources = load_discovery_sources(root / "config" / "discovery_sources.json")
    citi = next(source for source in sources if source["source_id"] == "citi-press-releases")

    assert citi["source_url"] == NEWS_URL
    assert citi["enabled"] is False
    assert citi["allowed_hosts"] == ["www.citigroup.com", "citigroup.com"]
