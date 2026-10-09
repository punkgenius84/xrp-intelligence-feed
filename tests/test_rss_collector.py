import feedparser
import pytest
import requests

from collectors.rss import FeedCollectionError, RSSCollector


SOURCE = {"source_id": "test", "name": "Test source", "url": "https://example.test/feed",
          "authority_tier": 1, "category": "regulatory", "entity_coverage": ["XRP"],
          "collection_type": "rss"}


def test_timeout_is_bounded_and_reported(monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == (5.0, 20.0)
        raise requests.Timeout("slow")
    monkeypatch.setattr("collectors.rss.requests.get", timeout)
    collector = RSSCollector(SOURCE)
    with pytest.raises(FeedCollectionError) as error:
        collector.collect()
    assert error.value.status == collector.last_report.status == "timeout"


@pytest.mark.parametrize(("exception", "status"), [
    (requests.ConnectionError("offline"), "request_error"),
    (requests.RequestException("failure"), "request_error"),
])
def test_request_errors_are_reported(monkeypatch, exception, status):
    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: (_ for _ in ()).throw(exception))
    collector = RSSCollector(SOURCE)
    with pytest.raises(FeedCollectionError) as error:
        collector.collect()
    assert error.value.status == collector.last_report.status == status


def test_http_failure_is_reported(monkeypatch):
    class Response:
        status_code = 503
        headers = {}
        url = SOURCE["url"]
        content = b""
        def raise_for_status(self):
            raise requests.HTTPError("503")
    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: Response())
    collector = RSSCollector(SOURCE)
    with pytest.raises(FeedCollectionError) as error:
        collector.collect()
    assert error.value.status == "http_error"
    assert error.value.http_status == 503


def test_malformed_feed_with_partial_entries_keeps_items(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        url = SOURCE["url"]
        content = b"body"
        def raise_for_status(self):
            pass
    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: Response())
    partial = feedparser.FeedParserDict(bozo=True, bozo_exception=ValueError("bad tail"),
        entries=[feedparser.FeedParserDict(title="Useful update", link="https://example.test/a")])
    monkeypatch.setattr("collectors.rss.feedparser.parse", lambda *a, **kw: partial)
    collector = RSSCollector(SOURCE)
    items = collector.collect()
    assert [item.title for item in items] == ["Useful update"]
    assert collector.last_report.status == "partial_malformed"
    assert collector.last_report.item_count == 1


def test_malformed_feed_without_useful_entries_raises_reported_error(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml"}
        url = SOURCE["url"]
        content = b"broken"
        def raise_for_status(self):
            pass
    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: Response())
    malformed = feedparser.FeedParserDict(bozo=True, bozo_exception=ValueError("invalid XML"), entries=[])
    monkeypatch.setattr("collectors.rss.feedparser.parse", lambda *a, **kw: malformed)
    collector = RSSCollector(SOURCE)
    with pytest.raises(FeedCollectionError) as error:
        collector.collect()
    assert error.value.status == collector.last_report.status == "malformed_feed"


def test_empty_feed_is_distinguished(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        url = SOURCE["url"]
        content = b"body"
        def raise_for_status(self):
            pass
    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: Response())
    monkeypatch.setattr("collectors.rss.feedparser.parse", lambda *a, **kw: feedparser.FeedParserDict(bozo=False, entries=[]))
    collector = RSSCollector(SOURCE)
    assert collector.collect() == []
    assert collector.last_report.status == "empty"


def test_unexpected_feedparser_exception_remains_a_reported_collection_error(monkeypatch):
    class Response:
        status_code = 200
        headers = {}
        url = SOURCE["url"]
        content = b"body"

        def raise_for_status(self):
            pass

    monkeypatch.setattr("collectors.rss.requests.get", lambda *a, **kw: Response())
    monkeypatch.setattr(
        "collectors.rss.feedparser.parse",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("parser blew up")),
    )

    collector = RSSCollector(SOURCE)

    with pytest.raises(FeedCollectionError, match="malformed RSS response: parser blew up") as error:
        collector.collect()

    assert error.value.status == "malformed_feed"
    assert collector.last_report.status == "malformed_feed"


def test_utf8_rss_with_stale_ascii_xml_and_http_charset_declarations_is_parsed(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml; charset=us-ascii"}
        url = SOURCE["url"]
        content = (
            '<?xml version="1.0" encoding="us-ascii"?>'
            '<rss version="2.0"><channel><title>Federal Reserve</title><item>'
            '<title>Federal Reserve Board requests comment on Café banking data</title>'
            '<link>https://www.federalreserve.gov/newsevents/pressreleases/bcreg20260924a.htm</link>'
            '<pubDate>Thu, 24 Sep 2026 16:00:00 +0000</pubDate>'
            '<guid>fed-utf8</guid></item></channel></rss>'
        ).encode("utf-8")

        def raise_for_status(self):
            pass

    monkeypatch.setattr("collectors.rss.requests.get", lambda *args, **kwargs: Response())
    collector = RSSCollector({
        **SOURCE,
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "name": "Federal Reserve Board",
    })

    items = collector.collect()

    assert len(items) == 1
    assert items[0].title.endswith("Café banking data")
    assert collector.last_report.status == "success"
    assert collector.last_report.error is None


def test_stale_ascii_declaration_with_invalid_utf8_stays_malformed(monkeypatch):
    class Response:
        status_code = 200
        headers = {"Content-Type": "application/rss+xml; charset=us-ascii"}
        url = SOURCE["url"]
        content = (
            b'<?xml version="1.0" encoding="us-ascii"?>'
            b'<rss version="2.0"><channel><title>Fed</title><item><title>Broken \xff'
        )

        def raise_for_status(self):
            pass

    monkeypatch.setattr("collectors.rss.requests.get", lambda *args, **kwargs: Response())
    collector = RSSCollector(SOURCE)

    with pytest.raises(FeedCollectionError) as error:
        collector.collect()

    assert error.value.status == "malformed_feed"
    assert collector.last_report.status == "malformed_feed"
