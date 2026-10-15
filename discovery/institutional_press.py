from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


METHOD = "institutional_press_html"
_MAX_ITEMS_HARD = 100


def _index_request_id(source_url: str) -> str:
    """Scope conditional-request validators to the exact index URL."""
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    return f"index:{digest}"
_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
    r"|\b\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4}\b",
    re.IGNORECASE,
)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._anchor is not None:
            return
        href = next((value or "" for key, value in attrs if key.lower() == "href"), "")
        self._anchor = {"href": href, "text": ""}

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self.links.append(self._anchor)
            self._anchor = None

    def close(self) -> None:
        super().close()
        if self._anchor is not None:
            raise ValueError("institutional page ended inside an anchor")


def _clean(value: object) -> str:
    return " ".join(unescape(value).split()) if isinstance(value, str) else ""


def _clean_href(value: object) -> str:
    """Decode an HTML href attribute, including optional surrounding quotes."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return unescape(value)


def _parse_date(value: object, numeric_date_order: str = "mdy") -> datetime | None:
    if not isinstance(value, str):
        return None
    raw_value = value.strip()
    if raw_value:
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    match = _DATE_RE.search(value)
    if not match:
        return None
    raw = match.group(0).replace(",", "").replace("Sept ", "Sep ")
    if "/" in raw:
        formats = ("%d/%m/%Y",) if numeric_date_order == "dmy" else ("%m/%d/%Y",)
    elif re.match(r"^\d{1,2}\s", raw):
        formats = ("%d %B %Y", "%d %b %Y")
    else:
        formats = ("%B %d %Y", "%b %d %Y")
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _official_article(source: dict[str, Any], href: object) -> tuple[str, str] | None:
    if not isinstance(href, str) or not href.strip():
        return None
    raw = urljoin(source["source_url"], href.strip())
    parts = urlsplit(raw)
    hosts = {host.casefold() for host in source["allowed_hosts"]}
    article_path = parts.path.rstrip("/") or "/"
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in hosts
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not re.fullmatch(source["article_path_regex"], article_path, re.IGNORECASE)
    ):
        return None
    last_segment = article_path.rsplit("/", 1)[-1]
    is_file_route = re.search(r"\.(?:html?|aspx|pdf)$", last_segment, re.IGNORECASE) is not None
    canonical_path = article_path if is_file_route else f"{article_path.rstrip('/')}/"
    canonical = f"https://{parts.hostname}{canonical_path}"
    query_param = source.get("native_id_query_param")
    if isinstance(query_param, str) and query_param:
        values = parse_qs(parts.query).get(query_param, [])
        if values:
            return raw, f"{article_path.casefold()}?{query_param}={values[0]}"
    return canonical, article_path.casefold()


def validate_institutional_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items", "allowed_hosts",
        "article_path_regex",
    }
    allowed = required | {"native_id_query_param", "numeric_date_order", "detail_fallback_limit"}
    if not isinstance(source, dict) or not required.issubset(source) or set(source) - allowed:
        raise ValueError(f"Institutional source must contain {sorted(required)} and only supported optional fields")
    for key in ("source_id", "name", "category", "discovery_method", "source_url", "article_path_regex"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["discovery_method"] != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("institutional authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= _MAX_ITEMS_HARD:
        raise ValueError(f"max_items must be an integer from 1 to {_MAX_ITEMS_HARD}")
    if not isinstance(source["allowed_hosts"], list) or not source["allowed_hosts"]:
        raise ValueError("allowed_hosts must be a non-empty list")
    if any(not isinstance(host, str) or not host.strip() for host in source["allowed_hosts"]):
        raise ValueError("allowed_hosts must contain non-empty strings")
    if "native_id_query_param" in source and (
        not isinstance(source["native_id_query_param"], str) or not source["native_id_query_param"].strip()
    ):
        raise ValueError("native_id_query_param must be a non-empty string when provided")
    if "numeric_date_order" in source and (
        not isinstance(source["numeric_date_order"], str)
        or source["numeric_date_order"] not in {"mdy", "dmy"}
    ):
        raise ValueError("numeric_date_order must be mdy or dmy")
    if "detail_fallback_limit" in source and (
        type(source["detail_fallback_limit"]) is not int
        or not 1 <= source["detail_fallback_limit"] <= 3
    ):
        raise ValueError("detail_fallback_limit must be an integer from 1 to 3")
    return source


def _json_ld_articles(source: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Extract article metadata from official JSON-LD when page markup is JS-heavy."""
    rows: list[dict[str, Any]] = []

    def walk(value: object):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    # HTML permits attributes in any order and permits unquoted attribute
    # values. Match the script's opening tag first, then locate its type
    # attribute without assuming it appears before every other attribute.
    scripts = re.findall(
        r'<script\b(?=[^>]*\btype\s*=\s*(?:"application/ld\+json"|\'application/ld\+json\'|application/ld\+json(?:\s|/|>)))[^>]*>(.*?)</script\s*>',
        text, re.IGNORECASE | re.DOTALL,
    )
    for raw in scripts:
        try:
            payload = json.loads(raw.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in walk(payload):
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            # Schema.org commonly serializes @type as a full URL
            # (https://schema.org/NewsArticle) rather than a bare type name.
            # Normalize both forms without accepting unrelated structured data.
            normalized_types = {
                kind.rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold()
                for kind in types
                if isinstance(kind, str)
            } if isinstance(types, list) else set()
            if not normalized_types.intersection({"article", "newsarticle", "pressrelease", "blogposting"}):
                continue
            title = _clean(item.get("headline") or item.get("name"))
            href = item.get("url") or item.get("mainEntityOfPage")
            if isinstance(href, dict):
                href = href.get("@id") or href.get("url")
            published = _parse_date(
                item.get("datePublished") or item.get("dateCreated"),
                source.get("numeric_date_order", "mdy"),
            )
            safe = _official_article(source, href)
            if safe is None or not title or published is None:
                continue
            rows.append({"url": safe[0], "native_id": safe[1], "title": title, "date": published})
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["native_id"], row)
    return list(unique.values())


