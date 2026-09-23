from collections import deque

import pytest
import requests

from discovery.http import BoundedHttpClient, DiscoveryHttpError


class Response:
    def __init__(self, status=200, headers=None, chunks=(b"{}",)):
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


class Session:
    def __init__(self, results):
        self.results = deque(results)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def client(results, *, sleeps=None, **kwargs):
    session = Session(results)
    recorded_sleeps = [] if sleeps is None else sleeps
    http = BoundedHttpClient(user_agent="TestAgent/0.3", session=session,
                             sleep=recorded_sleeps.append, **kwargs)
    return http, session, recorded_sleeps


def test_200_applies_timeout_user_agent_validators_and_content_type():
    response = Response(headers={"Content-Type": "application/json", "ETag": '"one"',
                                 "Last-Modified": "Wed, 23 Sep 2026 12:00:00 GMT"})
    http, session, _ = client([response])
    result = http.get("https://example.test/data", etag='"old"',
                      last_modified="Tue, 22 Sep 2026 12:00:00 GMT",
                      expected_content_types=("application/json",))
    _, kwargs = session.calls[0]
    assert kwargs["timeout"] == (5.0, 20.0)
    assert kwargs["headers"]["User-Agent"] == "TestAgent/0.3"
    assert kwargs["headers"]["If-None-Match"] == '"old"'
    assert kwargs["headers"]["If-Modified-Since"].startswith("Tue,")
    assert result.content == b"{}"
    assert result.headers["etag"] == '"one"'
    assert response.closed


def test_304_returns_empty_not_modified_response():
    response = Response(status=304, chunks=())
    http, _, _ = client([response])
    result = http.get("https://example.test/data")
    assert result.status_code == 304
    assert result.content == b""
    assert response.closed


def test_404_fails_without_retry():
    http, session, _ = client([Response(status=404)])
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/data")
    assert error.value.kind == "http_error"
    assert error.value.status_code == 404
    assert len(session.calls) == 1


def test_429_honors_bounded_retry_after_then_succeeds():
    http, session, sleeps = client([Response(status=429, headers={"Retry-After": "60"}), Response()],
                                  max_attempts=2, max_retry_after=4)
    assert http.get("https://example.test/data").status_code == 200
    assert sleeps == [4]
    assert len(session.calls) == 2


def test_5xx_retries_with_bounded_attempts():
    http, session, sleeps = client([Response(status=503), Response()], max_attempts=2)
    assert http.get("https://example.test/data").status_code == 200
    assert len(session.calls) == 2
    assert sleeps == [0.25]


def test_timeout_retries_then_reports_clear_error():
    http, session, sleeps = client([requests.Timeout("slow"), requests.Timeout("slow")],
                                  max_attempts=2)
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/data")
    assert error.value.kind == "timeout"
    assert len(session.calls) == 2
    assert sleeps == [0.25]


def test_oversized_response_is_rejected_from_header_and_stream():
    header_http, _, _ = client([Response(headers={"Content-Type": "application/json",
                                                 "Content-Length": "12"})], max_bytes=10)
    with pytest.raises(DiscoveryHttpError, match="size limit"):
        header_http.get("https://example.test/data")
    stream_http, _, _ = client([Response(chunks=(b"123456", b"78901"))], max_bytes=10)
    with pytest.raises(DiscoveryHttpError, match="size limit"):
        stream_http.get("https://example.test/data")


def test_invalid_content_type_and_non_https_url_are_rejected():
    http, _, _ = client([Response(headers={"Content-Type": "text/html"})])
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/data", expected_content_types=("application/json",))
    assert error.value.kind == "invalid_content_type"
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("http://example.test/data")
    assert error.value.kind == "invalid_url"
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://user:secret@example.test/data")
    assert error.value.kind == "invalid_url"


def test_redirect_is_bounded_and_cannot_downgrade_to_http():
    http, _, _ = client([Response(status=302, headers={"Location": "http://example.test/a"})])
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/start")
    assert error.value.kind == "invalid_url"


def test_cross_host_https_redirect_is_rejected():
    http, _, _ = client([Response(status=302, headers={"Location": "https://other.test/a"})])
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/start")
    assert error.value.kind == "redirect_error"


def test_redirect_count_is_bounded():
    http, session, _ = client([
        Response(status=302, headers={"Location": "/one"}),
        Response(status=302, headers={"Location": "/two"}),
    ], max_redirects=1)
    with pytest.raises(DiscoveryHttpError) as error:
        http.get("https://example.test/start")
    assert error.value.kind == "redirect_error"
    assert len(session.calls) == 2
