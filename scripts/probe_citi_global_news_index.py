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
        if len(body) > 1500000 or inspected >= 3:
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
            href_values = [
                normalized[key]
                for key in ("url", "href", "link", "path", "route", "slug", "nativeid", "canonicalurl", "canonical_url")
                if isinstance(normalized.get(key), str)
            ]
            for key in ("url", "href", "link", "canonicalurl", "canonical_url"):
                candidate = normalized.get(key)
                if isinstance(candidate, dict):
                    href_values.extend(
                        candidate[subkey]
                        for subkey in ("url", "href", "path", "slug", "route")
                        if isinstance(candidate.get(subkey), str)
                    )
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
            if not title_value or not href_values or not date_value:
                continue
            title = _clean(re.sub(r"<[^>]+>", " ", title_value))
            if len(title) < 12 or title.casefold() in {"read more", "learn more", "view more", "press release"}:
                continue
            valid_urls = {}
            for href_value in href_values:
                try:
                    safe = _official_article(source, href_value)
                except (TypeError, ValueError):
                    continue
                if safe is not None:
                    valid_urls.setdefault(safe[1], safe)
            # Multiple different official routes in one object are ambiguous; do not guess.
            if len(valid_urls) != 1:
                continue
            safe = next(iter(valid_urls.values()))
            published = _parse_date(date_value, source.get("numeric_date_order", "mdy"))
            if published is None:
                continue
            rows.setdefault(
                safe[1],
                {"url": safe[0], "native_id": safe[1], "title": title, "date": published},
            )
    return sorted(rows.values(), key=lambda row: (row["date"], row["native_id"]), reverse=True)



