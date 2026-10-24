from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import feedparser

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


SOURCE_ID = "canada-gazette-part-i"
METHOD = "canada_gazette_rss"
FEED_URL = "https://gazette.gc.ca/rss/p1-eng.xml"
HOSTS = {"gazette.gc.ca", "www.gazette.gc.ca"}
_ISSUE_PATH = re.compile(r"/rp-pr/p1/(20\d{2})/(\d{4}-\d{2}-\d{2})/html/index-eng\.html")
_ARTICLE_PATH = re.compile(r"/rp-pr/p1/20\d{2}/\d{4}-\d{2}-\d{2}/html/reg\d+-eng\.html")
MAX_ISSUES = 3
MAX_ARTICLES = 100


class _IssueIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.heading_text = ""
        self.heading_tag = ""
        self.anchor: dict[str, str] | None = None
        self.rows: list[tuple[str, str, str]] = []
        self.visible_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"h2", "h3", "h4", "h5"}:
            self.heading_tag = tag
            self.heading_text = ""
        if tag == "a" and self.anchor is None:
            href = next((value or "" for key, value in attrs if key.lower() == "href"), "")
            self.anchor = {"href": href, "text": ""}

    def handle_data(self, data: str) -> None:
        self.visible_text += data + " "
        if self.heading_tag:
            self.heading_text += data
        if self.anchor is not None:
            self.anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == self.heading_tag and self.heading_tag:
            heading = " ".join(self.heading_text.split())
            if heading:
                self.headings = self.headings[: max(0, {"h2": 1, "h3": 2, "h4": 3, "h5": 4}[tag] - 1)]
                self.headings.append(heading)
            self.heading_tag = ""
            self.heading_text = ""
        if tag == "a" and self.anchor is not None:
            href = self.anchor["href"].strip()
            title = " ".join(self.anchor["text"].split())
            if href and title:
                self.rows.append((href, title, " — ".join(self.headings[-3:])))
            self.anchor = None


