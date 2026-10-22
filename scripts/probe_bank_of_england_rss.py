from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import feedparser
from discovery.http import BoundedHttpClient, DiscoveryHttpError


FEED_URL = "https://www.bankofengland.co.uk/rss/news"
ALLOWED_HOSTS = {"www.bankofengland.co.uk", "bankofengland.co.uk"}


def _official_https_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme.casefold() == "https"
        and parts.hostname is not None
        and parts.hostname.casefold() in ALLOWED_HOSTS
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def _published(entry: object) -> str:
    if not isinstance(entry, dict):
        return "missing"
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value is not None and len(value) >= 6:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
    return "missing"


def probe_bank_of_england_rss() -> str:
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only official Bank of England RSS diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    lines = [f"Bank of England official RSS runner probe: url={FEED_URL}"]
    try:
        response = client.get(
            FEED_URL,
            expected_content_types=(
                "application/rss+xml", "application/atom+xml", "application/xml",
                "text/xml", "text/plain",
            ),
        )
    except DiscoveryHttpError as exc:
        lines.append(
            f"status=unavailable error_kind={exc.kind} http_status={exc.status_code or 'unknown'} "
            f"diagnostic={str(exc)[:220]!r}"
        )
        lines.append("persistent_state_saved=false publishing=disabled production_source_enabled=false")
        return "\n".join(lines)

    parsed = feedparser.parse(response.content)
    entries = list(parsed.entries)
    sample = entries[:12]
    valid_dates = sum(_published(entry) != "missing" for entry in sample)
    valid_links = sum(_official_https_url(entry.get("link")) for entry in sample)
    lines.append(
        f"http_status={response.status_code} content_type={response.headers.get('content-type', '')!r} "
        f"bytes={len(response.content)} parser_bozo={bool(parsed.bozo)} entries={len(entries)}"
    )
    for entry in sample:
        title = " ".join(str(entry.get("title", "")).split())[:180]
        link = str(entry.get("link", "")).strip()
        lines.append(
            f"  entry title={title!r} published_at={_published(entry)!r} "
            f"path={urlsplit(link).path[:200]!r} official_https_link={str(_official_https_url(link)).lower()}"
        )
    lines.append(
        f"sample_size={len(sample)} sample_valid_dates={valid_dates} sample_official_links={valid_links} "
        f"persistent_state_saved=false publishing=disabled production_source_enabled=false"
    )
    if parsed.bozo:
        lines.append(f"parser_diagnostic={str(getattr(parsed, 'bozo_exception', 'unknown'))[:220]!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(probe_bank_of_england_rss())
