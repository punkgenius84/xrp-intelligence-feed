from __future__ import annotations

from pathlib import Path
import json
import re
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



_SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_ROUTE_RE = re.compile(
    r"(?:https://(?:www\.)?citigroup\.com)?/global/news/press-release/20\d{2}/[a-z0-9-]+",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}(?:T[^\s\"'<>]+)?\b")
_RELEVANT_KEYS = {
    "title", "headline", "name", "slug", "url", "href", "link", "date",
    "datepublished", "publishedat", "published_at", "publishdate", "publish_date",
    "publicationdate", "publication_date", "createdat", "created_at", "path", "route",
}


def _embedded_json_diagnostics(content: bytes) -> str:
    """Inspect bounded JSON script data without printing raw page content."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = content.decode("utf-8", errors="replace")
    scripts = []
    for match in _SCRIPT_RE.finditer(html):
        attrs = match.group("attrs")
        if re.search(
            r"\btype\s*=\s*(?:\"application/json\"|'application/json'|application/json(?:\s|/|$))"
            r"|\bid\s*=\s*(?:\"__NEXT_DATA__\"|'__NEXT_DATA__')",
            attrs, re.IGNORECASE,
        ):
            scripts.append(match.group("body"))
    parsed = 0
    visited_total = 0
    route_values: set[str] = set()
    top_keys: set[str] = set()
    relevant_counts: dict[str, int] = {}
    date_string_count = 0
    skipped_oversize = 0
    for body in scripts[:2]:
        if len(body.encode("utf-8")) > 1_500_000:
            skipped_oversize += 1
            continue
        try:
            payload = json.loads(body.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        parsed += 1
        if isinstance(payload, dict):
            top_keys.update(str(key)[:60] for key in list(payload)[:20])
        stack = [payload]
        visited = 0
        while stack and visited < 50_000:
            value = stack.pop()
            visited += 1
            if isinstance(value, dict):
                for key, child in list(value.items())[:10_000]:
                    normalized = str(key).casefold()
                    if normalized in _RELEVANT_KEYS:
                        relevant_counts[normalized] = relevant_counts.get(normalized, 0) + 1
                    if isinstance(child, (dict, list)):
                        stack.append(child)
                    elif isinstance(child, str):
                        route_values.update(match.group(0).rstrip(".,") for match in _ROUTE_RE.finditer(child))
                        date_string_count += len(_ISO_DATE_RE.findall(child))
            elif isinstance(value, list):
                stack.extend(value[:10_000])
            elif isinstance(value, str):
                route_values.update(match.group(0).rstrip(".,") for match in _ROUTE_RE.finditer(value))
                date_string_count += len(_ISO_DATE_RE.findall(value))
        visited_total += visited
    top_key_list = sorted(top_keys)[:12]
    key_counts = {key: relevant_counts[key] for key in sorted(relevant_counts)}
    route_samples = sorted(route_values)[:5]
    return (
        f"embedded_json scripts={len(scripts)} parsed={parsed} visited_nodes={visited_total} "
        f"skipped_oversize={skipped_oversize} top_keys={top_key_list} "
        f"relevant_key_counts={key_counts} official_route_strings={len(route_values)} "
        f"iso_date_strings={date_string_count} route_samples={route_samples}"
    )


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
        embedded = _embedded_json_diagnostics(response.content)
        return (
            f"Citi official index diagnostic: status=invalid_page reason={type(exc).__name__}\n"
            f"  {structure}\n"
            f"  {embedded}"
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
