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
        or not 1 <= source["detail_fallback_limit"] <= 5
    ):
        raise ValueError("detail_fallback_limit must be an integer from 1 to 5")
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


def _route_month_is_before_cutoff(native_id: str, cutoff: datetime) -> bool:
    """Recognize old Mastercard press routes only when their whole URL month predates cutoff."""
    match = re.search(
        r"/press/(20\d{2})/(january|february|march|april|may|june|july|august|september|october|november|december)/",
        native_id.casefold(),
    )
    if match is None:
        return False
    year = int(match.group(1))
    month = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }[match.group(2)]
    next_month = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1, tzinfo=timezone.utc)
    return next_month <= cutoff.astimezone(timezone.utc)


def _semantic_container_ranges(text: str, *, max_chars: int = 10000) -> list[tuple[int, int]]:
    """Return bounded div/li/section/article ranges while ignoring script contents."""
    masked = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1\s*>",
        lambda match: " " * len(match.group(0)),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    tag_pattern = re.compile(r"<(?P<close>/)?(?P<tag>div|li|section|article)\b[^>]*>", re.IGNORECASE)
    stack: list[tuple[str, int]] = []
    ranges: list[tuple[int, int]] = []
    for match in tag_pattern.finditer(masked):
        tag = match.group("tag").casefold()
        if match.group("close"):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == tag:
                    start = stack[index][1]
                    del stack[index:]
                    if match.end() - start <= max_chars:
                        ranges.append((start, match.end()))
                    break
        elif not re.search(r"/\s*>$", match.group(0)):
            stack.append((tag, match.start()))
    return ranges


