from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.dispatch import load_discovery_sources
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import _index_diagnostics, _parse_page

URL = "https://www.citigroup.com/global/news/press-release"
ALLOWED_HOSTS = {"www.citigroup.com", "citigroup.com"}


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


def probe_citi_index(http=None) -> str:
    """Probe Citi's official index once; no detail fetches, state writes, or source activation."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi index diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    try:
        response = client.get(URL, expected_content_types=("text/html", "application/xhtml+xml"))
    except DiscoveryHttpError as exc:
        suffix = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"Citi official index diagnostic: status=unavailable reason={exc.kind}{suffix}"
    if response.status_code != 200:
        return f"Citi official index diagnostic: status=unavailable reason=unexpected_http_status http_status={response.status_code}"
    if not _official_final_url(response.url):
        return "Citi official index diagnostic: status=rejected reason=non_official_final_url"
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return (
            "Citi official index diagnostic: status=rejected reason=unexpected_content_type "
            f"content_type={content_type or 'missing'}"
        )
    try:
        rows, complete = _parse_page(source, response.content)
    except (UnicodeDecodeError, ValueError) as exc:
        structure = _index_diagnostics(source, response.content, response)
        return (
            f"Citi official index diagnostic: status=invalid_page reason={type(exc).__name__}\n"
            f"  {structure}"
        )
    lines = [
        "Citi official index diagnostic: "
        f"status=valid_index complete={complete} parsed_items={len(rows)}"
    ]
    for row in rows[:5]:
        date = row["date"].isoformat() if row.get("date") else "missing"
        lines.append(f"  date={date} title={row['title'][:180]!r} url={row['url']}")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_index())


if __name__ == "__main__":
    main()
