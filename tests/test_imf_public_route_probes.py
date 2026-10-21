import json
import re
from pathlib import Path

from discovery.http import DiscoveryHttpError, HttpResponse
from scripts.probe_imf_media_center import (
    PUBLIC_ENDPOINT_HOSTS,
    PUBLIC_ENDPOINT_URLS,
    probe_public_endpoints,
    public_probe_routes,
)

ROOT = Path(__file__).resolve().parents[1]


def test_imf_public_routes_are_probed_but_remain_disabled_in_production_config():
    config = json.loads((ROOT / "config" / "discovery_sources.json").read_text(encoding="utf-8"))
    configured = {item["source_id"]: item for item in config["sources"]}
    assert configured["imf-blogs"]["enabled"] is False
    assert configured["imf-global-financial-stability-report"]["enabled"] is False
    assert configured["imf-media-center-news"]["enabled"] is False

    routes = {item["source_id"]: item for item in public_probe_routes()}
    assert set(routes) == {
        "imf-public-news-runner-probe",
        "imf-blogs-runner-probe",
        "imf-public-news-canonical-runner-probe",
        "imf-blogs-canonical-runner-probe",
        "imf-gfsr-runner-probe",
    }
    samples = {
        "imf-public-news-runner-probe": "/en/news/articles/2026/10/21/sample-news",
        "imf-blogs-runner-probe": "/en/blogs/articles/2026/10/21/sample-blog",
        "imf-public-news-canonical-runner-probe": "/en/news/articles/2026/10/21/sample-news",
        "imf-blogs-canonical-runner-probe": "/en/blogs/articles/2026/10/21/sample-blog",
        "imf-gfsr-runner-probe": "/en/Publications/GFSR/Issues/2026/10/21/sample-report",
    }
    for source_id, route in routes.items():
        assert route["enabled"] is True  # in-memory probe only
        assert route["source_url"].startswith("https://www.imf.org/")
        assert set(route["allowed_hosts"]) <= {"www.imf.org", "imf.org"}
        assert re.search(route["article_path_regex"], samples[source_id])



class FakeEndpointHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_additional_imf_endpoint_probes_are_official_and_read_only():
    assert len(PUBLIC_ENDPOINT_URLS) >= 7
    for url in PUBLIC_ENDPOINT_URLS:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.hostname in PUBLIC_ENDPOINT_HOSTS

    responses = [
        HttpResponse(200, {"content-type": "text/html"}, b"<html><body>official listing</body></html>", url)
        for url in PUBLIC_ENDPOINT_URLS
    ]
    http = FakeEndpointHttp(responses)
    output = probe_public_endpoints(http)
    assert output.count("status=reachable") == len(PUBLIC_ENDPOINT_URLS)
    assert len(http.calls) == len(PUBLIC_ENDPOINT_URLS)
    assert all(call[1]["expected_content_types"] for call in http.calls)


def test_additional_imf_endpoint_probes_report_forbidden_routes_without_raising():
    errors = [
        DiscoveryHttpError("HTTP status 403", kind="http_error", status_code=403)
        for _ in PUBLIC_ENDPOINT_URLS
    ]
    http = FakeEndpointHttp(errors)
    output = probe_public_endpoints(http)
    assert output.count("status=unavailable") == len(PUBLIC_ENDPOINT_URLS)
    assert output.count("http_status=403") == len(PUBLIC_ENDPOINT_URLS)
    assert len(http.calls) == len(PUBLIC_ENDPOINT_URLS)



def test_additional_imf_endpoint_probes_do_not_treat_empty_202_as_usable():
    responses = [
        HttpResponse(202, {"content-type": "text/html"}, b"", url)
        for url in PUBLIC_ENDPOINT_URLS
    ]
    http = FakeEndpointHttp(responses)
    output = probe_public_endpoints(http)
    assert output.count("status=unusable reason=unexpected_http_status") == len(PUBLIC_ENDPOINT_URLS)
    assert output.count("http_status=202") == len(PUBLIC_ENDPOINT_URLS)
