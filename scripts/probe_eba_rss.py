from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import feedparser
from discovery.http import BoundedHttpClient, DiscoveryHttpError


FEED_URL = "https://www.eba.europa.eu/news-press/news/rss.xml"
ALLOWED_HOSTS = {"www.eba.europa.eu", "eba.europa.eu"}


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


def _date(entry: object) -> str:
    if not isinstance(entry, dict):
        return "missing"
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value is not None and len(value) >= 6:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
    raw = entry.get("published") or entry.get("updated")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            return "unparsed"
    return "missing"


def probe_eba_rss() -> str:
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only official EBA RSS diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    lines = [f"EBA official RSS runner probe: url={FEED_URL}"]
    try:
        response = client.get(
            FEED_URL,
            expected_content_types=(
                "application/rss+xml",
                "application/atom+xml",
                "application/xml",
                "text/xml",
                "text/plain",
            ),
        )
    except DiscoveryHttpError as exc:
        lines.append(
            f"status=unavailable error_kind={exc.kind} http_status={exc.status_code or 'unknown'} "
            f"diagnostic={str(exc)[:220]!r}"
        )
        lines.append("persistent_state_saved=false publishing=disabled production_source_enabled=false")
        return "\n".join(lines)

    content_type = response.headers.get("content-type", "")
    parsed = feedparser.parse(response.content)
    entries = list(parsed.entries)
    valid_dates = 0
    valid_links = 0
    lines.append(
        f"http_status={response.status_code} content_type={content_type!r} bytes={len(response.content)} "
        f"parser_bozo={bool(parsed.bozo)} entries={len(entries)}"
    )
    for entry in entries[:12]:
        title = " ".join(str(entry.get("title", "")).split())[:180]
        link = str(entry.get("link", "")).strip()
        date = _date(entry)
        valid_dates += date not in {"missing", "unparsed"}
        valid_links += _official_https_url(link)
        lines.append(
            f"  entry title={title!r} date={date!r} official_https_link={str(_official_https_url(link)).lower()}"
        )
    lines.append(
        f"sample_valid_dates={valid_dates} sample_official_links={valid_links} "
        f"persistent_state_saved=false publishing=disabled production_source_enabled=false"
    )
    if parsed.bozo:
        lines.append(f"parser_diagnostic={str(getattr(parsed, 'bozo_exception', 'unknown'))[:220]!r}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(probe_eba_rss())
