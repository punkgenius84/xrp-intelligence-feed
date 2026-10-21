from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

# Running a script by path puts scripts/ at sys.path[0], not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import InstitutionalPressDiscovery


CONFIG_PATH = Path("config/discovery_sources.json")

# Additional official listing variants, including IMF Media Center categories.
# These are read-only endpoint checks; they do not create production source config.
PUBLIC_ENDPOINT_URLS = (
    "https://mediacenter.imf.org/newslisting?category=news",
    "https://mediacenter.imf.org/newslisting?category=press-releases",
    "https://mediacenter.imf.org/newslisting?category=features",
    "https://mediacenter.imf.org/newslisting?category=news&page=2",
    "https://www.imf.org/en/news?cid=ca-com-compd-bkstfooter",
    "https://www.imf.org/external/sitemap.htm",
    "https://www.imf.org/en/Publications/WEO",
)
PUBLIC_ENDPOINT_HOSTS = {"www.imf.org", "imf.org", "mediacenter.imf.org"}
PUBLIC_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
)


def report_probe(label: str, source: dict[str, object]) -> None:
    # Enable only this in-memory copy; normal discovery config and state remain untouched.
    probe_source = dict(source)
    probe_source["enabled"] = True
    result = InstitutionalPressDiscovery(probe_source).collect(source_state=None)

    print(f"IMF runner probe: target={label} status={result.status}")
    print(f"source_id={result.source_id} method={result.discovery_method}")
    print(f"candidate_count={len(result.candidates)}")
    for candidate in result.candidates[:5]:
        published = candidate.published_at.isoformat() if candidate.published_at else "undated"
        print(f"candidate: {published} | {candidate.title} | {candidate.url}")
    for error in result.errors[:10]:
        print(f"diagnostic: {error}")
    if len(result.errors) > 10:
        print(f"diagnostic: {len(result.errors) - 10} additional diagnostic(s) omitted")


def public_probe_routes() -> list[dict[str, object]]:
    """Return temporary, read-only source definitions for public IMF routes."""
    return [
        {
            "source_id": "imf-public-news-runner-probe",
            "name": "International Monetary Fund — Public News",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/news",
            "enabled": True,
            "lookback_days": 365,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/news/articles/\d{4}/\d{2}/\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-blogs-runner-probe",
            "name": "International Monetary Fund — Blogs",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/blogs",
            "enabled": True,
            "lookback_days": 180,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/blogs/articles/\d{4}/\d{2}/\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-public-news-canonical-runner-probe",
            "name": "International Monetary Fund — Public News (canonical-case route)",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/News",
            "enabled": True,
            "lookback_days": 365,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/news/articles/\d{4}/\d{2}/\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-blogs-canonical-runner-probe",
            "name": "International Monetary Fund — Blogs (canonical listing route)",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/Blogs?path=Blogs",
            "enabled": True,
            "lookback_days": 180,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/blogs/articles/\d{4}/\d{2}/\d{2}/[^/?#]+",
        },
        {
            "source_id": "imf-gfsr-runner-probe",
            "name": "International Monetary Fund — Global Financial Stability Report",
            "authority_tier": 1,
            "category": "institutional",
            "discovery_method": "institutional_press_html",
            "source_url": "https://www.imf.org/en/Publications/GFSR",
            "enabled": True,
            "lookback_days": 365,
            "max_items": 50,
            "allowed_hosts": ["www.imf.org", "imf.org"],
            "article_path_regex": r"/en/Publications/GFSR/Issues/\d{4}/\d{2}/\d{2}/[^/?#]+",
        },
    ]



def probe_public_endpoints(http=None) -> str:
    """Check additional official IMF listing URLs without parsing or saving state."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only IMF endpoint diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    lines = []
    for url in PUBLIC_ENDPOINT_URLS:
        try:
            response = client.get(url, expected_content_types=PUBLIC_CONTENT_TYPES)
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"IMF endpoint: url={url} status=unavailable reason={exc.kind}{suffix}")
            continue
        parts = urlsplit(response.url)
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.hostname.casefold() not in PUBLIC_ENDPOINT_HOSTS
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            lines.append(f"IMF endpoint: url={url} status=rejected reason=non_official_final_url")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = response.content.lstrip()
        if response.status_code != 200:
            lines.append(
                f"IMF endpoint: url={url} status=unusable reason=unexpected_http_status"
                f" http_status={response.status_code} content_type={content_type or 'missing'}"
                f" bytes={len(response.content)}"
            )
            continue
        if not body:
            lines.append(
                f"IMF endpoint: url={url} status=unusable reason=empty_body"
                f" http_status={response.status_code} content_type={content_type or 'missing'}"
            )
            continue
        signature = "xml" if body.startswith(b"<?xml") or b"<rss" in body[:500].lower() or b"<feed" in body[:500].lower() else "html_or_other"
        lines.append(
            f"IMF endpoint: url={url} status=reachable http_status={response.status_code}"
            f" content_type={content_type or 'missing'} bytes={len(response.content)} body_signature={signature}"
        )
    return "\n".join(lines)


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    media_center = next(
        (item for item in config["sources"] if item.get("source_id") == "imf-media-center-news"),
        None,
    )
    if media_center is None:
        raise SystemExit("Missing configured source: imf-media-center-news")

    report_probe("official IMF Media Center", media_center)

    # Probe legacy and canonical public IMF News/blog routes plus GFSR using temporary definitions.
    # None of these sources is enabled in normal discovery configuration.
    for route in public_probe_routes():
        report_probe(route["name"], route)

    print("Additional official IMF endpoint probes (read-only):")
    print(probe_public_endpoints())

    # Observational only: keep the workflow green while exposing each actual
    # HTTP/parser result in logs. This script never publishes or persists state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