def validate_canada_gazette_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"Canada Gazette source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != SOURCE_ID or source["discovery_method"] != METHOD:
        raise ValueError("source identity or discovery method is not the official Canada Gazette Part I contract")
    if source["source_url"] != FEED_URL:
        raise ValueError("source_url must be the official Canada Gazette Part I RSS feed")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("Canada Gazette authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= MAX_ARTICLES:
        raise ValueError(f"max_items must be an integer from 1 to {MAX_ARTICLES}")
    return source


def _safe_url(value: object, *, article: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    pattern = _ARTICLE_PATH if article else _ISSUE_PATH
    if (
        parts.scheme.casefold() != "https" or parts.hostname is None
        or parts.hostname.casefold() not in HOSTS or port not in (None, 443)
        or parts.username is not None or parts.password is not None
        or not pattern.fullmatch(parts.path)
    ):
        return None
    return f"https://gazette.gc.ca{parts.path}"


def _published(entry: Any) -> datetime:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if value is None or len(value) < 6:
        raise ValueError("missing official issue publication date")
    return datetime(*value[:6], tzinfo=timezone.utc)


def _issue_date(issue_url: str) -> datetime:
    match = _ISSUE_PATH.fullmatch(urlsplit(issue_url).path)
    if match is None:
        raise ValueError("invalid Canada Gazette issue index route")
    return datetime.strptime(match.group(2), "%Y-%m-%d").replace(tzinfo=timezone.utc)


class CanadaGazetteRSSDiscovery:
    source_id = SOURCE_ID
    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_canada_gazette_source(source)
        self.http = http or BoundedHttpClient(
            user_agent="XRPIntelligenceFeed/0.4 (+official Canada Gazette RSS reader)"
        )
        self.now = now

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        fetched_at = fetched_at.astimezone(timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(SOURCE_ID, METHOD, "not_configured", fetched_at=fetched_at)
        state = source_state if isinstance(source_state, dict) else {}
        validators = JsonDiscoveryState.request_validators(state, SOURCE_ID, "rss")
        try:
            response = self.http.get(
                FEED_URL,
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/rss+xml", "application/xml", "text/xml", "application/atom+xml"),
            )
        except DiscoveryHttpError as exc:
            return DiscoveryResult(
                SOURCE_ID, METHOD, "failed", fetched_at=fetched_at,
                errors=["Canada Gazette Part I RSS: " + exc.kind + (f" (HTTP {exc.status_code})" if exc.status_code else "")],
            )
        if response.status_code == 304:
            kept = {key: response.headers.get(key, validators.get(key, "")) for key in ("etag", "last_modified")
                    if response.headers.get(key, validators.get(key, ""))}
            return DiscoveryResult(SOURCE_ID, METHOD, "not_modified", fetched_at=fetched_at,
                                   state_updates={"rss": kept})
        if response.status_code != 200:
            return DiscoveryResult(SOURCE_ID, METHOD, "failed", fetched_at=fetched_at,
                                   errors=[f"Canada Gazette Part I RSS: unexpected HTTP status {response.status_code}"])
        parsed = feedparser.parse(response.content)
        if getattr(parsed, "bozo", False) or getattr(parsed, "version", "") not in {"rss20", "rss10", "atom10", "atom"}:
            return DiscoveryResult(SOURCE_ID, METHOD, "failed", fetched_at=fetched_at,
                                   errors=["Canada Gazette Part I RSS: malformed or unsupported feed"])
        entries = list(getattr(parsed, "entries", []))[:MAX_ISSUES]
        errors: list[str] = []
        candidates: dict[str, DiscoveryCandidate] = {}
        for entry in entries:
            try:
                issue_url = _safe_url(entry.get("link"))
                if issue_url is None:
                    raise ValueError("missing or non-official issue index URL")
                feed_published = _published(entry)
                route_date = _issue_date(issue_url)
                # Canada Gazette regular editions are published online at 2 p.m.
                # Eastern the day before their printed issue date, so the RSS
                # timestamp may legitimately be one calendar day earlier.
                if abs((route_date.date() - feed_published.date()).days) > 1:
                    raise ValueError("issue route date is inconsistent with RSS publication date")
                index_response = self.http.get(
                    issue_url,
                    expected_content_types=("text/html", "application/xhtml+xml"),
                )
                if index_response.status_code != 200:
                    raise ValueError(f"issue index returned HTTP {index_response.status_code}")
                parser = _IssueIndexParser()
                parser.feed(index_response.content.decode("utf-8-sig"))
                visible = " ".join(parser.visible_text.split())
                if route_date.strftime("%B %d, %Y").replace(" 0", " ") not in visible:
                    raise ValueError("official issue date is missing from the issue index")
                issue_published = route_date
                for href, title, context in parser.rows:
                    url = _safe_url(urljoin(issue_url, href), article=True)
                    if url is None:
                        continue
                    path = urlsplit(url).path
                    native_id = path.casefold()
                    if native_id in candidates:
                        continue
                    summary = f"Canada Gazette Part I; {context}" if context else "Canada Gazette Part I official proposed regulation or notice."
                    candidate = DiscoveryCandidate(
                        title=title,
                        url=url,
                        source=self.source["name"],
                        published_at=issue_published,
                        summary=summary,
                        source_type="discovery",
                        source_id=SOURCE_ID,
                        authority_tier=1,
                        category=self.source["category"],
                        source_native_id=native_id,
                        candidate_id=f"{SOURCE_ID}:{native_id}",
                        discovery_method=METHOD,
                        source_url=FEED_URL,
                        document_type="Canada Gazette Part I notice or proposed regulation",
                        primary_url=url,
                        provenance=[FEED_URL, issue_url, url],
                        collected_at=fetched_at,
                        first_seen_at=fetched_at,
                        last_seen_at=fetched_at,
                    )
                    candidates[native_id] = candidate
                    if len(candidates) >= self.source["max_items"]:
                        break
                if len(candidates) >= self.source["max_items"]:
                    break
            except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as exc:
                errors.append(f"Canada Gazette Part I: malformed issue ({exc})")
        updates = {}
        if not errors:
            updates["rss"] = {key: response.headers[key] for key in ("etag", "last_modified") if response.headers.get(key)}
        ordered = sorted(candidates.values(), key=lambda item: (item.published_at, item.source_native_id), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        return DiscoveryResult(SOURCE_ID, METHOD, status, candidates=ordered, fetched_at=fetched_at,
                               errors=errors, state_updates=updates)
