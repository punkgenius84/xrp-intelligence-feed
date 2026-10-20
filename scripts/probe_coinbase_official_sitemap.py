from __future__ import annotations

from pathlib import Path
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError

ROBOTS_URL = "https://www.coinbase.com/robots.txt"
TARGET_URL = (
    "https://www.coinbase.com/blog/"
    "coinbase-opens-stock-trading-to-everyone-in-the-us-partners-with-yahoo-finance-to-power-discovery"
)
TARGET_PATH = urlsplit(TARGET_URL).path.rstrip("/")
ALLOWED_HOSTS = {"www.coinbase.com", "coinbase.com"}
MAX_BYTES = 2_000_000
MAX_SITEMAPS = 3


def _official_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme.casefold() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in ALLOWED_HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        return None
    return value.strip()


def _robots_sitemaps(content: bytes) -> list[str]:
    if len(content) > 256_000:
        return []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    urls = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().casefold() == "sitemap":
            safe = _official_url(value.strip())
            if safe and safe not in urls:
                urls.append(safe)
    return urls[:MAX_SITEMAPS]


def _sitemap_target_metadata(content: bytes) -> tuple[str, list[str]]:
    if len(content) > MAX_BYTES:
        return "oversize", []
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return "invalid_xml", []
    root_kind = root.tag.rsplit("}", 1)[-1].casefold()
    if root_kind not in {"urlset", "sitemapindex"}:
        return "unexpected_root", []
    matches = []
    for node in list(root)[:30000]:
        fields = {}
        news_dates = []
        for child in list(node):
            local = child.tag.rsplit("}", 1)[-1].casefold()
            fields[local] = (child.text or "").strip()
            if local == "news":
                for nested in list(child):
                    if nested.tag.rsplit("}", 1)[-1].casefold() == "publication_date":
                        value = (nested.text or "").strip()
                        if value:
                            news_dates.append(value)
        loc = _official_url(fields.get("loc"))
        if not loc or urlsplit(loc).path.rstrip("/") != TARGET_PATH:
            continue
        # Only explicit Google News publication_date is eligible. <lastmod> is not publication date.
        matches.extend(news_dates)
    return root_kind, matches


def probe_coinbase_official_sitemap(http=None) -> str:
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Coinbase sitemap diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=MAX_BYTES,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        robots = client.get(ROBOTS_URL, expected_content_types=("text/plain",))
    except DiscoveryHttpError as exc:
        suffix = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"status=robots_unavailable reason={exc.kind}{suffix}"
    if robots.status_code != 200 or _official_url(robots.url) is None:
        return f"status=robots_rejected http_status={robots.status_code}"
    sitemap_urls = _robots_sitemaps(robots.content)
    if not sitemap_urls:
        return "status=no_official_sitemap_directives"
    visited = set()
    queue = list(sitemap_urls)
    target_dates = []
    target_found = False
    errors = []
    while queue and len(visited) < MAX_SITEMAPS:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            response = client.get(url, expected_content_types=("application/xml", "text/xml", "application/octet-stream"))
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            errors.append(f"sitemap_unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200 or _official_url(response.url) is None:
            errors.append(f"sitemap_rejected http_status={response.status_code}")
            continue
        kind, dates = _sitemap_target_metadata(response.content)
        if kind == "invalid_xml" or kind == "oversize" or kind == "unexpected_root":
            errors.append(f"sitemap_invalid reason={kind}")
            continue
        if dates:
            target_found = True
            target_dates.extend(dates)
        if kind == "sitemapindex":
            try:
                root = ET.fromstring(response.content)
            except (ET.ParseError, ValueError):
                continue
            children = []
            for node in list(root)[:30000]:
                loc = next(
                    ((child.text or "").strip() for child in list(node)
                     if child.tag.rsplit("}", 1)[-1].casefold() == "loc"),
                    "",
                )
                safe = _official_url(loc)
                if safe and safe not in visited and safe not in queue:
                    # Prefer blog/news sitemap names; keep traversal bounded and official-host-only.
                    children.append(safe)
            children.sort(key=lambda value: ("blog" not in value.casefold() and "news" not in value.casefold(), value))
            queue.extend(children[:MAX_SITEMAPS])
    unique_dates = sorted(set(target_dates))
    if len(unique_dates) == 1:
        result = f"status=official_sitemap_publication_date date={unique_dates[0]}"
    elif len(unique_dates) > 1:
        result = f"status=conflicting_official_sitemap_dates distinct_dates={len(unique_dates)}"
    elif target_found:
        result = "status=target_in_sitemap_without_publication_date"
    else:
        result = "status=target_absent_from_bounded_official_sitemaps"
    suffix = f" errors={len(errors)}" if errors else " errors=0"
    return f"{result} sitemaps_checked={len(visited)}{suffix}"


def main() -> None:
    print(f"Coinbase official sitemap diagnostic: {probe_coinbase_official_sitemap()}")


if __name__ == "__main__":
    main()
