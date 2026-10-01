from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


WHITE_HOUSE_SOURCE_ID = "white-house-presidential-actions"
WHITE_HOUSE_METHOD = "white_house_actions_html"
WHITE_HOUSE_URL = "https://www.whitehouse.gov/presidential-actions/"
WHITE_HOUSE_HOST = "www.whitehouse.gov"
WHITE_HOUSE_NAME = "The White House"
_ACTION_PATH = re.compile(r"^/presidential-actions/([^/?#]+)/?$", re.IGNORECASE)
_DATE_TEXT = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
_ACTION_TYPES = (
    "Executive Orders",
    "Presidential Memoranda",
    "Proclamations",
    "Nominations & Appointments",
    "Nominations and Appointments",
)
_MAX_PAGES_HARD = 5


def validate_white_house_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items", "max_pages",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"White House source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != WHITE_HOUSE_SOURCE_ID:
        raise ValueError(f"source_id must be {WHITE_HOUSE_SOURCE_ID!r}")
    if source["discovery_method"] != WHITE_HOUSE_METHOD:
        raise ValueError(f"discovery_method must be {WHITE_HOUSE_METHOD!r}")
    if source["source_url"] != WHITE_HOUSE_URL:
        raise ValueError("source_url must be the fixed official White House Presidential Actions index")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("White House authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 180:
        raise ValueError("lookback_days must be an integer from 1 to 180")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    if type(source["max_pages"]) is not int or not 1 <= source["max_pages"] <= _MAX_PAGES_HARD:
        raise ValueError(f"max_pages must be an integer from 1 to {_MAX_PAGES_HARD}")
    return source


def _official_action_url(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("/"):
        raw = f"https://{WHITE_HOUSE_HOST}{raw}"
    parts = urlsplit(raw)
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() != WHITE_HOUSE_HOST
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
    ):
        return None
    match = _ACTION_PATH.fullmatch(parts.path)
    if not match:
        return None
    slug = match.group(1).casefold()
    if slug in {"page", "search"}:
        return None
    return f"https://{WHITE_HOUSE_HOST}/presidential-actions/{slug}/", slug


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    match = _DATE_TEXT.search(raw)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%B %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class _ActionParser(HTMLParser):
    """Parse only action cards/articles from the official index page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.articles: list[dict[str, Any]] = []
        self._article_depth: int | None = None
        self._div_depth = 0
        self._article: dict[str, Any] | None = None
        self._anchor: dict[str, str] | None = None
        self._time: dict[str, str] | None = None

    def _classes(self, attrs: list[tuple[str, str | None]]) -> set[str]:
        value = next((item or "" for key, item in attrs if key == "class"), "")
        return set(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "article" and self._article is None:
            self._article_depth = self._div_depth
            self._article = {"text": "", "links": [], "date": "", "types": []}
        if tag == "div":
            self._div_depth += 1
        if self._article is not None and tag == "a":
            href = next((value or "" for key, value in attrs if key == "href"), "")
            self._anchor = {"href": href, "text": ""}
        if self._article is not None and tag == "time":
            datetime_value = next((value or "" for key, value in attrs if key == "datetime"), "")
            self._time = {"datetime": datetime_value, "text": ""}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._article is not None:
            self._article["text"] += " " + data + " "
            if self._anchor is not None:
                self._anchor["text"] += data
            if self._time is not None:
                self._time["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None and self._article is not None:
            href = self._anchor["href"]
            safe = _official_action_url(href)
            if safe:
                self._article["links"].append((safe[0], safe[1], _clean(self._anchor["text"])))
            elif href.strip().lower().startswith(("http://", "https://")):
                # External links are expected on public indexes; ignore them.
                pass
            self._anchor = None
        if tag == "time" and self._time is not None and self._article is not None:
            self._article["date"] = self._time["datetime"] or self._time["text"]
            self._time = None
        if tag == "article" and self._article is not None:
            self.articles.append(self._article)
            self._article = None
            self._article_depth = None
        if tag == "div":
            self._div_depth = max(0, self._div_depth - 1)

    def close(self) -> None:
        super().close()
        if self._article is not None or self._anchor is not None or self._time is not None:
            raise ValueError("White House HTML ended inside an incomplete action card")


def _parse_page(content: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("White House response is not valid UTF-8") from exc

    matches = list(re.finditer(
        r"<a[^>]+href=['\"](?P<href>[^'\"]+)['\"][^>]*>(?P<title>.*?)</a>",
        text, re.IGNORECASE | re.DOTALL,
    ))
    if not matches:
        raise ValueError("White House response is missing Presidential Action links")

    parsed: list[dict[str, Any]] = []
    complete = True
    for index, match in enumerate(matches):
        safe = _official_action_url(match.group("href"))
        if safe is None:
            continue
        previous = matches[index - 1].start() if index else max(0, match.start() - 1800)
        following = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 1800)
        window = text[previous:following]
        published = None
        datetime_values = re.findall(r"\bdatetime=[\"']([^\"']+)[\"']", window, re.IGNORECASE)
        for value in datetime_values:
            published = _parse_date(value)
            if published is not None:
                break
        if published is None:
            date_matches = list(_DATE_TEXT.finditer(window))
            if date_matches:
                anchor_position = match.start() - previous
                nearest = min(date_matches, key=lambda item: abs(item.start() - anchor_position))
                published = _parse_date(nearest.group(0))
        title = _clean(re.sub(r"<[^>]+>", " ", match.group("title")))
        if not title or published is None:
            complete = False
            continue
        action_type = next(
            (kind for kind in _ACTION_TYPES if kind.casefold() in _clean(window).casefold()),
            "Presidential Action",
        )
        parsed.append({
            "url": safe[0],
            "native_id": safe[1],
            "title": title,
            "date": published,
            "action_type": action_type,
        })
    if not parsed:
        raise ValueError("White House response has no dated official Presidential Actions")
    return parsed, complete


class WhiteHouseActionsDiscovery:
    source_id = WHITE_HOUSE_SOURCE_ID
    discovery_method = WHITE_HOUSE_METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_white_house_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _page_url(self, page: int) -> str:
        return WHITE_HOUSE_URL if page == 1 else f"{WHITE_HOUSE_URL}page/{page}/"

    def _candidate(
        self, row: dict[str, Any], listing_url: str, fetched_at: datetime,
    ) -> DiscoveryCandidate:
        native_id = row["native_id"]
        return DiscoveryCandidate(
            title=row["title"],
            url=row["url"],
            source=self.source["name"],
            published_at=row["date"],
            summary=f"{WHITE_HOUSE_NAME} {row['action_type']}: {row['title']}",
            source_type="discovery",
            source_id=self.source_id,
            authority_tier=1,
            category=self.source["category"],
            source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}",
            discovery_method=self.discovery_method,
            source_url=listing_url,
            document_type=row["action_type"],
            primary_url=row["url"],
            provenance=[WHITE_HOUSE_URL, row["url"]],
            collected_at=fetched_at,
            first_seen_at=fetched_at,
            last_seen_at=fetched_at,
            source_native_metadata={
                "action_slug": native_id,
                "action_type": row["action_type"],
            },
        )

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(
                self.source_id, self.discovery_method, "not_configured", fetched_at=fetched_at,
            )

        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        state_updates: dict[str, dict[str, str]] = {}
        page_count = 0

        for page in range(1, self.source["max_pages"] + 1):
            page_count += 1
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
                    f"White House page {page}: {exc.kind}"
                    + (f" (HTTP {exc.status_code})" if exc.status_code else "")
                )
                break

            if response.status_code == 304:
                state_updates[request_id] = {
                    key: response.headers.get(key) or response.headers.get(key.replace("_", "-")) or validators.get(key, "")
                    for key in ("etag", "last_modified")
                    if response.headers.get(key, validators.get(key, ""))
                }
                continue
            if response.status_code != 200:
                errors.append(f"White House page {page}: unexpected HTTP status {response.status_code}")
                break

            try:
                rows, complete = _parse_page(response.content)
            except ValueError as exc:
                errors.append(f"White House page {page}: malformed HTML ({exc})")
                break
            if not complete:
                errors.append(f"White House page {page}: malformed action card")
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
                        errors.append(
                            f"White House page {page}: conflicting duplicate action identity {candidate.source_native_id}"
                        )
                    continue
                candidates[candidate.source_native_id] = candidate

            if len(candidates) >= self.source["max_items"]:
                break

            # Once a page is entirely older than the lookback boundary, later pages
            # cannot add relevant actions assuming the official index is newest-first.
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
            pagination={"pages_checked": page_count},
        )
