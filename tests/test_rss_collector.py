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
