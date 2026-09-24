from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate


OFAC_SOURCE_ID = "ofac-recent-actions"
OFAC_METHOD = "ofac_recent_actions_html"
OFAC_URL = "https://ofac.treasury.gov/recent-actions"
OFAC_HOST = "ofac.treasury.gov"
OFAC_CATEGORIES = {
    "enforcement-actions", "general-licenses", "miscellaneous",
    "regulations-and-guidance", "sanctions-list-updates",
}
_ACTION_PATH = re.compile(r"^/recent-actions/(\d{8})/?$")
_DATE_TEXT = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
_SHALLOW_PAGE = 0
_MAX_REQUESTS = 3


def validate_ofac_source(source: object) -> dict[str, Any]:
    required = {"source_id", "name", "authority_tier", "category", "discovery_method",
                "source_url", "enabled", "lookback_days"}
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"OFAC source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != OFAC_SOURCE_ID:
        raise ValueError(f"source_id must be {OFAC_SOURCE_ID!r}")
    if source["discovery_method"] != OFAC_METHOD:
        raise ValueError(f"discovery_method must be {OFAC_METHOD!r}")
    if source["source_url"] != OFAC_URL:
        raise ValueError(f"source_url must be the fixed official OFAC endpoint {OFAC_URL}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("OFAC authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 90:
        raise ValueError("lookback_days must be an integer from 1 to 90")
    return source


def _official_action_url(href: str) -> tuple[str, str] | None:
    if not isinstance(href, str) or not href.strip():
        return None
    absolute = urljoin(OFAC_URL, href.strip())
    parts = urlsplit(absolute)
    match = _ACTION_PATH.fullmatch(parts.path)
    if (parts.scheme.lower() != "https" or parts.hostname is None
            or parts.hostname.casefold() != OFAC_HOST or parts.port not in (None, 443)
            or parts.username is not None or parts.password is not None or not match):
        return None
    return f"https://{OFAC_HOST}{parts.path.rstrip('/')}", match.group(1)


class _RecentActionsParser(HTMLParser):
    """Parse only OFAC's Recent Actions view and its search-result rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.view_found = False
        self.view_content_found = False
        self.rows: list[dict[str, Any]] = []
        self._div_depth = 0
        self._view_depth: int | None = None
        self._content_depth: int | None = None
        self._row: dict[str, Any] | None = None
        self._row_depth: int | None = None
        self._date_field_depth: int | None = None
        self._anchor: dict[str, str] | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        value = next((item or "" for key, item in attrs if key == "class"), "")
        return set(value.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            self._div_depth += 1
            classes = self._classes(attrs)
            if {"view-recent-actions-search", "view-id-recent_actions_search"} <= classes:
                self.view_found = True
                self._view_depth = self._div_depth
            if self._view_depth is not None and "view-content" in classes:
                self.view_content_found = True
                self._content_depth = self._div_depth
            if (self._content_depth is not None
                    and {"search-result", "views-row"} <= classes):
                self._row = {"anchors": [], "text": "", "date_text": ""}
                self._row_depth = self._div_depth
            # In the current OFAC listing the date/category block is the
            # compact second field, marked by these presentation classes.
            if (self._row is not None
                    and {"font-sans-2xs", "line-height-sans-3"} <= classes):
                self._date_field_depth = self._div_depth
        elif tag == "a" and self._row is not None:
            href = next((value or "" for key, value in attrs if key == "href"), "")
            self._anchor = {"href": href, "text": ""}

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._row is not None:
            # Preserve element boundaries: date text may immediately follow a
            # title element in the source markup without literal whitespace.
            self._row["text"] += " " + data + " "
            if self._date_field_depth is not None:
                self._row["date_text"] += " " + data + " "
            if self._anchor is not None:
                self._anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None and self._row is not None:
            self._row["anchors"].append(self._anchor)
            self._anchor = None
        if tag == "div":
            if self._row is not None and self._row_depth == self._div_depth:
                self.rows.append(self._row)
                self._row = None
                self._row_depth = None
            if self._content_depth == self._div_depth:
                self._content_depth = None
            if self._date_field_depth == self._div_depth:
                self._date_field_depth = None
            if self._view_depth == self._div_depth:
                self._view_depth = None
            self._div_depth = max(0, self._div_depth - 1)


def _parse_page(content: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1252")
    parser = _RecentActionsParser()
    parser.feed(text)
    parser.close()
    if (parser._div_depth != 0 or parser._view_depth is not None
            or parser._content_depth is not None or parser._row is not None
            or parser._date_field_depth is not None):
        raise ValueError("OFAC Recent Actions HTML ended inside an incomplete listing")
    if not parser.view_found or not parser.view_content_found:
        raise ValueError("OFAC response is missing the Recent Actions listing structure")
    parsed: list[dict[str, Any]] = []
    complete = True
    for index, row in enumerate(parser.rows):
        action = None
        category = None
        for anchor in row["anchors"]:
            safe_action = _official_action_url(anchor["href"])
            if safe_action and action is None:
                action = (safe_action[0], safe_action[1], anchor["text"].strip())
            parts = urlsplit(urljoin(OFAC_URL, anchor["href"]))
            slug = parts.path.rstrip("/").rsplit("/", 1)[-1]
            if (parts.scheme == "https" and parts.hostname == OFAC_HOST
                    and slug in OFAC_CATEGORIES and category is None):
                category = (slug, anchor["text"].strip())
        match = _DATE_TEXT.search(row["date_text"])
        try:
            if not action or not action[2]:
                raise ValueError("missing official action link/title")
            if not category or not category[1]:
                raise ValueError("missing recognized action category")
            if not match:
                raise ValueError("missing action date")
            published = datetime.strptime(match.group(0), "%B %d, %Y").date()
            parsed.append({"url": action[0], "native_id": action[1],
                           "title": " ".join(action[2].split()), "date": published,
                           "category_slug": category[0], "category": " ".join(category[1].split())})
        except (ValueError, TypeError):
            complete = False
    return parsed, complete


class OFACRecentActionsDiscovery:
    source_id = OFAC_SOURCE_ID
    discovery_method = OFAC_METHOD

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_ofac_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _page_url(self, page: int, start: date, end: date) -> str:
        return OFAC_URL + "?" + urlencode({
            "ra-start-date": start.isoformat(), "ra-end-date": end.isoformat(), "page": page,
        })

    def _candidate(self, row: dict[str, Any], listing_url: str, fetched_at: datetime) -> DiscoveryCandidate:
        published = datetime.combine(row["date"], day_time.min, tzinfo=timezone.utc)
        native_id = row["native_id"]
        return DiscoveryCandidate(
            title=row["title"], url=row["url"], source=self.source["name"],
            published_at=published, summary=f"OFAC Recent Actions — {row['category']}",
            source_type="discovery", source_id=self.source_id, authority_tier=1,
            category=self.source["category"], source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}", discovery_method=self.discovery_method,
            source_url=listing_url, document_type=row["category"], primary_url=row["url"],
            provenance=[OFAC_URL, row["url"]], collected_at=fetched_at,
            first_seen_at=fetched_at, last_seen_at=fetched_at,
            source_native_metadata={"action_id": native_id, "action_date": row["date"].isoformat(),
                                   "category": row["category"], "category_slug": row["category_slug"]},
        )

    def collect(self, state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured",
                                   fetched_at=fetched_at)
        today = fetched_at.astimezone(timezone.utc).date()
        start = today - timedelta(days=self.source["lookback_days"] - 1)
        if state is not None and not isinstance(state, dict):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid discovery state; refusing to reset the OFAC frontier"])
        state = state or {}
        all_sources = state.get("sources", {})
        if not isinstance(all_sources, dict):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid discovery sources state; refusing to reset the OFAC frontier"])
        source_state = all_sources.get(self.source_id, {})
        if not isinstance(source_state, dict):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC source state; refusing to reset the frontier"])
        pagination = source_state.get("pagination", {})
        if not isinstance(pagination, dict):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC pagination container; refusing to reset the frontier"])
        has_pagination_state = "ofac_recent_actions" in pagination
        previous = pagination.get("ofac_recent_actions", {})
        if not isinstance(previous, dict):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC pagination state; refusing to reset the frontier"])
        required_state_fields = {"boundary_id", "boundary_date", "deep_page_hint", "lookback_days"}
        if has_pagination_state and not required_state_fields <= previous.keys():
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Incomplete OFAC pagination state; refusing to reset the frontier"])
        if has_pagination_state and set(previous) - required_state_fields - {"frontier_status"}:
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Unexpected OFAC pagination fields; refusing to reset the frontier"])
        boundary_id = previous.get("boundary_id")
        boundary_date_raw = previous.get("boundary_date")
        hint = previous.get("deep_page_hint", 1)
        if (boundary_id is not None and (not isinstance(boundary_id, str)
                                        or not re.fullmatch(r"\d{8}", boundary_id))):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC boundary_id; refusing to reset the frontier"])
        if type(hint) is not int or hint < 1:
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC deep_page_hint; refusing to reset the frontier"])
        stored_lookback = previous.get("lookback_days", self.source["lookback_days"])
        frontier_status = previous.get("frontier_status")
        if (type(stored_lookback) is not int or not 1 <= stored_lookback <= 90
                or (frontier_status is not None
                    and (not isinstance(frontier_status, str)
                         or frontier_status not in {"active", "boundary_expired"}))):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Invalid OFAC pagination metadata; refusing to reset the frontier"])
        boundary_date = None
        if boundary_date_raw is not None:
            try:
                boundary_date = date.fromisoformat(boundary_date_raw)
            except (TypeError, ValueError):
                return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                       errors=["Invalid OFAC boundary_date; refusing to reset the frontier"])
        if (boundary_id is None) != (boundary_date is None):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Incomplete OFAC boundary state; refusing to reset the frontier"])
        if (has_pagination_state and boundary_id is None
                and (hint != 1 or frontier_status == "boundary_expired")):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["Inconsistent empty OFAC frontier; refusing to reset the boundary"])
        if boundary_id is not None and boundary_date != date.fromisoformat(boundary_id):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", fetched_at=fetched_at,
                                   errors=["OFAC boundary ID/date mismatch; refusing to reset the frontier"])

        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        pages: dict[int, list[dict[str, Any]]] = {}
        page_fetches = 0
        failed = False

        def request(page: int) -> bool:
            nonlocal page_fetches, failed
            # This caps adapter-level page fetches. BoundedHttpClient may make
            # its own already-bounded retries/redirect requests underneath.
            if page_fetches >= _MAX_REQUESTS:
                errors.append("OFAC logical page-fetch limit exceeded")
                failed = True
                return False
            url = self._page_url(page, start, today)
            page_fetches += 1
            try:
                response = self.http.get(url, expected_content_types=("text/html", "application/xhtml+xml"))
                rows, complete = _parse_page(response.content)
                for row in rows:
                    candidate = self._candidate(row, url, fetched_at)
                    candidates.setdefault(candidate.source_native_id, candidate)
                pages[page] = rows
                if not complete:
                    errors.append(f"Malformed OFAC action row on page {page}")
                    failed = True
                    return False
                return True
            except DiscoveryHttpError as exc:
                errors.append(f"{exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else ""))
            except (UnicodeError, ValueError, TypeError) as exc:
                errors.append(str(exc) or "Malformed OFAC Recent Actions HTML")
            failed = True
            return False

        if not request(_SHALLOW_PAGE):
            return DiscoveryResult(self.source_id, self.discovery_method, "failed", list(candidates.values()),
                                   fetched_at, errors, pagination={"page_fetches": page_fetches})

        shallow_rows = pages[0]
        if boundary_id is None:
            if shallow_rows:
                oldest = shallow_rows[-1]
                next_progress = {"boundary_id": oldest["native_id"],
                                 "boundary_date": oldest["date"].isoformat(), "deep_page_hint": 1,
                                 "lookback_days": self.source["lookback_days"]}
            else:
                next_progress = {"boundary_id": None, "boundary_date": None, "deep_page_hint": 1,
                                 "lookback_days": self.source["lookback_days"]}
            return DiscoveryResult(self.source_id, self.discovery_method, "success", list(candidates.values()),
                                   fetched_at, pagination={"page_fetches": page_fetches,
                                                           "ofac_recent_actions": next_progress})

        if boundary_date < start:
            progress = dict(previous)
            progress["frontier_status"] = "boundary_expired"
            progress["lookback_days"] = self.source["lookback_days"]
            return DiscoveryResult(self.source_id, self.discovery_method, "success", list(candidates.values()),
                                   fetched_at, pagination={"page_fetches": page_fetches,
                                                           "ofac_recent_actions": progress})

        boundary_page = 0 if any(row["native_id"] == boundary_id for row in shallow_rows) else None
        if boundary_page is None:
            probe = hint
            while page_fetches < _MAX_REQUESTS:
                if not request(probe):
                    break
                rows = pages[probe]
                if any(row["native_id"] == boundary_id for row in rows):
                    boundary_page = probe
                    break
                # A valid empty page is a stopping signal for this run, not an
                # authoritative statement that the stored boundary expired.
                if not rows:
                    break
                probe += 1
            if failed:
                return DiscoveryResult(self.source_id, self.discovery_method, "partial",
                                       list(candidates.values()), fetched_at, errors,
                                       pagination={"page_fetches": page_fetches})
            if boundary_page is None:
                progress = dict(previous)
                if pages and all(p in pages for p in range(hint, max(pages) + 1)):
                    last = max(pages)
                    if pages[last]:
                        progress["deep_page_hint"] = last + 1
                progress["lookback_days"] = self.source["lookback_days"]
                return DiscoveryResult(self.source_id, self.discovery_method, "success",
                                       list(candidates.values()), fetched_at,
                                       pagination={"page_fetches": page_fetches,
                                                   "ofac_recent_actions": progress})

        # Once rediscovered, fetch consecutive pages after the boundary within
        # the remaining request budget. Boundary movement is based only on
        # complete rows after that positive match.
        next_page = boundary_page + 1
        while page_fetches < _MAX_REQUESTS:
            if not request(next_page):
                break
            if not pages[next_page]:
                break
            next_page += 1
        if failed:
            return DiscoveryResult(self.source_id, self.discovery_method, "partial",
                                   list(candidates.values()), fetched_at, errors,
                                   pagination={"page_fetches": page_fetches})

        ordered = [row for page in sorted(pages) for row in pages[page]]
        ids = [row["native_id"] for row in ordered]
        boundary_index = ids.index(boundary_id)
        beyond = ordered[boundary_index + 1:]
        progress = dict(previous)
        progress["frontier_status"] = "active"
        progress["lookback_days"] = self.source["lookback_days"]
        if beyond:
            newest_boundary = beyond[-1]
            progress["boundary_id"] = newest_boundary["native_id"]
            progress["boundary_date"] = newest_boundary["date"].isoformat()
            page_for_boundary = next(page for page in sorted(pages, reverse=True)
                                     if any(row["native_id"] == newest_boundary["native_id"]
                                            for row in pages[page]))
            progress["deep_page_hint"] = page_for_boundary + 1
        else:
            progress["deep_page_hint"] = boundary_page + 1
        return DiscoveryResult(self.source_id, self.discovery_method, "success",
                               list(candidates.values()), fetched_at,
                               pagination={"page_fetches": page_fetches,
                                           "ofac_recent_actions": progress})
