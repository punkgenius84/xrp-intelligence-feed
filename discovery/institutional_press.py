from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    return " ".join(value.split()) if isinstance(value, str) else ""


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

    scripts = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
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
            if not isinstance(types, list) or not any(
                isinstance(kind, str) and kind.casefold() in {"article", "newsarticle", "pressrelease"}
                for kind in types
            ):
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
        r"<a[^>]+href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    structured_rows = _json_ld_articles(source, text)
    if not matches and not structured_rows:
        raise ValueError("institutional response is missing links and structured article metadata")

    grouped_rows: dict[str, list[dict[str, Any]]] = {}
    incomplete_ids: set[str] = set()
    complete = True
    generic_titles = {
        "read more", "read more about", "learn more", "learn more about",
        "view more", "view article", "view release", "read full story",
        "continue reading", "more",
    }
    for index, match in enumerate(matches):
        safe = _official_article(source, match.group("href"))
        if safe is None:
            continue
        # Prefer a date immediately before the current article link. A date after
        # a link is used only when the next anchor is not a different allowlisted
        # article, so the next card's date cannot be borrowed by an undated item.
        previous = matches[index - 1].end() if index else max(0, match.start() - 1800)
        raw_window = text[previous:match.start()]
        date_matches = list(_DATE_RE.finditer(raw_window))
        if date_matches:
            nearest = date_matches[-1]
            published = _parse_date(nearest.group(0), source.get("numeric_date_order", "mdy"))
        else:
            next_is_different_article = False
            if index + 1 < len(matches):
                next_safe = _official_article(source, matches[index + 1].group("href"))
                next_is_different_article = next_safe is not None and next_safe[1] != safe[1]
            if not next_is_different_article:
                following = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 1800)
                trailing_dates = list(_DATE_RE.finditer(text[match.end():following]))
                published = (
                    _parse_date(trailing_dates[0].group(0), source.get("numeric_date_order", "mdy"))
                    if trailing_dates else None
                )
            else:
                published = None
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


    def _detail_fallback(self, content: bytes, errors: list[str]) -> list[dict[str, Any]]:
        """Fetch a tightly bounded number of dated official article details from a sparse index."""
        limit = self.source.get("detail_fallback_limit", 0)
        if not limit:
            return []
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            errors.append(f"{self.source['name']}: index is not valid UTF-8 for detail fallback")
            return []

        matches = re.finditer(
            r"<a[^>]+href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
            text, re.IGNORECASE | re.DOTALL,
        )
        links: dict[str, str] = {}
        for match in matches:
            safe = _official_article(self.source, match.group("href"))
            if safe is not None:
                links.setdefault(safe[1], safe[0])
        rows: list[dict[str, Any]] = []
        for native_id, url in list(links.items())[:limit]:
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
        validators = JsonDiscoveryState.request_validators(state, self.source["source_id"], "index")
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
            updates["index"] = {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }
            return DiscoveryResult(self.source["source_id"], METHOD, "empty",
                                   fetched_at=fetched_at, state_updates=updates)

        if response.status_code != 200:
            errors.append(f"{self.source['name']}: unexpected HTTP status {response.status_code}")
            return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                   fetched_at=fetched_at, errors=errors)

        try:
            rows, complete = _parse_page(self.source, response.content)
        except ValueError as exc:
            if str(exc) == "institutional response has no dated official articles" and self.source.get("detail_fallback_limit"):
                rows = self._detail_fallback(response.content, errors)
                if rows:
                    complete = False
                    used_detail_fallback = True
                    errors.append(
                        f"{self.source['name']}: index omits dated article metadata; "
                        "bounded detail fallback used, so source remains partial"
                    )
                else:
                    errors.append(f"{self.source['name']}: malformed HTML ({exc})")
                    return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                           fetched_at=fetched_at, errors=errors)
            else:
                errors.append(f"{self.source['name']}: malformed HTML ({exc})")
                return DiscoveryResult(self.source["source_id"], METHOD, "failed",
                                       fetched_at=fetched_at, errors=errors)

        if not complete:
            errors.append(f"{self.source['name']}: malformed article on official index")
        else:
            updates["index"] = {
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
