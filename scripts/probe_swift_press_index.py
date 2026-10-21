from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import _parse_page

# Probe multiple official pagination and legacy routes. These remain index-only requests.
INDEX_URLS = (
    "https://www.swift.com/about-us/media-centre/press-releases",
    "https://www.swift.com/about-us/media-centre/press-releases?page=0",
    "https://www.swift.com/about-us/media-centre/press-releases?page=1",
    "https://www.swift.com/news-events/press-releases",
    "https://www.swift.com/news-events/press-releases?page=0",
    "https://www.swift.com/news-events/press-releases?page=1",
    "https://www.swift.com/news-events/migrated-news/press-releases?page=0",
)
# Additional official news/media landing pages; status-only because article URL patterns differ.
ALTERNATE_URLS = (
    "https://www.swift.com/insights/news",
    "https://www.swift.com/news-events/news?news_type=hot",
    "https://www.swift.com/about-us/media-centre",
)
# User-discovered official sitemap endpoints. Treat XML as sitemap diagnostics only;
# do not infer news events from sitemap URLs without validating linked pages.
SITEMAP_URLS = (
    "https://www.swift.com/sitemap.xml?page=1",
    "https://www.swift.com/sitemap.xml?page=2",
    "https://www.swift.com/sitemap.xml",
)
SITEMAP_CONTENT_TYPES = (
    "application/xml", "text/xml", "application/xhtml+xml", "text/html",
    "application/octet-stream",
)
ALLOWED_HOSTS = {"www.swift.com", "swift.com"}


def _official_final_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return (
            parts.scheme.lower() == "https"
            and parts.hostname is not None
            and parts.hostname.casefold() in ALLOWED_HOSTS
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def probe_swift_indexes(http=None) -> str:
    """Probe official Swift press-index route variants without state writes or detail requests."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Swift index diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    configured = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "swift-press-releases"
    )
    source = dict(configured)
    source["enabled"] = True
    # This diagnostic intentionally performs index-only requests; it never fetches articles.
    source.pop("detail_fallback_limit", None)
    lines = []
    for url in INDEX_URLS:
        probe_source = {**source, "source_url": url}
        try:
            response = client.get(url, expected_content_types=("text/html", "application/xhtml+xml"))
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"index={url} status=unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200:
            lines.append(f"index={url} status=unavailable reason=unexpected_http_status http_status={response.status_code}")
            continue
        if not _official_final_url(response.url):
            lines.append(f"index={url} status=rejected reason=non_official_final_url")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            lines.append(
                f"index={url} status=rejected reason=unexpected_content_type "
                f"content_type={content_type or 'missing'}"
            )
            continue
        try:
            rows, complete = _parse_page(probe_source, response.content)
        except (UnicodeDecodeError, ValueError) as exc:
            lines.append(f"index={url} status=invalid_page reason={type(exc).__name__}")
            continue
        dated = [row for row in rows if row.get("date") is not None]
        lines.append(
            f"index={url} status=valid_index complete={complete} "
            f"parsed_items={len(rows)} dated_items={len(dated)}"
        )
        for row in dated[:5]:
            lines.append(
                f"  date={row['date'].isoformat()} title={row['title'][:180]!r} url={row['url']}"
            )
    # Probe alternate official news/media landing pages without article-detail requests.
    for url in ALTERNATE_URLS:
        try:
            response = client.get(url, expected_content_types=("text/html", "application/xhtml+xml"))
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"alternate={url} status=unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200:
            lines.append(f"alternate={url} status=unavailable reason=unexpected_http_status http_status={response.status_code}")
            continue
        if not _official_final_url(response.url):
            lines.append(f"alternate={url} status=rejected reason=non_official_final_url")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            lines.append(f"alternate={url} status=rejected reason=unexpected_content_type content_type={content_type}")
            continue
        if not response.content:
            lines.append(f"alternate={url} status=unusable reason=empty_body http_status={response.status_code}")
            continue
        lines.append(f"alternate={url} status=reachable http_status={response.status_code} content_type={content_type} bytes={len(response.content)}")
    # Sitemap pages supplied by the project owner. XML is probed separately from
    # HTML index pages so a valid sitemap response is not misclassified as HTML.
    for url in SITEMAP_URLS:
        try:
            response = client.get(url, expected_content_types=SITEMAP_CONTENT_TYPES)
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"sitemap={url} status=unavailable reason={exc.kind}{suffix}")
            continue
        if not _official_final_url(response.url):
            lines.append(f"sitemap={url} status=rejected reason=non_official_final_url")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        body = response.content.lstrip()
        if response.status_code != 200:
            lines.append(
                f"sitemap={url} status=unusable reason=unexpected_http_status "
                f"http_status={response.status_code} content_type={content_type or 'missing'} "
                f"bytes={len(response.content)}"
            )
            continue
        if not body:
            lines.append(f"sitemap={url} status=unusable reason=empty_body http_status=200")
            continue
        looks_xml = (
            body.startswith(b"<?xml")
            or b"<urlset" in body[:1000].lower()
            or b"<sitemapindex" in body[:1000].lower()
        )
        if not looks_xml:
            lines.append(
                f"sitemap={url} status=unusable reason=not_recognized_as_xml "
                f"content_type={content_type or 'missing'} bytes={len(response.content)}"
            )
            continue
        # Report only sitemap shape/count; do not crawl linked URLs in this probe.
        loc_count = body.lower().count(b"<loc>")
        lines.append(
            f"sitemap={url} status=valid_xml http_status=200 "
            f"content_type={content_type or 'missing'} bytes={len(response.content)} "
            f"loc_entries={loc_count}"
        )
    return "\n".join(lines)


def main() -> None:
    print("Swift official press-index diagnostic (read-only):")
    print(probe_swift_indexes())


if __name__ == "__main__":
    main()
