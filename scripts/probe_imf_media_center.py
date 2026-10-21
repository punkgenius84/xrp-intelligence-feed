from __future__ import annotations

import json

import feedparser
from pathlib import Path
import sys

# Running a script by path puts scripts/ at sys.path[0], not the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import InstitutionalPressDiscovery


CONFIG_PATH = Path("config/discovery_sources.json")


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


def public_rss_routes() -> list[dict[str, str]]:
    """Official IMF RSS URLs to probe without enabling any production source."""
    return [
        {
            "label": "official IMF News RSS",
            "url": "https://www.imf.org/en/News/rss?language=eng",
        },
        {
            "label": "official IMF Blogs RSS",
            "url": "https://www.imf.org/en/Blogs/rss?language=eng",
        },
    ]


def report_rss_probe(label: str, url: str) -> None:
    """Report bounded HTTP and feed-parse health; never persists or publishes."""
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.3 (read-only official-source diagnostic)",
        max_attempts=1,
    )
    try:
        response = client.get(url)
    except DiscoveryHttpError as exc:
        print(f"IMF RSS runner probe: target={label} status=error kind={exc.kind} http_status={exc.status_code}")
        return
    content_type = response.headers.get("content-type", "unknown")
    print(
        f"IMF RSS runner probe: target={label} http_status={response.status_code} "
        f"content_type={content_type} bytes={len(response.content)}"
    )
    if response.status_code < 200 or response.status_code >= 300:
        return
    parsed = feedparser.parse(response.content)
    print(
        f"feed_parse: target={label} entries={len(parsed.entries)} "
        f"bozo={bool(parsed.bozo)}"
    )
    for entry in parsed.entries[:3]:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        published = str(entry.get("published", entry.get("updated", "undated"))).strip()
        print(f"feed_entry: {published} | {title[:180]} | {link}")


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


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    media_center = next(
        (item for item in config["sources"] if item.get("source_id") == "imf-media-center-news"),
        None,
    )
    if media_center is None:
        raise SystemExit("Missing configured source: imf-media-center-news")

    report_probe("official IMF Media Center", media_center)

    # Probe the IMF's published RSS URL pattern separately from HTML routes.
    # These probes are observational only; no production source or state is changed.
    for route in public_rss_routes():
        report_rss_probe(route["label"], route["url"])

    # Probe legacy and canonical public IMF News/blog routes plus GFSR using temporary definitions.
    # None of these sources is enabled in normal discovery configuration.
    for route in public_probe_routes():
        report_probe(route["name"], route)

    # Observational only: keep the workflow green while exposing each actual
    # HTTP/parser result in logs. This script never publishes or persists state.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
