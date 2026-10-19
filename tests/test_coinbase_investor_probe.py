from datetime import datetime, timezone

from discovery.http import HttpResponse
from scripts.probe_coinbase_investor_news import PROBE_URL, probe_coinbase_investor_news


STAMP = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
INDEX_URL = PROBE_URL
ARTICLE_URL = "https://investor.coinbase.com/news/news-details/2026/example-release/default.aspx"


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return HttpResponse(200, {"content-type": "text/html"}, self.payload, url)


def test_coinbase_investor_probe_reads_only_index_and_reports_grounded_dates():
    http = FakeHttp(
        f'<html><body><article><time datetime="2026-10-17T12:00:00Z">October 17, 2026</time>'
        f'<a href="{ARTICLE_URL}">Coinbase Example Investor Release</a></article></body></html>'.encode()
    )

    output = probe_coinbase_investor_news(http=http, now=lambda: STAMP)

    assert "status=success candidates=1 detail_fallback=disabled" in output
    assert "date=2026-10-17T12:00:00+00:00" in output
    assert "Coinbase Example Investor Release" in output
    assert len(http.calls) == 1
    assert http.calls[0][0] == INDEX_URL


def test_coinbase_investor_probe_does_not_invent_a_date():
    http = FakeHttp(
        f'<html><body><article><h2>Undated Coinbase Investor Release</h2>'
        f'<a href="{ARTICLE_URL}">Undated Coinbase Investor Release</a></article></body></html>'.encode()
    )

    output = probe_coinbase_investor_news(http=http, now=lambda: STAMP)

    assert "candidates=0 detail_fallback=disabled" in output
    assert "Undated Coinbase Investor Release" not in output.splitlines()[1:]
    assert len(http.calls) == 1
    assert http.calls[0][0] == INDEX_URL
