from datetime import datetime, timezone

from discovery.base import DiscoveryResult
from discovery.canada_gazette_rss import (
    FEED_URL,
    METHOD,
    SOURCE_ID,
    CanadaGazetteRSSDiscovery,
    validate_canada_gazette_source,
)
from discovery.dispatch import load_discovery_sources, validate_discovery_sources
from discovery.http import HttpResponse


STAMP = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
ISSUE = "https://gazette.gc.ca/rp-pr/p1/2026/2026-10-24/html/index-eng.html"
ARTICLE = "https://gazette.gc.ca/rp-pr/p1/2026/2026-10-24/html/reg1-eng.html"


def source(**changes):
    value = {
        "source_id": SOURCE_ID,
        "name": "Canada Gazette Part I",
        "authority_tier": 1,
        "category": "regulatory",
        "discovery_method": METHOD,
        "source_url": FEED_URL,
        "enabled": True,
        "lookback_days": 180,
        "max_items": 100,
    }
    value.update(changes)
    return value


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


def response(body, *, url, status=200, headers=None, content_type="application/rss+xml"):
    return HttpResponse(status, headers or {"content-type": content_type, "etag": '"v1"'}, body, url)


def rss(issue=ISSUE, date="Fri, 23 Oct 2026 18:00:00 GMT"):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Canada Gazette Part I</title>'
        '<item><title>Canada Gazette Part I, Volume 160, Number 43</title>'
        f'<link>{issue}</link><pubDate>{date}</pubDate>'
        '<description>Official edition</description></item>'
        '</channel></rss>'
    ).encode()


INDEX = b"""<html><body>
<h1>Canada Gazette, Part I, Volume 160, Number 43: Index</h1>
<p>October 24, 2026</p>
<h2>Proposed Regulations</h2>
<h3>Finance, Dept. of</h3>
<h4>Stablecoin Act and Retail Payment Activities Act</h4>
<a href="/rp-pr/p1/2026/2026-10-24/html/reg1-eng.html">Stablecoin Regulations</a>
<h2>Government notices</h2><h3>Department of Environment</h3>
<a href="/rp-pr/p1/2026/2026-10-24/html/reg2-eng.html">Unrelated environmental notice</a>
</body></html>"""


def run(http, config=None, state=None):
    return CanadaGazetteRSSDiscovery(
        config or source(), http=http, now=lambda: STAMP
    ).collect(state)


def test_canada_gazette_part_i_is_registered_as_official_source():
    rows = load_discovery_sources()
    configured = next(row for row in rows if row["source_id"] == SOURCE_ID)
    assert configured["source_url"] == FEED_URL
    assert configured["enabled"] is True
    assert validate_canada_gazette_source(configured) == configured
    assert validate_discovery_sources({"schema_version": 1, "sources": [configured]}) == [configured]


def test_issue_index_extracts_individual_proposed_regulation_with_context():
    http = FakeHttp([
        response(rss(), url=FEED_URL),
        response(INDEX, url=ISSUE, content_type="text/html"),
    ])
    result = run(http)

    assert result.status == "success"
    stablecoin = next(item for item in result.candidates if item.title == "Stablecoin Regulations")
    assert stablecoin.url == ARTICLE
    assert stablecoin.published_at == datetime(2026, 10, 24, tzinfo=timezone.utc)
    assert "Stablecoin Act and Retail Payment Activities Act" in stablecoin.summary
    assert "Finance, Dept. of" in stablecoin.summary
    assert stablecoin.provenance == [FEED_URL, ISSUE, ARTICLE]
    assert result.state_updates == {"rss": {"etag": '"v1"'}}
    assert len(http.calls) == 2


def test_unchanged_rss_is_not_modified_without_fetching_issues():
    http = FakeHttp([response(b"", url=FEED_URL, status=304, headers={"etag": '"v1"'})])
    state = {"sources": {SOURCE_ID: {"requests": {"rss": {"etag": '"v1"'}}}}
    result = run(http, state=state)
    assert result.status == "not_modified"
    assert result.candidates == []
    assert result.state_updates == {"rss": {"etag": '"v1"'}}
    assert len(http.calls) == 1


def test_rejects_non_official_issue_and_conflicting_issue_date():
    http = FakeHttp([response(rss(issue="https://evil.example/rp-pr/p1/2026/2026-10-24/html/index-eng.html"),
                              url=FEED_URL)])
    result = run(http)
    assert result.status == "failed"
    assert result.candidates == []
    assert result.state_updates == {}

    http = FakeHttp([response(rss(date="Mon, 12 Oct 2026 00:00:00 GMT"), url=FEED_URL)])
    result = run(http)
    assert result.status == "failed"
    assert result.candidates == []
