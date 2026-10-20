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
from discovery.institutional_press import (
    _clean,
    _index_diagnostics,
    _official_article,
    _parse_date,
    _parse_page,
)
from scripts.probe_citi_index import (
    _embedded_json_diagnostics,
    _html_resource_diagnostics,
    _official_final_url,
    _probe_citi_js_assets,
)

NEWS_URL = "https://www.citigroup.com/global/news"


def _next_data_candidates(source: dict, content: bytes) -> list[dict]:
    """Extract only same-object title, official article URL, and explicit date fields."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    rows: dict[str, dict] = {}
    inspected = 0
    scripts = re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
        html,
        re.IGNORECASE | re.DOTALL,
    )

    def walk(value):
        stack = [value]
        visited = 0
        while stack and visited < 30000:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                yield item
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)

    for script in scripts:
        attrs = script.group("attrs")
        if not re.search(
            r"""\bid\s*=\s*(?:"__NEXT_DATA__"|'__NEXT_DATA__')|\btype\s*=\s*(?:"application/json"|'application/json')""",
            attrs,
            re.IGNORECASE,
        ):
            continue
        body = script.group("body")
        if len(body) > 300000 or inspected >= 3:
            continue
        inspected += 1
        try:
            payload = json.loads(body.strip())
        except (TypeError, ValueError):
            continue
        for item in walk(payload):
            normalized = {str(key).casefold(): value for key, value in item.items()}
            title_value = next(
                (normalized[key] for key in ("title", "headline", "name") if isinstance(normalized.get(key), str)),
                None,
            )
            href_value = next(
                (normalized[key] for key in ("url", "href", "link", "path", "route") if isinstance(normalized.get(key), str)),
                None,
            )
            if href_value is None:
                for key in ("url", "href", "link"):
                    candidate = normalized.get(key)
                    if isinstance(candidate, dict):
                        href_value = next(
                            (candidate.get(subkey) for subkey in ("url", "href", "path") if isinstance(candidate.get(subkey), str)),
                            None,
                        )
                        if href_value:
                            break
            date_value = next(
                (
                    normalized[key]
                    for key in (
                        "publishdate", "datepublished", "publishedat", "published_at",
                        "publicationdate", "publication_date",
                    )
                    if isinstance(normalized.get(key), str)
                ),
                None,
            )
            if not title_value or not href_value or not date_value:
                continue
            title = _clean(re.sub(r"<[^>]+>", " ", title_value))
            if len(title) < 12 or title.casefold() in {"read more", "learn more", "view more", "press release"}:
                continue
            safe = _official_article(source, href_value)
            published = _parse_date(date_value, source.get("numeric_date_order", "mdy"))
            if safe is None or published is None:
                continue
            rows.setdefault(
                safe[1],
                {"url": safe[0], "native_id": safe[1], "title": title, "date": published},
            )
    return sorted(rows.values(), key=lambda row: (row["date"], row["native_id"]), reverse=True)


def probe_citi_global_news_index(http=None) -> str:
    """Read-only probe of Citi's broader official news index; no state writes or activation."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Citi news-index diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    source = next(
        item for item in load_discovery_sources(ROOT / "config" / "discovery_sources.json")
        if item["source_id"] == "citi-press-releases"
    )
    source = {**source, "source_url": NEWS_URL}
    try:
        response = client.get(NEWS_URL, expected_content_types=("text/html", "application/xhtml+xml"))
    except DiscoveryHttpError as exc:
        suffix = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"Citi broader news-index diagnostic: status=unavailable reason={exc.kind}{suffix}"
    if response.status_code != 200:
        return f"Citi broader news-index diagnostic: status=unavailable reason=unexpected_http_status http_status={response.status_code}"
    if not _official_final_url(response.url):
        return "Citi broader news-index diagnostic: status=rejected reason=non_official_final_url"
    final = urlsplit(response.url)
    if final.path.rstrip("/") != "/global/news":
        return "Citi broader news-index diagnostic: status=rejected reason=unexpected_final_path"
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        return (
            "Citi broader news-index diagnostic: status=rejected reason=unexpected_content_type "
            f"content_type={content_type or 'missing'}"
        )

    rows = []
    complete = False
    parse_error = None
    try:
        rows, complete = _parse_page(source, response.content)
    except (UnicodeDecodeError, ValueError) as exc:
        parse_error = type(exc).__name__
    embedded_rows = _next_data_candidates(source, response.content)
    combined = {row["native_id"]: row for row in rows}
    for row in embedded_rows:
        combined.setdefault(row["native_id"], row)
    rows = sorted(combined.values(), key=lambda row: (row["date"], row["native_id"]), reverse=True)

    if parse_error is not None:
        lines = [
            "Citi broader news-index diagnostic: "
            f"status=invalid_page reason={parse_error} embedded_data_candidates={len(embedded_rows)}"
        ]
        if rows:
            for row in rows[:10]:
                lines.append(f"  date={row['date'].isoformat()} title={row['title'][:180]!r} url={row['url']}")
        lines.extend([
            f"  {_index_diagnostics(source, response.content, response)}",
            f"  {_html_resource_diagnostics(response.content)}",
            f"  {_embedded_json_diagnostics(response.content)}",
            f"  {_probe_citi_js_assets(client, response.content, response.url)}",
        ])
        return "\n".join(lines)

    lines = [
        "Citi broader news-index diagnostic: "
        f"status=valid_index complete={complete} parsed_items={len(rows)} "
        f"embedded_data_candidates={len(embedded_rows)}"
    ]
    for row in rows[:10]:
        date = row["date"].isoformat() if row.get("date") else "missing"
        lines.append(f"  date={date} title={row['title'][:180]!r} url={row['url']}")
    return "\n".join(lines)


def main() -> None:
    print(probe_citi_global_news_index())


if __name__ == "__main__":
    main()