def _parse_page(
    source: dict[str, Any], content: bytes, diagnostics: dict[str, Any] | None = None,
    *, lookback_cutoff: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
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
    # Dates embedded in a linked headline (for example, "earnings on November 4")
    # and in JSON-LD headlines are not publication dates. JSON-LD publication metadata
    # is parsed separately above; keep those script tokens out of proximity assignment.
    headline_ranges = [
        (headline.start(), headline.end())
        for headline in re.finditer(
                    r"<h[1-6]\b[^>]*>.*?</h[1-6]\s*>",
            text, re.IGNORECASE | re.DOTALL,
        )
    ]
    json_ld_script_ranges = [
        (script.start(), script.end())
        for script in re.finditer(
            r"""<script\b(?=[^>]*\btype\s*=\s*(?:"application/ld\+json"|'application/ld\+json'|application/ld\+json(?:\s|/|>)))[^>]*>.*?</script\s*>""",
            text, re.IGNORECASE | re.DOTALL,
        )
    ]
    for date_match in _DATE_RE.finditer(text):
        start, end = date_match.start(), date_match.end()
        if any(
            heading.start() <= start and end <= heading.end()
            for heading in headline_ranges
        ):
            continue
        if any(script_start <= start and end <= script_end for script_start, script_end in json_ld_script_ranges):
            continue
        parsed = _parse_date(date_match.group(0), source.get("numeric_date_order", "mdy"))
        if parsed is not None:
            date_candidates.append((start, end, parsed))
    unique_dates: dict[tuple[int, int, datetime], tuple[int, int, datetime]] = {}
    for candidate in date_candidates:
        unique_dates.setdefault(candidate, candidate)
    dates_by_native_id: dict[str, list[tuple[int, datetime]]] = {}
    assigned_date_spans: set[tuple[int, int, datetime]] = set()
    link_assignment_by_date: dict[tuple[int, int, datetime], str] = {}
    container_owner_by_date: dict[tuple[int, int, datetime], str] = {}
    nearest_date_gap_by_native_id: dict[str, int] = {}
    unique_nearest_date_tokens_by_native_id: dict[str, int] = {}
    tied_nearest_date_tokens_by_native_id: dict[str, int] = {}
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
            distances.append((distance, link_index, safe_link[1]))
            prior_gap = nearest_date_gap_by_native_id.get(safe_link[1])
            if prior_gap is None or distance < prior_gap:
                nearest_date_gap_by_native_id[safe_link[1]] = distance
        if not distances:
            continue
        nearest_distance = min(value[0] for value in distances)
        nearest = [value for value in distances if value[0] == nearest_distance]
        nearest_ids = {value[2] for value in nearest}
        if len(nearest_ids) == 1:
            nearest_id = next(iter(nearest_ids))
            unique_nearest_date_tokens_by_native_id[nearest_id] = unique_nearest_date_tokens_by_native_id.get(nearest_id, 0) + 1
        else:
            for nearest_id in nearest_ids:
                tied_nearest_date_tokens_by_native_id[nearest_id] = tied_nearest_date_tokens_by_native_id.get(nearest_id, 0) + 1
        if nearest_distance <= 2500 and len(nearest_ids) == 1:
            native_id = next(iter(nearest_ids))
            dates_by_native_id.setdefault(native_id, []).append((date_start, date_end, parsed))
            date_span = (date_start, date_end, parsed)
            assigned_date_spans.add(date_span)
            link_assignment_by_date[date_span] = native_id

    heading_candidates_by_native_id: dict[str, list[tuple[float, int, int, str]]] = {}
    heading_positions_by_native_id: dict[str, list[tuple[int, int, int, str]]] = {}
    generic_heading_labels = {"news", "press release", "press releases", "blog", "article", "read more", "learn more"}
    stop_words = {"a", "an", "and", "for", "in", "of", "on", "the", "to", "with"}
    safe_url_by_id = {
        safe[1]: safe[0] for safe in safe_by_index.values() if safe is not None
    }
    heading_matches = list(re.finditer(
        r"<h[1-6]\b[^>]*>(?P<title>.*?)</h[1-6]\s*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    for heading in heading_matches:
        heading_title = _clean(re.sub(r"<[^>]+>", " ", heading.group("title")))
        normalized_heading = re.sub(r"[^a-z0-9]+", " ", heading_title.casefold()).strip()
        if len(normalized_heading) < 12 or len(normalized_heading.split()) < 2 or normalized_heading in generic_heading_labels:
            continue
        distances = []
        for link_index, link_match in enumerate(matches):
            safe_link = safe_by_index[link_index]
            if safe_link is None:
                continue
            if heading.end() <= link_match.start():
                distance = link_match.start() - heading.end()
            elif heading.start() >= link_match.end():
                distance = heading.start() - link_match.end()
            else:
                distance = 0
            if distance <= 2500:
                distances.append((distance, safe_link[1]))
        if not distances:
            continue
        nearest_distance = min(value[0] for value in distances)
        nearest_ids = {native_id for distance, native_id in distances if distance == nearest_distance}
        if len(nearest_ids) != 1:
            continue
        native_id = next(iter(nearest_ids))
        article_path = urlsplit(safe_url_by_id[native_id]).path.rstrip("/")
        slug = re.sub(r"\.(?:html?|aspx?)$", "", article_path.rsplit("/", 1)[-1].casefold())
        slug_tokens = set(re.findall(r"[a-z0-9]+", slug)) - stop_words
        heading_tokens = set(re.findall(r"[a-z0-9]+", normalized_heading)) - stop_words
        shared_tokens = slug_tokens & heading_tokens
        if not slug_tokens or len(shared_tokens) < min(2, len(slug_tokens)):
            continue
        coverage = len(shared_tokens) / len(slug_tokens)
        heading_candidates_by_native_id.setdefault(native_id, []).append(
            (coverage, len(shared_tokens), nearest_distance, heading_title)
        )
        heading_positions_by_native_id.setdefault(native_id, []).append(
            (heading.start(), heading.end(), nearest_distance, heading_title)
        )
    heading_title_by_native_id = {
        native_id: max(
            candidates,
            key=lambda item: (item[0], item[1], -item[2], len(item[3])),
        )[3]
        for native_id, candidates in heading_candidates_by_native_id.items()
    }

    # Coinbase card containers are more reliable than global nearest-link distance
    # when a date is physically inside one card but closer to the next card's image link.
    if source.get("source_id") == "coinbase-blog":
        container_owners: dict[tuple[int, int, datetime], tuple[int, str]] = {}
        ambiguous_container_dates: set[tuple[int, int, datetime]] = set()
        container_ranges = _semantic_container_ranges(text)
        container_stats = {
            "ranges": len(container_ranges), "single_route": 0,
            "single_date": 0, "multiple_dates": 0,
        }
        for container_start, container_end in container_ranges:
            container_links = [
                (index, safe)
                for index, safe in safe_by_index.items()
                if safe is not None
                and container_start <= matches[index].start()
                and matches[index].end() <= container_end
            ]
            route_ids = {safe[1] for _, safe in container_links}
            if len(route_ids) != 1:
                continue
            container_stats["single_route"] += 1
            container_dates = [
                date for date in unique_dates.values()
                if container_start <= date[0] and date[1] <= container_end
            ]
            if not container_dates:
                continue
            if len({date[2] for date in container_dates}) != 1:
                container_stats["multiple_dates"] += 1
                continue
            container_stats["single_date"] += 1
            native_id = next(iter(route_ids))
            link_matches = [matches[index] for index, _ in container_links]
            def date_distance(date: tuple[int, int, datetime], link: re.Match[str]) -> int:
                if date[1] <= link.start():
                    return link.start() - date[1]
                if date[0] >= link.end():
                    return date[0] - link.end()
                return 0
            chosen_date = min(
                container_dates,
                key=lambda date: min(date_distance(date, link) for link in link_matches),
            )
            span_length = container_end - container_start
            previous = container_owners.get(chosen_date)
            if previous is None or span_length < previous[0]:
                container_owners[chosen_date] = (span_length, native_id)
                ambiguous_container_dates.discard(chosen_date)
            elif span_length == previous[0] and native_id != previous[1]:
                ambiguous_container_dates.add(chosen_date)

        container_stats["owned_date_spans"] = len(container_owners)
        container_stats["ambiguous_date_spans"] = len(ambiguous_container_dates)
        container_owner_by_date = {
            date_span: owner[1]
            for date_span, owner in container_owners.items()
            if date_span not in ambiguous_container_dates
        }
        if diagnostics is not None:
            diagnostics["coinbase_container_diagnostics"] = container_stats
        if container_owners:
            rebuilt_dates: dict[str, list[tuple[int, int, datetime]]] = {}
            rebuilt_spans: set[tuple[int, int, datetime]] = set()
            for date_span in unique_dates.values():
                if date_span in ambiguous_container_dates:
                    assigned_id = link_assignment_by_date.get(date_span)
                elif date_span in container_owners:
                    assigned_id = container_owners[date_span][1]
                else:
                    assigned_id = link_assignment_by_date.get(date_span)
                if assigned_id is not None:
                    rebuilt_dates.setdefault(assigned_id, []).append(date_span)
                    rebuilt_spans.add(date_span)
            dates_by_native_id = rebuilt_dates
            assigned_date_spans = rebuilt_spans

    # Coinbase's image-only index can place a date near a slug-matched heading
    # but too far from its link. Only use this fallback for dates not already
    # assigned to any official link, so it never steals another card's date.
    if source.get("source_id") == "coinbase-blog":
        for date_start, date_end, parsed in unique_dates.values():
            date_span = (date_start, date_end, parsed)
            if date_span in assigned_date_spans:
                continue
            link_gap_by_id: dict[str, int] = {}
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
                prior_gap = link_gap_by_id.get(safe_link[1])
                if prior_gap is None or distance < prior_gap:
                    link_gap_by_id[safe_link[1]] = distance
            heading_distances: list[tuple[int, str]] = []
            for native_id, headings in heading_positions_by_native_id.items():
                if dates_by_native_id.get(native_id):
                    continue
                route_link_gap = link_gap_by_id.get(native_id)
                if route_link_gap is None:
                    continue
                for heading_start, heading_end, _, _ in headings:
                    if date_end <= heading_start:
                        distance = heading_start - date_end
                    elif date_start >= heading_end:
                        distance = date_start - heading_end
                    else:
                        distance = 0
                    if distance <= 2500 and distance + 200 < route_link_gap:
                        heading_distances.append((distance, native_id))
            if not heading_distances:
                continue
            nearest_heading_distance = min(value[0] for value in heading_distances)
            nearest_heading_ids = {native_id for distance, native_id in heading_distances if distance == nearest_heading_distance}
            if len(nearest_heading_ids) == 1:
                native_id = next(iter(nearest_heading_ids))
                dates_by_native_id.setdefault(native_id, []).append((date_start, date_end, parsed))
                assigned_date_spans.add(date_span)

    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    incomplete_ids: set[str] = set()
    incomplete_reasons: dict[str, set[str]] = {}
    conflicting_ids: set[str] = set()
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
            block_headline_ranges = [
                (headline.start(), headline.end())
                for headline in re.finditer(
                    r"<h[1-6]\b[^>]*>.*?</h[1-6]\s*>",
                    block_text, re.IGNORECASE | re.DOTALL,
                )
            ]
            date_matches = [
                item for item in _DATE_RE.finditer(block_text)
                if not any(
                    heading_start <= item.start() and item.end() <= heading_end
                    for heading_start, heading_end in block_headline_ranges
                )
            ]
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
        if not title or normalized_title in generic_titles:
            opening_tag = match.group(0).split(">", 1)[0] + ">"
            label_match = re.search(
                r"<a\b[^>]*?\b(?:aria-label|title)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')",
                opening_tag, re.IGNORECASE | re.DOTALL,
            )
            label_title = _clean(label_match.group("double") or label_match.group("single") or "") if label_match else ""
            if label_title:
                article_path = urlsplit(safe[0]).path.rstrip("/")
                slug = re.sub(r"\.(?:html?|aspx?)$", "", article_path.rsplit("/", 1)[-1].casefold())
                slug_tokens = set(re.findall(r"[a-z0-9]+", slug)) - stop_words
                label_tokens = set(re.findall(r"[a-z0-9]+", label_title.casefold())) - stop_words
                if slug_tokens and len(slug_tokens & label_tokens) >= min(2, len(slug_tokens)):
                    title = label_title
                    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        if not title or normalized_title in generic_titles:
            alt_match = re.search(
                r"<img\b[^>]*?\balt\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)')[^>]*>",
                match.group("title"), re.IGNORECASE | re.DOTALL,
            )
            alt_title = _clean(alt_match.group("double") or alt_match.group("single") or "") if alt_match else ""
            if alt_title:
                article_path = urlsplit(safe[0]).path.rstrip("/")
                slug = re.sub(r"\.(?:html?|aspx?)$", "", article_path.rsplit("/", 1)[-1].casefold())
                slug_tokens = set(re.findall(r"[a-z0-9]+", slug)) - stop_words
                alt_tokens = set(re.findall(r"[a-z0-9]+", alt_title.casefold())) - stop_words
                if slug_tokens and len(slug_tokens & alt_tokens) >= min(2, len(slug_tokens)):
                    title = alt_title
                    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        if not title or normalized_title in generic_titles:
            assigned_title = heading_title_by_native_id.get(safe[1])
            if assigned_title:
                title = assigned_title
                normalized_title = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
        missing_title = not title or normalized_title in generic_titles
        missing_date = published is None
        if missing_title or missing_date:
            incomplete_ids.add(safe[1])
            reasons = incomplete_reasons.setdefault(safe[1], set())
            if missing_title:
                reasons.add("title")
            if missing_date:
                reasons.add("date")
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
            conflicting_ids.add(native_id)
            # Preserve unresolved conflicts for the collector's fail-closed
            # duplicate identity handling; never choose arbitrarily.
            rows.extend(variants)

    valid_ids = set(grouped_rows)
    structured_ids = set(structured_by_id)
    unresolved_incomplete_ids = incomplete_ids - valid_ids - structured_ids
    if lookback_cutoff is not None:
        unresolved_incomplete_ids = {
            native_id for native_id in unresolved_incomplete_ids
            if not _route_month_is_before_cutoff(native_id, lookback_cutoff)
        }
    if diagnostics is not None:
        diagnostics["incomplete_native_ids"] = sorted(unresolved_incomplete_ids)
        diagnostics["incomplete_reasons"] = {
            native_id: sorted(incomplete_reasons.get(native_id, set()))
            for native_id in sorted(unresolved_incomplete_ids)
        }
        diagnostics["incomplete_date_proximity"] = {
            native_id: {
                "nearest_gap": nearest_date_gap_by_native_id.get(native_id),
                "uniquely_nearest_date_tokens": unique_nearest_date_tokens_by_native_id.get(native_id, 0),
                "tied_nearest_date_tokens": tied_nearest_date_tokens_by_native_id.get(native_id, 0),
            }
            for native_id in sorted(unresolved_incomplete_ids)
            if "date" in incomplete_reasons.get(native_id, set())
        }
        # Show the owner of the nearest date token for unresolved routes. This is
        # diagnostic only: it never changes the parser's conservative assignments.
        assignment_context: dict[str, dict[str, object]] = {}
        for native_id in sorted(unresolved_incomplete_ids):
            if "date" not in incomplete_reasons.get(native_id, set()):
                continue
            route_links = [
                matches[index] for index, safe in safe_by_index.items()
                if safe is not None and safe[1] == native_id
            ]
            if not route_links or not unique_dates:
                continue
            date_gaps: list[tuple[int, tuple[int, int, datetime]]] = []
            for date_span in unique_dates.values():
                date_start, date_end, _ = date_span
                gap = min(
                    (link.start() - date_end if date_end <= link.start()
                     else date_start - link.end() if date_start >= link.end()
                     else 0)
                    for link in route_links
                )
                date_gaps.append((gap, date_span))
            nearest_gap = min(gap for gap, _ in date_gaps)
            nearest_spans = [span for gap, span in date_gaps if gap == nearest_gap]
            owners = {link_assignment_by_date.get(span) for span in nearest_spans}
            card_owners = {container_owner_by_date.get(span) for span in nearest_spans}
            heading_ranges = heading_positions_by_native_id.get(native_id, [])
            heading_gaps = [
                min(
                    (heading_start - span[1] if span[1] <= heading_start
                     else span[0] - heading_end if span[0] >= heading_end
                     else 0)
                    for heading_start, heading_end, _, _ in heading_ranges
                )
                for span in nearest_spans
            ] if heading_ranges else []
            assignment_context[native_id] = {
                "nearest_gap": nearest_gap,
                "global_date_owners": sorted(owner for owner in owners if owner is not None),
                "card_date_owners": sorted(owner for owner in card_owners if owner is not None),
                "nearest_heading_gap": min(heading_gaps) if heading_gaps else None,
            }
        diagnostics["incomplete_date_assignment_context"] = assignment_context
        diagnostics["conflicting_native_ids"] = sorted(conflicting_ids)
    if unresolved_incomplete_ids:
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
    heading_matches = list(re.finditer(
        r"<h[1-6]\b[^>]*>(?P<title>.*?)</h[1-6]\s*>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    heading_titles_with_text = sum(
        bool(_clean(re.sub(r"<[^>]+>", " ", match.group("title"))))
        for match in heading_matches
    )
    article_blocks = list(re.finditer(r"<article\b[^>]*>.*?</article\s*>", text, re.IGNORECASE | re.DOTALL))
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
        r"<time\b[^>]*\bdatetime=['\"](?P<date>[^'\"]+)['\"][^>]*>",
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
    json_key_names = (
        "datePublished", "dateCreated", "publishedAt", "published_at", "createdAt",
        "created_at", "publicationDate", "publication_date", "published", "date",
        "title", "headline", "name", "slug", "url", "href", "link", "canonicalUrl",
        "canonical_url", "article",
    )
    json_key_counts = {key: 0 for key in json_key_names}
    script_iso_date_strings = 0
    iso_date_pattern = re.compile(r"^20\d{2}-\d{2}-\d{2}(?:T[^\s]+)?$")
    inspected_json_scripts = 0
    for match in script_blocks:
        attrs = match.group("attrs")
        is_json_data = bool(re.search(
            r"\btype\s*=\s*(?:\"application/json\"|'application/json'|application/json(?:\s|/|>))|\bid\s*=\s*(?:\"__NEXT_DATA__\"|'__NEXT_DATA__')",
            attrs, re.IGNORECASE,
        ))
        if not is_json_data or inspected_json_scripts >= 3 or len(match.group("body")) > 300000:
            continue
        inspected_json_scripts += 1
        try:
            payload = json.loads(match.group("body").strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        stack = [payload]
        visited = 0
        while stack and visited < 30000:
            item = stack.pop()
            visited += 1
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in json_key_counts:
                        json_key_counts[key] += 1
                    stack.append(child)
            elif isinstance(item, list):
                stack.extend(item[:10000])
            elif isinstance(item, str) and iso_date_pattern.fullmatch(item.strip()):
                script_iso_date_strings += 1
    json_key_counts = {key: count for key, count in json_key_counts.items() if count}
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
        f"official_links_without_text={max(0, official_links - official_links_with_text)} "
        f"heading_tags={len(heading_matches)} heading_titles_with_text={heading_titles_with_text} "
        f"official_links_in_article_blocks={official_links_in_article_blocks} article_blocks={len(article_blocks)} "
        f"median_nearest_date_distance={median_nearest_distance} "
        f"script_tags={len(script_blocks)} json_data_scripts={json_data_scripts} "
        f"script_date_tokens={script_date_tokens} script_iso_date_strings={script_iso_date_strings} "
        f"json_data_key_counts={json_key_counts} script_bytes={script_bytes}"
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


    def _detail_fallback(
        self, content: bytes, errors: list[str], *, exclude_ids: set[str] | None = None,
        preferred_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
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
        preferred = preferred_ids or set()
        pending = [
            (native_id, url) for native_id, url in links.items()
            if native_id not in excluded
        ]
        pending.sort(key=lambda item: item[0] not in preferred)
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

        parse_diagnostics: dict[str, Any] = {}
        try:
            rows, complete = _parse_page(
                self.source, response.content, parse_diagnostics, lookback_cutoff=cutoff
            )
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

        # Public indexes occasionally place an event or unrelated future date beside a release card.
        # Never publish a row whose purported publication date is implausibly ahead of collection time.
        # A 24-hour tolerance accommodates timezone/date-only representation differences; unresolved
        # routes remain partial and are retried rather than having their validators advanced.
        future_cutoff = fetched_at.astimezone(timezone.utc) + timedelta(days=1)
        future_ids = {
            row["native_id"] for row in rows
            if row["date"].astimezone(timezone.utc) > future_cutoff
        }
        if future_ids:
            rows = [row for row in rows if row["native_id"] not in future_ids]
            complete = False
            parse_diagnostics["future_dated_native_ids"] = sorted(future_ids)
            incomplete_ids = set(parse_diagnostics.get("incomplete_native_ids", [])) | future_ids
            parse_diagnostics["incomplete_native_ids"] = sorted(incomplete_ids)
            reasons_by_id = parse_diagnostics.setdefault("incomplete_reasons", {})
            for native_id in future_ids:
                reasons_by_id[native_id] = sorted(set(reasons_by_id.get(native_id, [])) | {"future_date"})
            errors.append(
                f"{self.source['name']}: rejected {len(future_ids)} future-dated official article route(s) "
                "more than 24 hours ahead of collection time"
            )

        if not complete:
            if parse_diagnostics.get("incomplete_native_ids") or parse_diagnostics.get("conflicting_native_ids"):
                print(
                    f"Source index diagnostics: {self.source['source_id']} "
                    f"{_index_diagnostics(self.source, response.content, response)}"
                    + (
                        f" coinbase_container_diagnostics={parse_diagnostics.get('coinbase_container_diagnostics', {})}"
                        if self.source.get("source_id") == "coinbase-blog" else ""
                    )
                )
            unparsed_ids = set(parse_diagnostics.get("incomplete_native_ids", []))
            incomplete_reasons = parse_diagnostics.get("incomplete_reasons", {})
            conflicting_ids = set(parse_diagnostics.get("conflicting_native_ids", []))
            resolved_conflicts: set[str] = set()
            recovered_unparsed_ids: set[str] = set()
            if not used_detail_fallback and self.source.get("detail_fallback_limit"):
                known_ids = {
                    row["native_id"] for row in rows
                    if row["native_id"] not in conflicting_ids
                }
                recovered = self._detail_fallback(
                    response.content, errors, exclude_ids=known_ids,
                    preferred_ids=conflicting_ids | future_ids,
                )
                if recovered:
                    recovered_ids = {row["native_id"] for row in recovered}
                    recovered_unparsed_ids = unparsed_ids & recovered_ids
                    resolved_conflicts = conflicting_ids & recovered_ids
                    if resolved_conflicts:
                        rows = [row for row in rows if row["native_id"] not in resolved_conflicts]
                        rows.extend(row for row in recovered if row["native_id"] in resolved_conflicts)
                    existing_ids = {row["native_id"] for row in rows}
                    additional_rows = [row for row in recovered if row["native_id"] not in existing_ids]
                    rows.extend(additional_rows)
                    used_detail_fallback = True
                    if resolved_conflicts:
                        print(
                            f"Source recovery: {self.source['source_id']} resolved "
                            f"{len(resolved_conflicts)} conflicting duplicate article(s) from official detail pages"
                        )
                    if additional_rows:
                        unresolved_after_fallback = unparsed_ids - recovered_unparsed_ids
                        unresolved_conflicts_after_fallback = conflicting_ids - resolved_conflicts
                        if unresolved_after_fallback or unresolved_conflicts_after_fallback:
                            errors.append(
                                f"{self.source['name']}: bounded detail fallback recovered "
                                f"{len(additional_rows)} additional official article(s); source remains partial"
                            )
                        else:
                            print(
                                f"Source recovery: {self.source['source_id']} recovered "
                                f"{len(additional_rows)} incomplete official article(s); "
                                "all known index gaps resolved"
                            )
            unresolved_conflicts = conflicting_ids - resolved_conflicts
            unresolved_unparsed_ids = unparsed_ids - recovered_unparsed_ids
            if unresolved_unparsed_ids or unresolved_conflicts:
                errors.append(
                    f"{self.source['name']}: malformed article on official index "
                    f"(index_unparsed_routes={len(unparsed_ids)} sample={sorted(unparsed_ids)[:3]}, "
                    f"recovered_unparsed_routes={len(recovered_unparsed_ids)}, "
                    f"unresolved_after_fallback={len(unresolved_unparsed_ids)} "
                    f"sample={sorted(unresolved_unparsed_ids)[:3]}, "
                    f"incomplete_reasons={ {key: incomplete_reasons.get(key, []) for key in sorted(unparsed_ids)[:3]} }, "
                    f"date_proximity={ {key: parse_diagnostics.get('incomplete_date_proximity', {}).get(key) for key in sorted(unparsed_ids)[:3] if key in parse_diagnostics.get('incomplete_date_proximity', {})} }, "
                    f"date_assignment_context={ {key: parse_diagnostics.get('incomplete_date_assignment_context', {}).get(key) for key in sorted(unparsed_ids)[:3] if key in parse_diagnostics.get('incomplete_date_assignment_context', {})} }, "
                    f"conflicting_duplicate_routes={len(unresolved_conflicts)} sample={sorted(unresolved_conflicts)[:3]})"
                )
            if not unresolved_unparsed_ids and not unresolved_conflicts and not errors:
                updates[index_request_id] = {
                    key: response.headers[key]
                    for key in ("etag", "last_modified") if response.headers.get(key)
                }
        else:
            updates[index_request_id] = {
                key: response.headers[key]
                for key in ("etag", "last_modified") if response.headers.get(key)
            }

        # Detail fallback is also untrusted until its date passes the same future-date guard.
        remaining_future_ids = {
            row["native_id"] for row in rows
            if row["date"].astimezone(timezone.utc) > future_cutoff
        }
        if remaining_future_ids:
            rows = [row for row in rows if row["native_id"] not in remaining_future_ids]
            if not future_ids:
                errors.append(
                    f"{self.source['name']}: rejected {len(remaining_future_ids)} future-dated official detail route(s) "
                    "more than 24 hours ahead of collection time"
                )

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
