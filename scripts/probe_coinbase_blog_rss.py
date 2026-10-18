from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
import sys
import time

import feedparser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError


RSS_URL = "https://www.coinbase.com/blog/rss.xml"
OFFICIAL_HOSTS = {"www.coinbase.com", "coinbase.com"}
TARGET_PATH = (
    "/blog/coinbase-opens-stock-trading-to-everyone-in-the-us-"
    "partners-with-yahoo-finance-to-power-discovery"
)
_ALLOWED_CONTENT_TYPES = {"application/rss+xml", "application/xml", "text/xml"}


def _official_blog_path(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in OFFICIAL_HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    path = parts.path.rstrip("/")
    if not path.startswith("/blog/") or path == "/blog/landing":
        return None
    return path


def probe_coinbase_blog_rss(http=None) -> str:
    """Read-only, one-shot probe; never writes state or changes source activation."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Coinbase RSS diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=1_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        response = client.get(
            RSS_URL,
            expected_content_types=tuple(sorted(_ALLOWED_CONTENT_TYPES)),
        )
    except DiscoveryHttpError as exc:
        status = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"status=unavailable reason={exc.kind}{status}"

    if response.status_code != 200:
        return f"status=unavailable reason=unexpected_http_status http_status={response.status_code}"

    final = urlsplit(response.url)
    if (
        final.scheme.lower() != "https"
        or final.hostname is None
        or final.hostname.casefold() not in OFFICIAL_HOSTS
        or final.port not in (None, 443)
        or final.username is not None
        or final.password is not None
    ):
        return "status=rejected reason=non_official_final_url"

    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in _ALLOWED_CONTENT_TYPES:
        return f"status=rejected reason=unexpected_content_type content_type={content_type or 'missing'}"

    parsed = feedparser.parse(response.content)
    if getattr(parsed, "bozo", False) or getattr(parsed, "version", "") not in {"rss20", "atom10", "atom"}:
        return (
            "status=invalid_feed "
            f"format={getattr(parsed, 'version', '') or 'unknown'} "
            f"entries={len(getattr(parsed, 'entries', []))}"
        )

    entries = list(getattr(parsed, "entries", []))
    if not entries:
        return f"status=valid_feed format={parsed.version} entries=0 target_route=absent"

    target_date = ""
    target_found = False
    for entry in entries:
        if _official_blog_path(entry.get("link")) != TARGET_PATH:
            continue
        target_found = True
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if stamp:
            target_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)
        break

    date_label = target_date or ("missing" if target_found else "not_applicable")
    return (
        f"status=valid_feed format={parsed.version} entries={len(entries)} "
        f"target_route={'present' if target_found else 'absent'} target_date={date_label}"
    )


def main() -> None:
    print(f"Coinbase official RSS diagnostic ({RSS_URL}): {probe_coinbase_blog_rss()}")


if __name__ == "__main__":
    main()
