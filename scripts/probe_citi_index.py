from __future__ import annotations

from pathlib import Path
import json
import re
import sys
from urllib.parse import urljoin, urlsplit

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


def _safe_citi_url_sample(value: str) -> str:
    """Return only a bounded official URL path, never query strings or external hosts."""
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return ""
    if parts.scheme:
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.hostname.casefold() not in ALLOWED_HOSTS
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
        ):
            return ""
    path = parts.path
    if not path or not (
        path.startswith("/global/")
        or "press-release" in path.casefold()
        or "news" in path.casefold()
    ):
        return ""
    return path[:160]


def _official_script_asset_urls(content: bytes, base_url: str = URL) -> list[str]:
    """Select at most two same-host Next.js bundles; never follow arbitrary script hosts."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = content.decode("utf-8", errors="replace")
    selected: set[str] = set()
    for match in re.finditer(r"<script\b(?P<attrs>[^>]*)>", html, re.IGNORECASE | re.DOTALL):
        attrs = match.group("attrs")
        src_match = re.search(
            r"\bsrc\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
            attrs, re.IGNORECASE | re.DOTALL,
        )
        if src_match is None:
            continue
        src = src_match.group("double") or src_match.group("single") or src_match.group("bare") or ""
        try:
            parts = urlsplit(urljoin(base_url, src.strip()))
        except ValueError:
            continue
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.hostname.casefold() not in ALLOWED_HOSTS
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or "/_next/static/chunks/" not in parts.path
            or not parts.path.casefold().endswith(".js")
        ):
            continue
        selected.add(f"https://{parts.hostname.casefold()}{parts.path}")
    return sorted(
        selected,
        key=lambda value: (
            0 if "/chunks/pages/%5b" in value.casefold() or "/chunks/pages/[" in value else
            1 if "/chunks/main-" in value.casefold() else 2,
            value,
        ),
    )[:2]


def _inspect_citi_js_asset(content: bytes, url: str) -> str:
    """Summarize route/API hints from a bounded official JS asset, never print source."""
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        source = content.decode("utf-8", errors="replace")
    route_values = {
        urlsplit(match.group(0).rstrip(".,")).path
        for match in _ROUTE_RE.finditer(source)
    }
    api_paths = {
        match.group(0).split("?", 1)[0].split("#", 1)[0]
        for match in re.finditer(
            r"""(?:/graphql|/(?:api|global/api)(?:/[a-zA-Z0-9._~!def _html_resource_diagnostics(content: bytes) -> str:
'()*+,;=:@%/-]{1,120})?)""",
            source,
            re.IGNORECASE,
        )
    }
    operations = set(re.findall(
        r"\b(?:query|mutation)\s+([A-Za-z_][A-Za-z0-9_]{2,70})",
        source,
    ))
    path = urlsplit(url).path
    return (
        f"js_asset path={path[:180]!r} bytes={len(content)} "
        f"official_route_strings={len(route_values)} route_samples={sorted(route_values)[:5]} "
        f"api_like_paths={sorted(api_paths)[:10]} graphql_operation_names={sorted(operations)[:10]}"
    )


def _probe_citi_js_assets(http, content: bytes, base_url: str) -> str:
    urls = _official_script_asset_urls(content, base_url)
    if not urls:
        return "js_asset_probe selected=0"
    lines = [f"js_asset_probe selected={len(urls)}"]
    for url in urls:
        try:
            response = http.get(
                url,
                expected_content_types=("application/javascript", "text/javascript", "application/x-javascript"),
            )
        except DiscoveryHttpError as exc:
            suffix = f" http_status={exc.status_code}" if exc.status_code else ""
            lines.append(f"  js_asset status=unavailable reason={exc.kind}{suffix}")
            continue
        if response.status_code != 200:
            lines.append(f"  js_asset status=unavailable reason=unexpected_http_status http_status={response.status_code}")
            continue
        if not _official_final_url(response.url):
            lines.append("  js_asset status=rejected reason=non_official_final_url")
            continue
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/javascript", "text/javascript", "application/x-javascript"}:
            lines.append(
                "  js_asset status=rejected reason=unexpected_content_type "
                f"content_type={content_type or 'missing'}"
            )
            continue
        lines.append("  " + _inspect_citi_js_asset(response.content, response.url))
    return "\n".join(lines)


def _html_resource_diagnostics(content: bytes) -> str:
    """Report bounded official script paths and API-like paths without fetching them."""
    try:
        html = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        html = content.decode("utf-8", errors="replace")
    script_tags = list(re.finditer(r"<script\b(?P<attrs>[^>]*)>", html, re.IGNORECASE | re.DOTALL))
    official_paths: set[str] = set()
    external_count = 0
    for match in script_tags:
        attrs = match.group("attrs")
        src_match = re.search(
            r"\bsrc\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
            attrs, re.IGNORECASE | re.DOTALL,
        )
        if src_match is None:
            continue
        src = src_match.group("double") or src_match.group("single") or src_match.group("bare") or ""
        try:
            parts = urlsplit(urljoin(URL, src.strip()))
        except ValueError:
            external_count += 1
            continue
        if (
            parts.scheme.lower() == "https"
            and parts.hostname is not None
            and parts.hostname.casefold() in ALLOWED_HOSTS
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        ):
            if parts.path:
                official_paths.add(parts.path[:180])
        else:
            external_count += 1
    api_like_paths = set()
    for match in re.finditer(
        r"""(?P<path>/(?:api|graphql|_next/data|global/api)(?:/[a-zA-Z0-9._~!def _embedded_json_diagnostics(content: bytes) -> str:
'()*+,;=:@%/-]{1,180})?)""",
        html,
        re.IGNORECASE,
    ):
        path = match.group("path").split("?", 1)[0].split("#", 1)[0]
        if path and not path.startswith("//"):
            api_like_paths.add(path)
    return (
        f"html_resources script_tags={len(script_tags)} "
        f"official_script_srcs={len(official_paths)} external_script_srcs={external_count} "
        f"official_script_paths={sorted(official_paths)[:8]} "
        f"api_like_paths={sorted(api_like_paths)[:10]}"
    )


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
    url_samples: set[str] = set()
    press_release_like_strings = 0
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
                        route_values.update(urlsplit(match.group(0).rstrip(".,")).path for match in _ROUTE_RE.finditer(child))
                        date_string_count += len(_ISO_DATE_RE.findall(child))
                        if "press-release" in child.casefold() or "press release" in child.casefold():
                            press_release_like_strings += 1
                        if normalized in {"url", "link", "href", "path", "canonicalurl", "canonical_url"}:
                            sample = _safe_citi_url_sample(child)
                            if sample:
                                url_samples.add(f"{normalized}:{sample}")
            elif isinstance(value, list):
                stack.extend(value[:10_000])
            elif isinstance(value, str):
                route_values.update(urlsplit(match.group(0).rstrip(".,")).path for match in _ROUTE_RE.finditer(value))
                date_string_count += len(_ISO_DATE_RE.findall(value))
                if "press-release" in value.casefold() or "press release" in value.casefold():
                    press_release_like_strings += 1
        visited_total += visited
    top_key_list = sorted(top_keys)[:12]
    key_counts = {key: relevant_counts[key] for key in sorted(relevant_counts)}
    route_samples = sorted(route_values)[:5]
    return (
        f"embedded_json scripts={len(scripts)} parsed={parsed} visited_nodes={visited_total} "
        f"skipped_oversize={skipped_oversize} top_keys={top_key_list} "
        f"relevant_key_counts={key_counts} official_route_strings={len(route_values)} "
        f"press_release_like_strings={press_release_like_strings} iso_date_strings={date_string_count} "
        f"route_samples={route_samples} url_samples={sorted(url_samples)[:10]}"
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
        resources = _html_resource_diagnostics(response.content)
        embedded = _embedded_json_diagnostics(response.content)
        js_assets = _probe_citi_js_assets(client, response.content, response.url)
        return (
            f"Citi official index diagnostic: status=invalid_page reason={type(exc).__name__}\n"
            f"  {structure}\n"
            f"  {resources}\n"
            f"  {embedded}\n"
            f"  {js_assets}"
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
