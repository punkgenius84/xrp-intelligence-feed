from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlencode, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError, HttpAttemptBudget
from discovery.models import DiscoveryCandidate


FDIC_SOURCE_ID = "fdic-press-releases"
FDIC_METHOD = "fdic_press_releases"
FDIC_URL = "https://www.fdic.gov/news/press-releases"
FDIC_HOST = "www.fdic.gov"
_MAX_PAGE_FETCHES = 3
_MAX_HTTP_ATTEMPTS = 9
_ROUTE = re.compile(r"^/news/press-releases/(20\d{2})/([a-z0-9]+(?:-[a-z0-9]+)*)/?$")


def validate_fdic_source(source: object) -> dict[str, Any]:
    required = {"source_id", "name", "authority_tier", "category", "discovery_method",
                "source_url", "enabled", "max_items"}
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"FDIC source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != FDIC_SOURCE_ID:
        raise ValueError(f"source_id must be {FDIC_SOURCE_ID!r}")
    if source["discovery_method"] != FDIC_METHOD:
        raise ValueError(f"discovery_method must be {FDIC_METHOD!r}")
    if source["source_url"] != FDIC_URL:
        raise ValueError(f"source_url must be the fixed official FDIC listing {FDIC_URL}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("FDIC authority_tier must be 1")
    if source["category"] != "government":
        raise ValueError("FDIC category must be government")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 50:
        raise ValueError("max_items must be an integer from 1 to 50")
    return source


def _article_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip() or value.startswith("//"):
        return None
    try:
        parts = urlsplit(urljoin(FDIC_URL, value.strip()))
    except ValueError:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    match = _ROUTE.fullmatch(parts.path)
    if (parts.scheme.lower() != "https" or parts.hostname is None
            or parts.hostname.casefold() != FDIC_HOST or port not in (None, 443)
            or parts.username is not None or parts.password is not None or not match):
        return None
    native_id = f"{match.group(1)}/{match.group(2)}"
    return native_id, f"https://{FDIC_HOST}{parts.path.rstrip('/') }"


class _FDICListingParser(HTMLParser):
    """Extract FDIC releases from the main listing when a semantic main region exists."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []
        self._all_items: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None
        self._anchor_in_main = False
        self._main_depth = 0
        self._saw_main = False
        self.no_results = False
        self._route_like_links = 0
        self._anchor_tags = 0
        self._href_anchors = 0
        self._main_tags = 0
        self._empty_state_tags: list[str] = []
        self._empty_state_in_main: list[bool] = []
        self._empty_state_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").casefold().split())
        empty_state_classes = {
            "view-empty", "no-results", "no-results-found", "empty-state",
            "empty-results", "results-empty",
        }
        if classes & empty_state_classes:
            self._empty_state_tags.append(tag)
            self._empty_state_in_main.append(self._main_depth > 0)
            self._empty_state_text = []
        if tag == "main":
            self._saw_main = True
            self._main_tags += 1
            self._main_depth += 1
        if tag != "a":
            return
        self._anchor_tags += 1
        href = next((value or "" for key, value in attrs if key.casefold() == "href"), "")
        if href:
            self._href_anchors += 1
        identity = _article_identity(href)
        if identity is not None:
            self._anchor = {"native_id": identity[0], "url": identity[1], "title": ""}
            self._anchor_in_main = self._main_depth > 0
        else:
            # Distinguish a genuinely empty listing from a page whose official
            # press-release links changed shape and are no longer recognized.
            try:
                parts = urlsplit(urljoin(FDIC_URL, href.strip()))
            except ValueError:
                return
            if (parts.scheme.lower() == "https" and parts.hostname is not None
                    and parts.hostname.casefold() == FDIC_HOST
                    and parts.path.startswith("/news/press-releases/")):
                self._route_like_links += 1

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["title"] += data
        if self._empty_state_tags:
            self._empty_state_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            title = " ".join(self._anchor["title"].split())
            if title:
                self._all_items.append(self._anchor)
                if self._anchor_in_main:
                    self.items.append(self._anchor)
            self._anchor = None
            self._anchor_in_main = False
        elif tag == "main" and self._main_depth:
            self._main_depth -= 1
        for index in range(len(self._empty_state_tags) - 1, -1, -1):
            if self._empty_state_tags[index] == tag:
                in_main = self._empty_state_in_main[index]
                del self._empty_state_tags[index:]
                del self._empty_state_in_main[index:]
                if not self._empty_state_tags:
                    if in_main and "no results found" in " ".join(self._empty_state_text).casefold():
                        self.no_results = True
                    self._empty_state_text = []
                break

    def listing_items(self) -> list[dict[str, str]]:
        # Prefer the actual semantic content region so an old sidebar/related link
        # cannot masquerade as the saved pagination boundary. Keep compatibility
        # with official pages that do not expose a <main> element.
        return self.items if self._saw_main else self._all_items


def _parse_page(content: bytes, diagnostics: dict[str, object] | None = None) -> tuple[list[dict[str, str]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1252")
    parser = _FDICListingParser()
    parser.feed(text)
    parser.close()
    if diagnostics is not None:
        diagnostics.update({
            "bytes": len(content),
            "anchor_tags": parser._anchor_tags,
            "href_anchors": parser._href_anchors,
            "main_tags": parser._main_tags,
            "all_official_articles": len(parser._all_items),
            "main_official_articles": len(parser.items),
            "all_article_ids": [item["native_id"] for item in parser._all_items],
            "main_article_ids": [item["native_id"] for item in parser.items],
            "route_like_links": parser._route_like_links,
            "semantic_empty_state": parser.no_results,
        })
    if parser._anchor is not None:
        raise ValueError("FDIC Press Releases HTML ended inside an article link")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    if parser._saw_main and len(parser.items) <= 1:
        main_ids = {item["native_id"] for item in parser.items}
        outside_ids = {
            item["native_id"] for item in parser._all_items
            if item["native_id"] not in main_ids
        }
        if outside_ids:
            raise ValueError(
                "FDIC main region contains at most one recognized release while "
                f"{len(outside_ids)} additional official release route(s) exist outside it; "
                "refusing to treat the main region as a complete listing"
            )
    for item in parser.listing_items():
        if item["native_id"] not in seen:
            seen.add(item["native_id"])
            rows.append(item)
    if not rows and parser._route_like_links:
        raise ValueError(
            "FDIC response contains official press-release links with unrecognized routes "
            f"({parser._route_like_links} link(s)); refusing to treat it as an empty listing"
        )
    if not rows and not parser.no_results:
        raise ValueError(
            "FDIC response contains no recognizable Press Releases listing "
            f"(bytes={len(content)}, anchor_tags={parser._anchor_tags}, "
            f"href_anchors={parser._href_anchors}, main_tags={parser._main_tags}, "
            f"recognized_articles={len(rows)}, route_like_links={parser._route_like_links}, "
            f"semantic_empty_state={parser.no_results})"
        )
    return rows, True


class FDICPressReleasesDiscovery:
    source_id = FDIC_SOURCE_ID
    discovery_method = FDIC_METHOD

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_fdic_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _page_url(self, page: int) -> str:
        return FDIC_URL if page == 0 else FDIC_URL + "?" + urlencode({"page": page})

    @staticmethod
    def _saved_progress(state: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        if state is None:
            return {}, False
        if not isinstance(state, dict):
            raise ValueError("Invalid discovery state; refusing to reset the FDIC frontier")
        sources = state.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError("Invalid discovery sources state; refusing to reset the FDIC frontier")
        source_state = sources.get(FDIC_SOURCE_ID, {})
        if not isinstance(source_state, dict):
            raise ValueError("Invalid FDIC source state; refusing to reset the frontier")
        pagination = source_state.get("pagination", {})
        if not isinstance(pagination, dict):
            raise ValueError("Invalid FDIC pagination container; refusing to reset the frontier")
        exists = "fdic_press_releases" in pagination
        saved = pagination.get("fdic_press_releases", {})
        if not isinstance(saved, dict):
            raise ValueError("Invalid FDIC pagination state; refusing to reset the frontier")
        required = {"boundary_id", "deep_page_hint", "frontier_status"}
        if exists and set(saved) != required:
            raise ValueError("Invalid FDIC pagination fields; refusing to reset the frontier")
        if exists and (saved["boundary_id"] is not None and
                        (not isinstance(saved["boundary_id"], str) or
                         not _ROUTE.fullmatch("/news/press-releases/" + saved["boundary_id"]))):
            raise ValueError("Invalid FDIC boundary_id; refusing to reset the frontier")
        if exists and (type(saved["deep_page_hint"]) is not int or saved["deep_page_hint"] < 1):
            raise ValueError("Invalid FDIC deep_page_hint; refusing to reset the frontier")
        if exists and saved["frontier_status"] not in {"active", "boundary_expired"}:
            raise ValueError("Invalid FDIC frontier_status; refusing to reset the frontier")
        return dict(saved), exists

    def _candidate(self, row: dict[str, str], listing_url: str, fetched_at: datetime) -> DiscoveryCandidate:
        native_id = row["native_id"]
        year, slug = native_id.split("/", 1)
        return DiscoveryCandidate(
            title=row["title"], url=row["url"], source=self.source["name"],
            published_at=None,
            summary="FDIC Press Release",
            source_type="discovery", source_id=self.source_id, authority_tier=1,
            category=self.source["category"], source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}", discovery_method=self.discovery_method,
            source_url=FDIC_URL, primary_url=row["url"], provenance=[listing_url, row["url"]],
            document_type="press_release", collected_at=fetched_at,
            first_seen_at=fetched_at, last_seen_at=fetched_at,
            source_native_metadata={"year": year, "slug": slug, "listing_page": listing_url},
        )

    def collect(self, state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured", fetched_at=fetched_at)
        try:
            previous, has_state = self._saved_progress(state)
        except ValueError as exc:
            return DiscoveryResult(self.source_id, self.discovery_method, "failed",
                                   fetched_at=fetched_at, errors=[str(exc)])

        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        rows_by_page: dict[int, list[dict[str, str]]] = {}
        page_fetches = 0
        pages_probed: list[int] = []
        request_updates: dict[str, dict[str, str]] = {}
        budget = HttpAttemptBudget(_MAX_HTTP_ATTEMPTS)

        def request_page(page: int) -> tuple[bool, bool]:
            nonlocal page_fetches
            if page_fetches >= _MAX_PAGE_FETCHES:
                return False, False
            page_fetches += 1
            pages_probed.append(page)
            url = self._page_url(page)
            diagnostics: dict[str, object] = {}

            def report_diagnostics() -> None:
                if page == 0 and diagnostics:
                    all_ids = diagnostics.get("all_article_ids", [])
                    main_ids = diagnostics.get("main_article_ids", [])
                    boundary_id = previous.get("boundary_id") if has_state else None
                    diagnostics["all_first_ids"] = all_ids[:4] if isinstance(all_ids, list) else []
                    diagnostics["main_first_ids"] = main_ids[:4] if isinstance(main_ids, list) else []
                    diagnostics["all_boundary_index"] = (
                        all_ids.index(boundary_id)
                        if isinstance(all_ids, list) and boundary_id in all_ids else -1
                    )
                    diagnostics["main_boundary_index"] = (
                        main_ids.index(boundary_id)
                        if isinstance(main_ids, list) and boundary_id in main_ids else -1
                    )
                    printable = {
                        key: value for key, value in diagnostics.items()
                        if key not in {"all_article_ids", "main_article_ids"}
                    }
                    print(
                        "FDIC listing structure: "
                        + " ".join(f"{key}={value}" for key, value in printable.items())
                    )

            try:
                validators = {}
                if isinstance(state, dict):
                    source_state = state.get("sources", {}).get(self.source_id, {})
                    validators = source_state.get("requests", {}).get(f"page:{page}", {}) if isinstance(source_state, dict) else {}
                response = self.http.get(url, etag=validators.get("etag", ""),
                                         last_modified=validators.get("last_modified", ""),
                                         expected_content_types=("text/html",), attempt_budget=budget)
                if response.status_code == 304:
                    return True, True
                rows, complete = _parse_page(response.content, diagnostics)
                report_diagnostics()
                if not complete:
                    errors.append(f"Malformed FDIC listing page {page}")
                    return False, False
                rows_by_page[page] = rows
                request_updates[f"page:{page}"] = {
                    key: response.headers[key] for key in ("etag", "last-modified") if response.headers.get(key)
                }
                return True, False
            except DiscoveryHttpError as exc:
                errors.append(f"page {page}: {exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else ""))
                return False, False
            except (UnicodeError, ValueError, TypeError) as exc:
                report_diagnostics()
                errors.append(f"page {page}: {exc}")
                return False, False

        ok, unchanged = request_page(0)
        if not ok:
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "partial" if candidates else "failed", list(candidates.values()),
                                   fetched_at, errors, state_updates={},
                                   pagination={"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed)})
        if unchanged:
            pagination = {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed)}
            if has_state:
                pagination["fdic_press_releases"] = previous
            return DiscoveryResult(self.source_id, self.discovery_method, "not_modified",
                                   fetched_at=fetched_at, pagination=pagination)

        page0 = rows_by_page[0]
        if not has_state:
            boundary = page0[-1]["native_id"] if page0 else None
            progress = {"boundary_id": boundary, "deep_page_hint": 1, "frontier_status": "active"}
            for row in page0[:self.source["max_items"]]:
                candidates[row["native_id"]] = self._candidate(row, self._page_url(0), fetched_at)
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "success" if candidates else "empty", list(candidates.values()),
                                   fetched_at, errors, request_updates,
                                   {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed), "fdic_press_releases": progress})

        boundary = previous["boundary_id"]
        hint = previous["deep_page_hint"]
        if boundary is None:
            boundary = page0[-1]["native_id"] if page0 else None
            progress = {**previous, "boundary_id": boundary}
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "empty", [], fetched_at, errors, request_updates,
                                   {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed), "fdic_press_releases": progress})

        def index_of(rows: list[dict[str, str]], native_id: str) -> int | None:
            for index, row in enumerate(rows):
                if row["native_id"] == native_id:
                    return index
            return None

        boundary_page = 0 if index_of(page0, boundary) is not None else None
        if boundary_page is None:
            for page in (hint, hint + 1):
                if page_fetches >= _MAX_PAGE_FETCHES:
                    break
                ok, unchanged = request_page(page)
                if not ok or unchanged:
                    break
                if index_of(rows_by_page[page], boundary) is not None:
                    boundary_page = page
                    break
                if not rows_by_page[page]:
                    break

        if errors:
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "partial" if candidates else "failed", list(candidates.values()),
                                   fetched_at, errors, {}, {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed)})

        progress = dict(previous)
        if boundary_page is None:
            # We have not proven that the saved frontier was crossed. Keep it and
            # move the recovery hint only after complete non-empty probes.
            probed = [p for p in rows_by_page if p > 0]
            if probed and rows_by_page[max(probed)]:
                progress["deep_page_hint"] = max(probed) + 1
            progress["frontier_status"] = "active"
            # Do not claim the page-0 rows as newly discovered when the frontier
            # could not be located; candidates are withheld from this result.
            return DiscoveryResult(self.source_id, self.discovery_method, "empty",
                                   [], fetched_at, [], request_updates,
                                   {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed), "fdic_press_releases": progress})

        # Emit only rows newer than the saved boundary. Recovery pages are emitted
        # only after the boundary has been positively located.
        ordered_new: list[dict[str, str]] = []
        rows = rows_by_page[boundary_page]
        boundary_index = index_of(rows, boundary)
        assert boundary_index is not None
        print(
            "FDIC frontier position: "
            f"page={boundary_page} boundary_index={boundary_index} page_rows={len(rows)} "
            f"first_is_boundary={boundary_index == 0} last_is_boundary={boundary_index == len(rows) - 1}"
        )
        ordered_new.extend(rows[:boundary_index])
        # Every fetched page before the boundary is newer than it. Page 0 is
        # included explicitly because it is always the first shallow page.
        for page in sorted(p for p in rows_by_page if p < boundary_page):
            if page == boundary_page:
                continue
            ordered_new.extend(rows_by_page[page])
        for row in ordered_new:
            if row["native_id"] not in candidates:
                candidates[row["native_id"]] = self._candidate(row, self._page_url(0), fetched_at)

        if ordered_new:
            progress["boundary_id"] = ordered_new[-1]["native_id"]
            progress["deep_page_hint"] = boundary_page + 1
        progress["frontier_status"] = "active"
        status = "success" if candidates else "empty"
        return DiscoveryResult(self.source_id, self.discovery_method, status,
                               list(candidates.values()), fetched_at, [], request_updates,
                               {"page_fetches": page_fetches, "fdic_pages_probed": list(pages_probed), "fdic_press_releases": progress})
