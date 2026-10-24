from datetime import datetime, timezone

import pytest

from discovery.dispatch import load_discovery_sources
from discovery.http import HttpResponse
from discovery.institutional_press import (
    InstitutionalPressDiscovery,
    _parse_sitemap_entries,
    _safe_sitemap_url,
    validate_institutional_source,
)

STAMP = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
SITEMAP_URL = "https://www.bot.or.th/th.sitemap.xml"
STABLECOIN_URL = (
    "https://www.bot.or.th/th/laws-and-rules/public-hearing/"
    "public-hearing-20261020-Stablecoin.html"
)
OLD_URL = (
    "https://www.bot.or.th/th/laws-and-rules/public-hearing/"
    "public-hearing-20240829--theapprovalsfis.html"
)


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == SITEMAP_URL:
            return HttpResponse(
                200,
                {"content-type": "application/xml"},
                (
                    "<urlset>"
                    f"<url><loc>{STABLECOIN_URL}</loc></url>"
                    f"<url><loc>{OLD_URL}</loc></url>"
                    "<url><loc>https://evil.example/th/laws-and-rules/public-hearing/public-hearing-20261021.html</loc></url>"
                    "<url><loc>https://www.bot.or.th/en/news-and-media/news/news-20261020.html</loc></url>"
                    "</urlset>"
                ).encode(),
                SITEMAP_URL,
            )
        if url == STABLECOIN_URL:
            body = b"""<html><head>
              <meta property="article:published_time" content="2026-10-20T00:00:00+07:00">
            </head><body><h1>Public Hearing on THB Stablecoin Business Regulation</h1></body></html>"""
        elif url == OLD_URL:
            body = b"""<html><head>
              <meta property="article:published_time" content="2024-08-29T00:00:00+07:00">
            </head><body><h1>Older Public Hearing</h1></body></html>"""
        else:
            raise AssertionError(f"unexpected URL: {url}")
        return HttpResponse(200, {"content-type": "text/html"}, body, url)


def bot_source():
    return next(
        source for source in load_discovery_sources()
        if source["source_id"] == "bot-thai-public-hearings"
    )


def test_bot_thai_sitemap_source_is_registered_disabled_and_path_bounded():
    source = bot_source()
    assert source["enabled"] is False
    assert source["sitemap_url"] == SITEMAP_URL
    assert source["sitemap_path_prefixes"] == ["/th/"]
    assert source["sitemap_detail_limit"] == 5
    assert validate_institutional_source(source) == source
    assert _safe_sitemap_url(source, SITEMAP_URL) == SITEMAP_URL
    assert _safe_sitemap_url(source, STABLECOIN_URL) == STABLECOIN_URL
    assert _safe_sitemap_url(
        source, "https://www.bot.or.th/en/news-and-media/news/news-20261020.html"
    ) is None
    assert _safe_sitemap_url(
        source, "https://evil.example/th/laws-and-rules/public-hearing/public-hearing-20261020.html"
    ) is None


def test_bot_sitemap_parser_filters_off_host_and_non_thai_paths():
    source = bot_source()
    xml = (
        "<urlset>"
        f"<url><loc>{STABLECOIN_URL}</loc></url>"
        "<url><loc>https://evil.example/th/laws-and-rules/public-hearing/public-hearing-20261020.html</loc></url>"
        "<url><loc>https://www.bot.or.th/en/news-and-media/news/news-20261020.html</loc></url>"
        "</urlset>"
    ).encode()
    kind, entries = _parse_sitemap_entries(source, xml)
    assert kind == "urlset"
    assert entries == [(STABLECOIN_URL, "")]


def test_bot_sitemap_collector_finds_dated_stablecoin_hearing_without_using_sitemap_dates():
    source = {**bot_source(), "enabled": True}
    http = FakeHttp()
    result = InstitutionalPressDiscovery(
        source, http=http, now=lambda: STAMP
    ).collect(source_state={})

    assert result.status == "success"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.url == STABLECOIN_URL
    assert candidate.title == "Public Hearing on THB Stablecoin Business Regulation"
    assert candidate.published_at == datetime(2026, 10, 19, 17, tzinfo=timezone.utc)
    assert SITEMAP_URL in http.calls
    assert STABLECOIN_URL in http.calls
    assert OLD_URL in http.calls
    assert all("evil.example" not in url for url in http.calls)
    assert not any("/en/" in url for url in http.calls)


def test_sitemap_path_prefix_configuration_fails_closed():
    source = bot_source()
    with pytest.raises(ValueError, match="sitemap_path_prefixes"):
        validate_institutional_source({**source, "sitemap_path_prefixes": ["/th/../global/"]})
    with pytest.raises(ValueError, match="sitemap_path_prefixes"):
        validate_institutional_source({**source, "sitemap_path_prefixes": ["//evil/"]})
    invalid = {**source}
    invalid.pop("sitemap_url")
    with pytest.raises(ValueError, match="sitemap_path_prefixes"):
        validate_institutional_source(invalid)