def _parse_page(source: dict[str, Any], content: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("institutional response is not valid UTF-8") from exc

    matches = list(re.finditer(
        r"<a\b[^>]*?\bhref\s*=\s*(?P<href>\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>(?P<title>.*?)</a>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    structured_rows = _json_ld_articles(source, text)
    if not matches and not structured_rows:
        raise ValueError("institutional response is missing links and structured article metadata")

    # Prefer dates inside semantic article cards. For sites that render cards
    # with generic divs instead, associate each visible/structured date with only
    # its nearest validated official article link. A date is never reused by two
    # cards, and distant page-level dates are ignored.
    article_blocks = [
        (block.start(), block.end(), block.group(0))
        for block in re.finditer(r"<article\b[^>]*>.*?</article\s*>", text, re.IGNORECASE | re.DOTALL)
    ]
    safe_by_index = {
        index: _official_article(source, _clean_href(match.group("href")))
        for index, match in enumerate(matches)
    }
    date_candidates: list[tuple[int, int, datetime]] = []
    for date_match in re.finditer(
        r"<time\b[^>]*\bdatetime=['\"](?P<date>[^'\"]+)['\"][^>]*>",
        text, re.IGNORECASE | re.DOTALL,
    ):
        parsed = _parse_date(date_match.group("date"), source.get("numeric_date_order", "mdy"))
        if parsed is not None:
            date_candidates.append((date_match.start(), date_match.end(), parsed))
    for date_match in _DATE_RE.finditer(text):
        parsed = _parse_date(date_match.group(0), source.get("numeric_date_order", "mdy"))
        if parsed is not None:
            date_candidates.append((date_match.start(), date_match.end(), parsed))
    unique_dates: dict[tuple[int, int, datetime], tuple[int, int, datetime]] = {}
    for candidate in date_candidates:
        unique_dates.setdefault(candidate, candidate)
    dates_by_native_id: dict[str, list[tuple[int, datetime]]] = {}
    for date_start, date_end, parsed in unique_dates.values():
        distances: list[tuple[int, int, str]] = []
        for link_index, link_match in enumerate(matches):
            safe_link = safe_by_index[link_index]
            if safe_link is None:
                continue
            if date_end <= link_match.start():
                distance = link_match.start() - date_end
            elif date_start >= link_match.end():
                distance = date_start - link_match.end()
            else:
                distance = 0
            if distance <= 2500:
                distances.append((distance, link_index, safe_link[1]))
        if not distances:
            continue
        nearest_distance = min(value[0] for value in distances)
        nearest = [value for value in distances if value[0] == nearest_distance]
        if len({value[2] for value in nearest}) != 1:
            continue
        _, _, native_id = nearest[0]
        dates_by_native_id.setdefault(native_id, []).append((date_start, date_end, parsed))

    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    incomplete_ids: set[str] = set()
    complete = True
    generic_titles = {
        "read more", "read more about", "learn more", "learn more about",
        "view more", "view article", "view release", "read full story",
        "continue reading", "more",
    }
    for index, match in enumerate(matches):
        safe = safe_by_index[index]
        if safe is None:
            continue
        containing_block = next(
            (block for block in article_blocks if block[0] <= match.start() < block[1]),
            None,
        )
        if containing_block is not None:
            block_start, _, block_text = containing_block
            date_attrs = list(re.finditer(
                r"<time\b[^>]*\bdatetime=['\"](?P<date>[^'\"]+)['\"][^>]*>",
                block_text, re.IGNORECASE | re.DOTALL,
            ))
            date_matches = list(_DATE_RE.finditer(block_text))
            if date_attrs:
                published = _parse_date(
                    min(date_attrs, key=lambda item: abs(block_start + item.start() - match.start())).group("date"),
                    source.get("numeric_date_order", "mdy"),
                )
            elif date_matches:
                nearest = min(date_matches, key=lambda item: abs(block_start + item.start() - match.start()))
                published = _parse_date(nearest.group(0), source.get("numeric_date_order", "mdy"))
            else:
                published = None
        else:
            published = None
        # Some JS-heavy indexes wrap each link in an <article> shell while the
        # date sits outside that shell. Prefer a local card date when available,
        # but fall back to the same bounded, unique nearest-link association when
        # the semantic wrapper contains no parseable date.
        if published is None:
            assigned_dates = dates_by_native_id.get(safe[1], [])

            def distance_to_link(item: tuple[int, int, datetime]) -> int:
                date_start, date_end, _ = item
                if date_end <= match.start():
                    return match.start() - date_end
                if date_start >= match.end():
                    return date_start - match.end()
                return 0

            nearest_assigned = min(assigned_dates, key=distance_to_link) if assigned_dates else None
            published = nearest_assigned[2] if nearest_assigned is not None else None
        title = _clean(re.sub(r"<[^>]+>", " ", match.group("title")))
        normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        if not title or normalized_title in generic_titles or published is None:
            incomplete_ids.add(safe[1])
            continue
        grouped_rows.setdefault(safe[1], []).append({
            "url": safe[0],
            "native_id": safe[1],
            "title": title,
            "date": published,
        })

    structured_by_id = {row["native_id"]: row for row in structured_rows}
    rows: list[dict[str, Any]] = []
    for native_id, variants in grouped_rows.items():
        if len(variants) == 1:
            rows.append(variants[0])
            continue
        dates = {row["date"] for row in variants}
        normalized_titles = [
            re.sub(r"[^a-z0-9]+", " ", row["title"].casefold()).strip()
            for row in variants
        ]
        longest_title = max(normalized_titles, key=len)
        if len(dates) == 1 and all(
            title in longest_title or longest_title in title
            for title in normalized_titles
        ):
            # Same official URL and date with abbreviated/expanded anchor labels:
            # keep the most descriptive title rather than reporting a false conflict.
            rows.append(max(variants, key=lambda row: len(row["title"])))
            continue

        # When duplicate anchor cards conflict, prefer structured metadata only if it
        # refers to the same validated official route, has a date represented by an
        # HTML variant, and its title is compatible with every variant. Otherwise keep
        # the conflict visible and fail closed as before.
        structured = structured_by_id.get(native_id)
        structured_title = (
            re.sub(r"[^a-z0-9]+", " ", structured["title"].casefold()).strip()
            if structured is not None else ""
        )
        same_route = structured is not None and all(
            row["url"] == structured["url"] for row in variants
        )
        compatible_title = bool(structured_title) and all(
            title in structured_title or structured_title in title
            for title in normalized_titles
        )
        if same_route and structured["date"].date() in {date.date() for date in dates} and compatible_title:
            rows.append(structured)
        else:
            complete = False
            # Preserve unresolved conflicts for the collector's fail-closed
            # duplicate identity handling; never choose arbitrarily.
            rows.extend(variants)

    valid_ids = set(grouped_rows)
    structured_ids = set(structured_by_id)
    if incomplete_ids - valid_ids - structured_ids:
        complete = False
    if not rows:
        if structured_rows:
            return structured_rows, True
        raise ValueError("institutional response has no dated official articles")
    known_ids = {item["native_id"] for item in rows}
    for row in structured_rows:
        if row["native_id"] not in known_ids:
            rows.append(row)
    return rows, complete


def _index_diagnostics(source: dict[str, Any], content: bytes, response: object) -> str:
    """Return bounded structural diagnostics without logging response bodies or query strings."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
    url = urlsplit(getattr(response, "url", ""))
    final_path = url.path[:180] or "/"
    headers = getattr(response, "headers", {})
    content_type = headers.get("content-type", "missing").split(";", 1)[0]
    href_anchors = list(re.finditer(
        r"<a\b[^>]*?\bhref\s*=\s*(?P<href>\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    official_links = sum(
        _official_article(source, _clean_href(match.group("href"))) is not None
        for match in href_anchors
    )
    full_links = list(re.finditer(
        r"<a\b[^>]*?\bhref\s*=\s*(?P<href>\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>(?P<title>.*?)</a>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    validated_links = [
        (match, _official_article(source, _clean_href(match.group("href"))))
        for match in full_links
    ]
    validated_links = [(match, safe) for match, safe in validated_links if safe is not None]
    generic_titles = {
        "read more", "read more about", "learn more", "learn more about",
        "view more", "view article", "view release", "read full story",
        "continue reading", "more",
    }
    normalized_link_titles = [
        re.sub(r"[^a-z0-9]+", " ", _clean(re.sub(r"<[^>]+>", " ", match.group("title"))).casefold()).strip()
        for match, _ in validated_links
    ]
    official_links_with_text = sum(bool(title) for title in normalized_link_titles)
    official_links_with_generic_text = sum(title in generic_titles for title in normalized_link_titles)
    article_blocks = list(re.finditer(r"<article\\b[^>]*>.*?</article\\s*>", text, re.IGNORECASE | re.DOTALL))
    official_links_in_article_blocks = sum(
        any(block.start() <= match.start() < block.end() for block in article_blocks)
        for match, _ in validated_links
    )
    date_matches = list(_DATE_RE.finditer(text))
    parseable_date_matches = [
        match for match in date_matches
        if _parse_date(match.group(0), source.get("numeric_date_order", "mdy")) is not None
    ]
    time_date_matches = list(re.finditer(
        r"<time\\b[^>]*\\bdatetime=['\\\"](?P<date>[^'\\\"]+)['\\\"][^>]*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    parseable_time_dates = sum(
        _parse_date(match.group("date"), source.get("numeric_date_order", "mdy")) is not None
        for match in time_date_matches
    )
    parseable_date_tokens_within_2500 = 0
    articles_with_parseable_date_within_2500: set[str] = set()
    for date_match in parseable_date_matches:
        distances = []
        for link_match, safe in validated_links:
            if date_match.end() <= link_match.start():
                distance = link_match.start() - date_match.end()
            elif date_match.start() >= link_match.end():
                distance = date_match.start() - link_match.end()
            else:
                distance = 0
            distances.append((distance, safe[1]))
        if not distances:
            continue
        nearest_distance = min(value[0] for value in distances)
        nearest_ids = {native_id for distance, native_id in distances if distance == nearest_distance}
        if len(nearest_ids) == 1 and nearest_distance <= 2500:
            parseable_date_tokens_within_2500 += 1
            articles_with_parseable_date_within_2500.update(nearest_ids)
    date_tokens_near_articles = 0
    dates_near_articles: set[str] = set()
    date_tokens_within_2500 = 0
    date_tokens_within_5000 = 0
    date_tokens_within_10000 = 0
    articles_within_2500: set[str] = set()
    articles_within_5000: set[str] = set()
    articles_within_10000: set[str] = set()
    nearest_distances: list[int] = []
    for date_match in date_matches:
        distances = []
        for link_match, safe in validated_links:
            if date_match.end() <= link_match.start():
                distance = link_match.start() - date_match.end()
            elif date_match.start() >= link_match.end():
                distance = date_match.start() - link_match.end()
            else:
                distance = 0
            distances.append((distance, safe[1]))
        if not distances:
            continue
        nearest_distance = min(value[0] for value in distances)
        nearest_ids = {native_id for distance, native_id in distances if distance == nearest_distance}
        nearest_distances.append(nearest_distance)
        if len(nearest_ids) != 1:
            continue
        native_id = next(iter(nearest_ids))
        if nearest_distance <= 1200:
            date_tokens_near_articles += 1
            dates_near_articles.add(native_id)
        if nearest_distance <= 2500:
            date_tokens_within_2500 += 1
            articles_within_2500.add(native_id)
        if nearest_distance <= 5000:
            date_tokens_within_5000 += 1
            articles_within_5000.add(native_id)
        if nearest_distance <= 10000:
            date_tokens_within_10000 += 1
            articles_within_10000.add(native_id)
    sorted_distances = sorted(nearest_distances)
    median_nearest_distance = sorted_distances[len(sorted_distances) // 2] if sorted_distances else -1
    script_blocks = list(re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    json_ld_scripts = sum(
        bool(re.search(
            r"\btype\s*=\s*(?:\"application/ld\+json\"|'application/ld\+json'|application/ld\+json(?:\s|/|>))",
            match.group("attrs"), re.IGNORECASE,
        ))
        for match in script_blocks
    )
    json_data_scripts = sum(
        bool(re.search(
            r"\btype\s*=\s*(?:\"application/json\"|'application/json'|application/json(?:\s|/|>))|\bid\s*=\s*(?:\"__NEXT_DATA__\"|'__NEXT_DATA__')",
            match.group("attrs"), re.IGNORECASE,
        ))
        for match in script_blocks
    )
    script_date_tokens = sum(len(_DATE_RE.findall(match.group("body"))) for match in script_blocks)
    script_bytes = sum(len(match.group("body").encode("utf-8")) for match in script_blocks)
    return (
        "index diagnostics: "
        f"final_path={final_path!r} content_type={content_type!r} "
        f"bytes={len(content)} anchor_tags={len(re.findall(r'<a\b', text, re.IGNORECASE))} "
        f"href_anchors={len(href_anchors)} allowlisted_article_links={official_links} "
        f"json_ld_scripts={json_ld_scripts} date_tokens={len(_DATE_RE.findall(text))} "
        f"date_tokens_near_article_links={date_tokens_near_articles} "
        f"articles_with_nearby_date_tokens={len(dates_near_articles)} "
        f"date_tokens_within_2500={date_tokens_within_2500} "
        f"articles_within_2500={len(articles_within_2500)} "
        f"date_tokens_within_5000={date_tokens_within_5000} "
        f"articles_within_5000={len(articles_within_5000)} "
        f"date_tokens_within_10000={date_tokens_within_10000} "
        f"articles_within_10000={len(articles_within_10000)} "
        f"parseable_date_tokens={len(parseable_date_matches)} parseable_time_dates={parseable_time_dates} "
        f"parseable_date_tokens_within_2500={parseable_date_tokens_within_2500} "
        f"articles_with_parseable_date_within_2500={len(articles_with_parseable_date_within_2500)} "
        f"official_links_with_text={official_links_with_text} "
        f"official_links_with_generic_text={official_links_with_generic_text} "
        f"official_links_in_article_blocks={official_links_in_article_blocks} article_blocks={len(article_blocks)} "
        f"median_nearest_date_distance={median_nearest_distance} "
        f"script_tags={len(script_blocks)} json_data_scripts={json_data_scripts} "
        f"script_date_tokens={script_date_tokens} script_bytes={script_bytes}"
    )


def _official_links_from_index(source: dict[str, Any], text: str) -> dict[str, str]:
    """Find allowlisted article routes in anchors and bounded quoted page-data strings."""
    links: dict[str, str] = {}
    anchor_pattern = re.compile(
        r"<a\b[^>]*?\bhref\s*=\s*(?P<href>\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in anchor_pattern.finditer(text):
        safe = _official_article(source, _clean_href(match.group("href")))
        if safe is not None:
            links.setdefault(safe[1], safe[0])

    source_path = urlsplit(source["source_url"]).path.rstrip("/") + "/"
    quoted_strings = re.finditer(
        r"""(?P<quote>["'])(?P<value>(?:\\.|[^"'\\]){1,1200})(?P=quote)""",
        text,
    )
    inspected = 0
    for match in quoted_strings:
        inspected += 1
        if inspected > 20000:
            break
        value = match.group("value")
        # JSON/JavaScript may escape URL separators. Only the route itself is
        # retained; _official_article still enforces the configured host/path.
        normalized = value.replace(r"\/", "/").replace(r"\u002F", "/").replace(r"\u002f", "/")
        if normalized.startswith(("https://", "http://")):
            candidate = normalized
        elif normalized.startswith(source_path):
            candidate = normalized
        else:
            continue
        safe = _official_article(source, candidate)
        if safe is not None:
            links.setdefault(safe[1], safe[0])
    return links


def _parse_article_detail(source: dict[str, Any], href: str, content: bytes) -> dict[str, Any] | None:
    """Extract one dated item from an official article page reached through an allowlisted index link."""
    safe = _official_article(source, href)
    if safe is None:
        return None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None

    structured_rows = _json_ld_articles(source, text)
    for row in structured_rows:
        if row["native_id"] == safe[1]:
            return row

    heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", text, re.IGNORECASE | re.DOTALL)
    if heading is None:
        return None
    title = _clean(re.sub(r"<[^>]+>", " ", heading.group(1)))
    if not title:
        return None
    date_attrs = list(re.finditer(
        r"<time\b[^>]*\bdatetime=['\"](?P<date>[^'\"]+)['\"][^>]*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    if date_attrs:
        nearest_attr = min(date_attrs, key=lambda item: abs(item.start() - heading.start()))
        published = _parse_date(nearest_attr.group("date"), source.get("numeric_date_order", "mdy"))
    else:
        dates = list(_DATE_RE.finditer(text))
        if not dates:
            return None
        nearest = min(dates, key=lambda item: abs(item.start() - heading.start()))
        published = _parse_date(nearest.group(0), source.get("numeric_date_order", "mdy"))
    if published is None:
        return None
    return {"url": safe[0], "native_id": safe[1], "title": title, "date": published}


class InstitutionalPressDiscovery:
    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_institutional_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4")
        self.now = now

    def _candidate(self, row: dict[str, Any], fetched_at: datetime) -> DiscoveryCandidate:
        native_id = row["native_id"]
        return DiscoveryCandidate(
            title=row["title"],
            url=row["url"],
            source=self.source["name"],
            published_at=row["date"],
            summary=f"{self.source['name']} official release: {row['title']}",
            source_type="discovery",
            source_id=self.source["source_id"],
            authority_tier=1,
            category=self.source["category"],
            source_native_id=native_id,
            candidate_id=f"{self.source['source_id']}:{native_id}",
            discovery_method=METHOD,
            source_url=self.source["source_url"],
            document_type=f"{self.source['name']} Press Release",
            primary_url=row["url"],
            provenance=[self.source["source_url"], row["url"]],
            collected_at=fetched_at,
            first_seen_at=fetched_at,
            last_seen_at=fetched_at,
        )


    def _detail_fallback(self, content: bytes, errors: list[str], *, exclude_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Fetch a tightly bounded number of dated official article details from a sparse index."""
        limit = self.source.get("detail_fallback_limit", 0)
        if not limit:
            return []
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"{self.source['name']}: index is not valid UTF-8 for detail fallback")
            return []

        links = _official_links_from_index(self.source, text)
        rows: list[dict[str, Any]] = []
        excluded = exclude_ids or set()
        pending = [
            (native_id, url) for native_id, url in links.items()
            if native_id not in excluded
        ]
        if not links:
            errors.append(
                f"{self.source['name']}: bounded detail fallback found no allowlisted official article links"
            )
            return []
        if not pending:
            errors.append(
                f"{self.source['name']}: bounded detail fallback found no unparsed official article links"
            )
            return []
        for native_id, url in pending[:limit]:
            try:
                response = self.http.get(
                    url, expected_content_types=("text/html", "application/xhtml+xml")
                )
            except DiscoveryHttpError as exc:
                errors.append(
                    f"{self.source['name']}: bounded article-detail fallback failed"
                    + (f" (HTTP {exc.status_code})" if exc.status_code else f" ({exc.kind})")
                )
                continue
            if response.status_code != 200:
                errors.append(
                    f"{self.source['name']}: article-detail fallback returned HTTP {response.status_code}"
                )
                continue
            row = _parse_article_detail(self.source, url, response.content)
            if row is None or row["native_id"] != native_id:
                errors.append(f"{self.source['name']}: official detail page lacked grounded title/date metadata")
                continue
            rows.append(row)
        return rows

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source["source_id"], METHOD, "not_configured",
                                   fetched_at=fetched_at)

        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        state = source_state if isinstance(source_state, dict) else {}
        index_request_id = _index_request_id(self.source["source_url"])
        validators = JsonDiscoveryState.request_validators(state, self.source["source_id"], index_request_id)
        errors: list[str] = []
        updates: dict[str, dict[str, str]] = {}
        candidates: dict[str, DiscoveryCandidate] = {}
        used_detail_fallback = False

        try:
            response = self.http.get(
                self.source["source_url"],
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("text/html", "application/xhtml+xml"),
            )
        except DiscoveryHttpError as exc:
            errors.append(
                f"{self.source['name']}: {exc.kind}"
                + (f" (HTTP {exc.status_code})" if exc.status_code else "")
            )
            return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                   fetched_at=fetched_at, errors=errors)

        if response.status_code == 304:
            updates[index_request_id] = {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }
            return DiscoveryResult(self.source["source_id"], METHOD, "not_modified",
                                   fetched_at=fetched_at, state_updates=updates)

        if response.status_code != 200:
            errors.append(f"{self.source['name']}: unexpected HTTP status {response.status_code}")
            return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                   fetched_at=fetched_at, errors=errors)

        try:
            rows, complete = _parse_page(self.source, response.content)
        except ValueError as exc:
            fallback_eligible = str(exc) in {
                "institutional response has no dated official articles",
                "institutional response is missing links and structured article metadata",
            }
            if fallback_eligible and self.source.get("detail_fallback_limit"):
                rows = self._detail_fallback(response.content, errors)
                if rows:
                    complete = False
                    used_detail_fallback = True
                    errors.append(
                        f"{self.source['name']}: index omits dated article metadata; "
                        "bounded detail fallback used, so source remains partial"
                    )
                else:
                    errors.append(
                        f"{self.source['name']}: {_index_diagnostics(self.source, response.content, response)}"
                    )
                    errors.append(f"{self.source['name']}: malformed HTML ({exc})")
                    return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                           fetched_at=fetched_at, errors=errors)
            else:
                if str(exc) == "institutional response has no dated official articles":
                    errors.append(
                        f"{self.source['name']}: {_index_diagnostics(self.source, response.content, response)}"
                    )
                errors.append(f"{self.source['name']}: malformed HTML ({exc})")
                return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                       fetched_at=fetched_at, errors=errors)

        if not complete:
            errors.append(f"{self.source['name']}: malformed article on official index")
            if not used_detail_fallback and self.source.get("detail_fallback_limit"):
                known_ids = {row["native_id"] for row in rows}
                recovered = self._detail_fallback(
                    response.content, errors, exclude_ids=known_ids
                )
                if recovered:
                    rows.extend(recovered)
                    used_detail_fallback = True
                    errors.append(
                        f"{self.source['name']}: bounded detail fallback recovered "
                        f"{len(recovered)} additional official article(s); source remains partial"
                    )
        else:
            updates[index_request_id] = {
                key: response.headers[key]
                for key in ("etag", "last_modified") if response.headers.get(key)
            }

        for row in rows:
            if row["date"] < cutoff:
                continue
            candidate = self._candidate(row, fetched_at)
            existing = candidates.get(candidate.source_native_id)
            if existing is None:
                candidates[candidate.source_native_id] = candidate
            elif existing.url != candidate.url or existing.title != candidate.title:
                errors.append(
                    f"{self.source['name']}: conflicting duplicate article {candidate.source_native_id}"
                )

        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.published_at or fetched_at, item.source_native_id),
            reverse=True,
        )[:self.source["max_items"]]
        status = "partial" if errors and (ordered or used_detail_fallback) else "failed" if errors else (
            "empty" if not ordered else "success"
        )
        return DiscoveryResult(
            self.source["source_id"], METHOD, status, candidates=ordered,
            fetched_at=fetched_at, errors=errors, state_updates=updates,
        )