def _embedded_record_shape_diagnostics(source: dict, content: bytes) -> str:
    """Report bounded key-shape counts for route-bearing embedded records, never values."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "embedded_record_shapes unavailable=decode_error"
    route_re = re.compile(
        r"(?:https://(?:www\.)?citigroup\.com)?/global/news/press-release/20\d{2}/[a-z0-9-]+",
        re.IGNORECASE,
    )
    route_keys = {"url", "href", "link", "path", "route", "slug", "nativeid", "canonicalurl", "canonical_url"}
    title_keys = {"title", "headline", "name"}
    date_keys = {
        "date", "datepublished", "publishedat", "published_at", "publishdate", "publish_date",
        "publicationdate", "publication_date", "createdat", "created_at", "modifiedat", "modified_at",
    }
    objects = 0
    route_objects = route_title = route_date = route_title_date = 0
    shapes = []
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

    inspected = 0
    for match in scripts:
        attrs, body = match.group("attrs"), match.group("body")
        if not re.search(
            r"""\bid\s*=\s*(?:"__NEXT_DATA__"|'__NEXT_DATA__')|\btype\s*=\s*(?:"application/json"|'application/json')""",
            attrs,
            re.IGNORECASE,
        ):
            continue
        if len(body) > 1500000 or inspected >= 3:
            continue
        inspected += 1
        try:
            payload = json.loads(body.strip())
        except (TypeError, ValueError):
            continue
        for item in walk(payload):
            objects += 1
            normalized = {str(key).casefold(): value for key, value in item.items()}
            found_route_keys = sorted(
                key for key in route_keys
                if isinstance(normalized.get(key), str) and route_re.search(normalized[key])
            )
            if not found_route_keys:
                continue
            has_title = any(isinstance(normalized.get(key), str) and normalized[key].strip() for key in title_keys)
            has_date = any(isinstance(normalized.get(key), str) and normalized[key].strip() for key in date_keys)
            route_objects += 1
            route_title += int(has_title)
            route_date += int(has_date)
            route_title_date += int(has_title and has_date)
            if len(shapes) < 4:
                field_types = {}
                for field in ("info", "subtitle", "newstags", "category", "publishdatestr", "year", "month"):
                    value = normalized.get(field)
                    if value is not None:
                        field_types[field] = type(value).__name__
                        if isinstance(value, str):
                            field_types[field + "_length"] = len(value)
                info = normalized.get("info")
                info_nested_shapes = []
                nested_stack = [(info, 0)]
                nested_seen = 0
                while nested_stack and nested_seen < 100:
                    nested, depth = nested_stack.pop()
                    nested_seen += 1
                    if isinstance(nested, dict):
                        if depth <= 3 and len(info_nested_shapes) < 3:
                            info_nested_shapes.append({
                                "depth": depth,
                                "keys": sorted(str(key).casefold()[:40] for key in nested.keys())[:18],
                            })
                        if depth < 3:
                            nested_stack.extend((value, depth + 1) for value in nested.values())
                    elif isinstance(nested, list) and depth < 3:
                        nested_stack.extend((value, depth + 1) for value in nested[:20])
                shapes.append({
                    "keys": sorted(str(key).casefold()[:40] for key in item.keys())[:18],
                    "route_keys": found_route_keys,
                    "title_keys": sorted(key for key in title_keys if isinstance(normalized.get(key), str)),
                    "date_keys": sorted(key for key in date_keys if isinstance(normalized.get(key), str)),
                    "field_types": field_types,
                    "info_nested_shapes": info_nested_shapes,
                })
    return (
        f"embedded_record_shapes scripts={inspected} objects={objects} route_objects={route_objects} "
        f"route_with_title={route_title} route_with_date={route_date} "
        f"route_with_title_and_date={route_title_date} key_shapes={shapes}"
    )


def _embedded_official_article_urls(source: dict, content: bytes, limit: int = 2) -> list[str]:
    """Find a tiny bounded set of official article routes, even when title fields are absent."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return []
    route_keys = ("url", "href", "link", "path", "route", "slug", "nativeid", "canonicalurl", "canonical_url")
    urls = {}
    for script in re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        attrs, body = script.group("attrs"), script.group("body")
        if not re.search(
            r"""\bid\s*=\s*(?:"__NEXT_DATA__"|'__NEXT_DATA__')|\btype\s*=\s*(?:"application/json"|'application/json')""",
            attrs,
            re.IGNORECASE,
        ):
            continue
        if len(body) > 1500000 or len(urls) >= limit:
            continue
        try:
            payload = json.loads(body.strip())
        except (TypeError, ValueError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 30000:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                normalized = {str(key).casefold(): value for key, value in item.items()}
                for key in route_keys:
                    value = normalized.get(key)
                    if not isinstance(value, str):
                        continue
                    try:
                        safe = _official_article(source, value)
                    except (TypeError, ValueError):
                        continue
                    if safe is not None and (safe[1] in urls or len(urls) < 100):
                        urls.setdefault(safe[1], safe[0])
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return [urls[key] for key in sorted(urls)[:limit]]


def _probe_citi_article_details(http, source: dict, content: bytes) -> str:
    """Probe at most two validated official detail pages; emit metadata counts only."""
    urls = _embedded_official_article_urls(source, content, limit=2)
    if not urls:
        return "article_detail_probe selected=0"
    lines = [f"article_detail_probe selected={len(urls)}"]
    for url in urls:
        requested = urlsplit(url)
        try:
            response = http.get(
                url,
                expected_content_types=("text/html", "application/xhtml+xml"),
            )
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"  detail status=unavailable reason={exc.kind}{suffix}")
            continue
        final = urlsplit(response.url)
        if (
            response.status_code != 200
            or not _official_final_url(response.url)
            or final.path.rstrip("/") != requested.path.rstrip("/")
        ):
            lines.append(
                f"  detail status=rejected http_status={response.status_code} "
                f"final_path={final.path[:160]!r}"
            )
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            lines.append(
                f"  detail status=rejected reason=unexpected_content_type "
                f"content_type={content_type or 'missing'}"
            )
            continue
        try:
            page = response.content.decode("utf-8-sig")
        except UnicodeDecodeError:
            lines.append("  detail status=rejected reason=decode_error")
            continue
        h1_count = len(re.findall(r"<h1\b", page, re.IGNORECASE))
        title_tag_count = len(re.findall(r"<title\b", page, re.IGNORECASE))
        time_count = len(re.findall(r"<time\b[^>]*\bdatetime\s*=", page, re.IGNORECASE))
        jsonld_count = len(re.findall(
            r"""<script\b(?=[^>]*\btype\s*=\s*(?:"application/ld\+json"|'application/ld\+json'))""",
            page,
            re.IGNORECASE,
        ))
        published_meta_count = len(re.findall(
            r"""<meta\b[^>]*(?:property|name)\s*=\s*["'](?:article:published_time|datepublished|publishdate)["']""",
            page,
            re.IGNORECASE,
        ))
        lines.append(
            f"  detail status=success path={final.path[:160]!r} bytes={len(response.content)} "
            f"h1_count={h1_count} title_tag_count={title_tag_count} time_datetime_count={time_count} "
            f"jsonld_scripts={jsonld_count} published_meta_tags={published_meta_count}"
        )
    return "\n".join(lines)

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
            f"  {_embedded_record_shape_diagnostics(source, response.content)}",
            f"  {_probe_citi_article_details(client, source, response.content)}",
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
