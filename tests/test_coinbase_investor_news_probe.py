from datetime import datetime, timezone

from discovery.http import HttpResponse
from scripts.probe_coinbase_investor_news import PROBE_URL, probe_coinbase_investor_news


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_coinbase_ir_probe_uses_official_dated_default_route_without_detail_fetches():
    article_url = (
        "https://investor.coinbase.com/news/news-details/2026/"
        "Coinbase-Announces-Date-of-Third-Quarter-2026-Financial-Results/default.aspx"
    )
    html = (
        '<html><body><article><span>10/14/2026</span>'
        f'<a href="{article_url}">Coinbase Announces Date of Third Quarter 2026 Financial Results</a>'
        '</article></body></html>'
    ).encode()
    http = FakeHttp(HttpResponse(200, {"content-type": "text/html"}, html, PROBE_URL))

    result = probe_coinbase_investor_news(http, now=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc))

    assert f"index={PROBE_URL}" in result
    assert "status=success candidates=1" in result
    assert "2026-10-14T00:00:00+00:00" in result
    assert len(http.calls) == 1
    assert http.calls[0][0] == PROBE_URL


def test_coinbase_ir_probe_reports_forbidden_route_without_raising():
    http = FakeHttp(HttpResponse(403, {"content-type": "text/plain"}, b"Forbidden", PROBE_URL))

    result = probe_coinbase_investor_news(http)

    assert "status=failed candidates=0" in result
    assert "detail_fallback=disabled" in result
    assert len(http.calls) == 1


def test_coinbase_ir_probe_does_not_treat_generic_shell_as_dated_news():
    html = b'<html><body><a href="/about">About</a><a href="/news/">News</a></body></html>'
    http = FakeHttp(HttpResponse(200, {"content-type": "text/html"}, html, PROBE_URL))

    result = probe_coinbase_investor_news(http)

    assert "status=failed candidates=0" in result
    assert len(http.calls) == 1
