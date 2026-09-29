from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


XRPL_SOURCE_ID = "xrpl-community-blog"
XRPL_METHOD = "xrpl_blog_html"
XRPL_URL = "https://xrpl.org/blog"
XRPL_HOST = "xrpl.org"
XRPL_NAME = "XRP Ledger Community"
_MAX_PAGES_HARD = 6

_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
    r"Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)
_LINK_RE = re.compile(
    r'<a[^>]+href=["\'](?P<href>/blog/\d{4}/[^"\']+)["\'][^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def validate_xrpl_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items", "max_pages",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"XRPL source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != XRPL_SOURCE_ID:
        raise ValueError(f"source_id must be {XRPL_SOURCE_ID!r}")
    if source["discovery_method"] != XRPL_METHOD:
        raise ValueError(f"discovery_method must be {XRPL_METHOD!r}")
    if source["source_url"] != XRPL_URL:
        raise ValueError("source_url must be the fixed official XRPL blog endpoint")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("XRPL authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    if type(source["max_pages"]) is not int or not 1 <= source["max_pages"] <= _MAX_PAGES_HARD:
        raise ValueError(f"max_pages must be an integer from 1 to {_MAX_PAGES_HARD}")
    return source


def _official_url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() != XRPL_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/blog/")
    ):
        return None
    match = re.fullmatch(r"/blog/(\d{4})/([^/?#]+)/?", parts.path, re.IGNORECASE)
    if not match:
        return None
    slug = f"{match.group(1)}/{match.group(2).casefold()}"
    return f"https://{XRPL_HOST}/blog/{slug}/", slug


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unescape(_TAG_RE.sub(" ", value)).split())


def _parse_nearest_date(text: str, position: int) -> datetime | None:
    candidates = list(_DATE_RE.finditer(text))
    if not candidates:
        return None
    nearest = min(candidates, key=lambda match: abs(match.start() - position))
    return _parse_date(nearest.group(0))


def _parse_date(value: str) -> datetime | None:
    match = _DATE_RE.search(value)
    if not match:
        return None
    raw = re.sub(r"\s+", " ", match.group(0).replace(",", "")).strip()
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_page(content: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("XRPL Blog response is not valid UTF-8") from exc
    matches = list(_LINK_RE.finditer(text))
    if not matches:
        raise ValueError("XRPL Blog response is missing blog article links")
    rows: list[dict[str, Any]] = []
    complete = True
    for index, match in enumerate(matches):
        try:
            href = match.group("href").strip()
            safe = _official_url(
                href if href.lower().startswith(("https://", "http://")) else f"https://{XRPL_HOST}{href}"
            )
            if safe is None:
                raise ValueError("invalid official XRPL blog URL")
            start = max(0, match.start() - 1200)
            end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 1200)
            window = text[start:end]
            title = _clean(match.group("title"))
            published = _parse_nearest_date(window, match.start() - start)
            if not title:
                raise ValueError("missing XRPL blog title")
            if published is None:
                raise ValueError("missing XRPL blog publication date")
            rows.append({
                "url": safe[0],
                "slug": safe[1],
                "title": title,
                "date": published,
            })
        except (TypeError, ValueError):
            complete = False
    return rows, complete


class XRPLBlogDiscovery:
    source_id = XRPL_SOURCE_ID
    discovery_method = XRPL_METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_xrpl_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _page_url(self, page: int) -> str:
        return XRPL_URL if page == 1 else f"{XRPL_URL}/page/{page}/"

    def _candidate(self, row: dict[str, Any], listing_url: str, fetched_at: datetime) -> DiscoveryCandidate:
        native_id = row["slug"]
        return DiscoveryCandidate(
            title=row["title"],
            url=row["url"],
            source=self.source["name"],
            published_at=row["date"],
            summary=f"XRPL Community Blog: {row['title']}",
            source_type="discovery",
            source_id=self.source_id,
            authority_tier=1,
            category=self.source["category"],
            source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}",
            discovery_method=self.discovery_method,
            source_url=listing_url,
            document_type="XRPL Blog",
            primary_url=row["url"],
            provenance=[XRPL_URL, row["url"]],
            collected_at=fetched_at,
            first_seen_at=fetched_at,
            last_seen_at=fetched_at,
            source_native_metadata={"blog_slug": native_id},
        )

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured",
                                   fetched_at=fetched_at)

        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        state_updates: dict[str, dict[str, str]] = {}
        pages_checked = 0

        for page in range(1, self.source["max_pages"] + 1):
            pages_checked += 1
            url = self._page_url(page)
            request_id = f"page-{page}"
            validators = JsonDiscoveryState.request_validators(
                source_state if isinstance(source_state, dict) else {},
                self.source_id,
                request_id,
            )
            try:
                response = self.http.get(
                    url,
                    etag=validators.get("etag", ""),
                    last_modified=validators.get("last_modified", ""),
                    expected_content_types=("text/html", "application/xhtml+xml"),
                )
            except DiscoveryHttpError as exc:
                errors.append(
                    f"XRPL page {page}: {exc.kind}"
                    + (f" (HTTP {exc.status_code})" if exc.status_code else "")
                )
                break
            if response.status_code == 304:
                state_updates[request_id] = {
                    key: response.headers.get(key, validators.get(key, ""))
                    for key in ("etag", "last_modified")
                    if response.headers.get(key, validators.get(key, ""))
                }
                continue
            if response.status_code != 200:
                errors.append(f"XRPL page {page}: unexpected HTTP status {response.status_code}")
                break
            try:
                rows, complete = _parse_page(response.content)
            except ValueError as exc:
                errors.append(f"XRPL page {page}: malformed HTML ({exc})")
                break
            if not complete:
                errors.append(f"XRPL page {page}: malformed blog article")
            if complete:
                state_updates[request_id] = {
                    key: response.headers[key]
                    for key in ("etag", "last_modified")
                    if response.headers.get(key)
                }
            for row in rows:
                if row["date"] < cutoff:
                    continue
                candidate = self._candidate(row, url, fetched_at)
                existing = candidates.get(candidate.source_native_id)
                if existing is not None:
                    if existing.url != candidate.url or existing.title != candidate.title:
                        errors.append(f"XRPL: conflicting duplicate slug {candidate.source_native_id}")
                    continue
                candidates[candidate.source_native_id] = candidate
            if len(candidates) >= self.source["max_items"]:
                break
            if rows and all(row["date"] < cutoff for row in rows):
                break

        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.published_at or fetched_at, item.source_native_id),
            reverse=True,
        )[:self.source["max_items"]]
        status = (
            "partial" if errors and ordered
            else "failed" if errors
            else "empty" if not ordered
            else "success"
        )
        return DiscoveryResult(
            self.source_id,
            self.discovery_method,
            status,
            candidates=ordered,
            fetched_at=fetched_at,
            errors=errors,
            state_updates=state_updates,
            pagination={"pages_checked": pages_checked},
        )
