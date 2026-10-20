from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import InstitutionalPressDiscovery, _official_article, _parse_article_detail

SITEMAP_URL = "https://www.citigroup.com/global/sitemap.xml"
ALLOWED_HOSTS = {"www.citigroup.com", "citigroup.com"}
MAX_SITEMAPS = 3
MAX_DETAIL_PAGES = 3
MAX_XML_BYTES = 2_000_000


def _safe_sitemap_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in ALLOWED_HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/global/")
        or parts.query
        or parts.fragment
    ):
        return None
    return value.strip()


def _parse_sitemap(content: bytes) -> tuple[str, list[tuple[str, str]]]:
    if len(content) > MAX_XML_BYTES:
        return "oversize", []
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return "invalid_xml", []
    kind = root.tag.rsplit("}", 1)[-1].casefold()
    if kind not in {"urlset", "sitemapindex"}:
        return "unexpected_root", []
    entries = []
    for node in list(root):
        fields = {}
        for child in list(node):
            fields[child.tag.rsplit("}", 1)[-1].casefold()] = (child.text or "").strip()
        loc = _safe_sitemap_url(fields.get("loc"))
        if loc:
            entries.append((loc, fields.get("lastmod", "")))
    return kind, entries


def _candidate_article_urls(source: dict, entries: list[tuple[str, str]]) -> list[str]:
    candidates = {}
    for url, lastmod in entries:
        safe = _official_article(source, url)
        if safe is None:
            continue
        # lastmod is only a fetch-order hint; never use it as publication time.
        candidates.setdefault(safe[1], (safe[0], lastmod))
    return [
        item[0]
        for item in sorted(
            candidates.values(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )[:MAX_DETAIL_PAGES]
    ]


def probe_citi_sitemap(http=None) -> str:
    """Probe official sitemap discovery and dated detail extraction without state writes."""
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi sitemap diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=MAX_XML_BYTES,
        max_redirects=2,
        max_attempts=1,
    )
    sitemap_urls = [SITEMAP_URL]
    visited_sitemaps = set()
    article_entries = []
    errors = []
    while sitemap_urls and len(visited_sitemaps) < MAX_SITEMAPS:
        url = sitemap_urls.pop(0)
        if url in visited_sitemaps:
            continue
        visited_sitemaps.add(url)
        try:
            response = client.get(url, expected_content_types=("application/xml", "text/xml", "application/octet-stream"))
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            errors.append(f"sitemap_unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200 or _safe_sitemap_url(response.url) is None:
            errors.append(f"sitemap_rejected status={response.status_code} reason=invalid_final_url_or_status")
            continue
        kind, entries = _parse_sitemap(response.content)
        if kind == "sitemapindex":
            for loc, _ in entries:
                if loc not in visited_sitemaps and loc not in sitemap_urls:
                    sitemap_urls.append(loc)
                    if len(sitemap_urls) + len(visited_sitemaps) >= MAX_SITEMAPS:
                        break
        elif kind == "urlset":
            article_entries.extend(entries)

    article_urls = _candidate_article_urls(source, article_entries)
    lines = [
        f"Citi sitemap diagnostic: sitemaps_fetched={len(visited_sitemaps)} "
        f"article_urls={len(article_entries)} selected_detail_pages={len(article_urls)}"
    ]
    for url in article_urls:
        try:
            response = client.get(url, expected_content_types=("text/html", "application/xhtml+xml"))
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"  detail status=unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200:
            lines.append(f"  detail status=unavailable http_status={response.status_code}")
            continue
        row = _parse_article_detail(source, url, response.content)
        if row is None:
            lines.append(f"  detail status=unparsed path={urlsplit(url).path[:180]!r}")
            continue
        lines.append(
            f"  detail status=success path={urlsplit(row['url']).path[:180]!r} "
            f"published_at={row['date'].isoformat()} title_length={len(row['title'])}"
        )
    lines.extend(f"  {error}" for error in errors[:4])
    lines.append("  state_updates=0 observations=0 publishing=disabled")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_sitemap())


if __name__ == "__main__":
    main()
