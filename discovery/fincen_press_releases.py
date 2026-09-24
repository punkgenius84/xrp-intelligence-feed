from __future__ import annotations

from datetime import date, datetime, time as day_time, timedelta, timezone
from html.parser import HTMLParser
import re
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


FINCEN_SOURCE_ID = "fincen-press-releases"
FINCEN_METHOD = "fincen_press_releases"
FINCEN_URL = "https://www.fincen.gov/news/press-releases"
FINCEN_HOST = "www.fincen.gov"
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ARTICLE_PATH = re.compile(r"^/news/news-releases/([a-z0-9]+(?:-[a-z0-9]+)*)/?$")
_DATE_FORMATS = ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y")
_MAX_PAGE_FETCHES = 3


def validate_fincen_source(source: object) -> dict[str, Any]:
    required = {"source_id", "name", "authority_tier", "category", "discovery_method",
                "source_url", "enabled", "lookback_days", "max_items"}
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"FinCEN source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != FINCEN_SOURCE_ID:
        raise ValueError(f"source_id must be {FINCEN_SOURCE_ID!r}")
    if source["discovery_method"] != FINCEN_METHOD:
        raise ValueError(f"discovery_method must be {FINCEN_METHOD!r}")
    if source["source_url"] != FINCEN_URL:
        raise ValueError(f"source_url must be the fixed official FinCEN listing {FINCEN_URL}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("FinCEN authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 180:
        raise ValueError("lookback_days must be an integer from 1 to 180")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    return source


def _article_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # Allow site-relative routes, but reject protocol-relative or non-HTTPS URLs.
    if raw.startswith("//"):
        return None
    try:
        parts = urlsplit(urljoin(FINCEN_URL, raw))
    except ValueError:
        return None
    match = _ARTICLE_PATH.fullmatch(parts.path)
    try:
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme.lower() != "https" or not parts.hostname
            or parts.hostname.casefold() != FINCEN_HOST or port not in (None, 443)
            or parts.username is not None or parts.password is not None or not match):
        return None
    slug = match.group(1)
    if not _SLUG.fullmatch(slug):
        return None
    return slug, f"https://{FINCEN_HOST}/news/news-releases/{slug}"


def _parse_date(value: object) -> date:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing or invalid publication date")
    text = " ".join(value.split())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[^\s]+)?", text):
        try:
            return (datetime.fromisoformat(text.replace("Z", "+00:00")).date()
                    if "T" in text else date.fromisoformat(text))
        except ValueError as exc:
            raise ValueError("missing or invalid publication date") from exc
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("missing or invalid publication date")


def _is_listing(classes: set[str]) -> bool:
    combined = " ".join(classes).casefold().replace("_", "-")
    return "view" in classes and "press" in combined and "release" in combined


def _is_date_field(classes: set[str]) -> bool:
    for value in classes:
        folded = value.casefold()
        if ((folded.startswith("views-field-") or folded.startswith("field--name-"))
                and ("date" in folded or "created" in folded)):
            return True
    return False


def _is_category_field(classes: set[str]) -> bool:
    return any((value.casefold().startswith(("views-field-", "field--name-"))
                and any(term in value.casefold() for term in ("category", "type", "news")))
               for value in classes)


class _FinCENListingParser(HTMLParser):
    """Read only views rows inside the FinCEN Press Releases Drupal listing."""

    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.listing_found = False
        self.content_found = False
        self._stack: list[str] = []
        self._listing_depth: int | None = None
        self._content_depth: int | None = None
        self._row: dict[str, Any] | None = None
        self._row_depth: int | None = None
        self._date_depth: int | None = None
        self._category_depth: int | None = None
        self._title_field_depth: int | None = None
        self._anchor: dict[str, str] | None = None

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        raw = next((value or "" for key, value in attrs if key.casefold() == "class"), "")
        return set(raw.split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        depth = len(self._stack) + 1
        classes = self._classes(attrs)
        if tag in {"div", "section"} and self._listing_depth is None and _is_listing(classes):
            self.listing_found = True
            self._listing_depth = depth
        if (self._listing_depth is not None and tag in {"div", "section"}
                and "view-content" in classes):
            self.content_found = True
            self._content_depth = depth
        if (self._content_depth is not None and self._row is None
                and tag in {"div", "article", "li"} and "views-row" in classes):
            self._row = {"anchors": [], "date": "", "category": ""}
            self._row_depth = depth
        if self._row is not None:
            if _is_date_field(classes):
                self._date_depth = depth
            if _is_category_field(classes):
                self._category_depth = depth
            if any(value.casefold() == "views-field-title" for value in classes):
                self._title_field_depth = depth
            if tag == "time" and self._date_depth is not None:
                datetime_value = next((value or "" for key, value in attrs
                                       if key.casefold() == "datetime"), "")
                if datetime_value:
                    self._row["date"] = datetime_value
            if tag == "a":
                href = next((value or "" for key, value in attrs if key.casefold() == "href"), "")
                self._anchor = {"href": href, "text": "",
                                "is_title": self._title_field_depth is not None}
        if tag not in self._VOID:
            self._stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        if self._date_depth is not None and not self._row["date"]:
            self._row["date"] += data
        if self._category_depth is not None:
            self._row["category"] += data
        if self._anchor is not None:
            self._anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        depth = len(self._stack)
        if tag == "a" and self._anchor is not None and self._row is not None:
            self._row["anchors"].append(self._anchor)
            self._anchor = None
        if self._date_depth == depth:
            self._date_depth = None
        if self._category_depth == depth:
            self._category_depth = None
        if self._title_field_depth == depth:
            self._title_field_depth = None
        if self._row is not None and self._row_depth == depth and tag in {"div", "article", "li"}:
            self.rows.append(self._row)
            self._row = None
            self._row_depth = None
            self._date_depth = self._category_depth = self._title_field_depth = None
        if self._content_depth == depth:
            self._content_depth = None
        if self._listing_depth == depth:
            self._listing_depth = None
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()
        elif tag in self._stack:
            index = len(self._stack) - 1 - self._stack[::-1].index(tag)
            del self._stack[index:]


def _parse_page(content: bytes) -> tuple[list[dict[str, Any]], bool]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("windows-1252")
    parser = _FinCENListingParser()
    parser.feed(text)
    parser.close()
    if not parser.listing_found or not parser.content_found:
        raise ValueError("FinCEN response is missing the Press Releases listing structure")
    if (parser._row is not None or parser._listing_depth is not None
            or parser._content_depth is not None or parser._stack):
        raise ValueError("FinCEN Press Releases HTML ended inside an incomplete row")

    rows: list[dict[str, Any]] = []
    complete = True
    for raw in parser.rows:
        article = next(((_article_identity(anchor["href"]), anchor["text"])
                        for anchor in raw["anchors"] if anchor.get("is_title")), None)
        if article is None or article[0] is None:
            # A row with no title-field link is malformed; never infer identity from its text.
            complete = False
            continue
        native_id, article_url = article[0]
        title = " ".join(article[1].split())
        if not title:
            complete = False
            continue
        try:
            published = _parse_date(raw["date"])
        except ValueError:
            complete = False
            continue
        category = " ".join(raw["category"].split())
        if not category:
            complete = False
            continue
        rows.append({"native_id": native_id, "url": article_url, "title": title,
                     "date": published, "category": category})
    return rows, complete


class FinCENPressReleasesDiscovery:
    source_id = FINCEN_SOURCE_ID
    discovery_method = FINCEN_METHOD

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_fincen_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _page_url(self, page: int) -> str:
        return FINCEN_URL + "?" + urlencode({"page": page})

    def _candidate(self, row: dict[str, Any], listing_url: str, fetched_at: datetime
                   ) -> DiscoveryCandidate:
        native_id = row["native_id"]
        category = row["category"]
        return DiscoveryCandidate(
            title=row["title"], url=row["url"], source=self.source["name"],
            published_at=datetime.combine(row["date"], day_time.min, tzinfo=timezone.utc),
            summary=(f"FinCEN Press Release" + (f" — {category}" if category else "")),
            source_type="discovery", source_id=self.source_id, authority_tier=1,
            category=self.source["category"], source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}", discovery_method=self.discovery_method,
            source_url=listing_url, primary_url=row["url"],
            provenance=[listing_url, row["url"]], document_type=category,
            collected_at=fetched_at, first_seen_at=fetched_at, last_seen_at=fetched_at,
            source_native_metadata={"slug": native_id, "publication_date": row["date"].isoformat(),
                                    "category": category},
        )

    @staticmethod
    def _saved_progress(state: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        if state is None:
            return {}, False
        if not isinstance(state, dict):
            raise ValueError("Invalid discovery state; refusing to reset the FinCEN frontier")
        sources = state.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError("Invalid discovery sources state; refusing to reset the FinCEN frontier")
        source_state = sources.get(FINCEN_SOURCE_ID, {})
        if not isinstance(source_state, dict):
            raise ValueError("Invalid FinCEN source state; refusing to reset the frontier")
        pagination = source_state.get("pagination", {})
        if not isinstance(pagination, dict):
            raise ValueError("Invalid FinCEN pagination container; refusing to reset the frontier")
        exists = "fincen_press_releases" in pagination
        saved = pagination.get("fincen_press_releases", {})
        if not isinstance(saved, dict):
            raise ValueError("Invalid FinCEN pagination state; refusing to reset the frontier")
        required = {"boundary_id", "boundary_date", "deep_page_hint", "frontier_status", "lookback_days"}
        if exists and (set(saved) != required or type(saved.get("deep_page_hint")) is not int
                       or saved["deep_page_hint"] < 1
                       or not isinstance(saved.get("frontier_status"), str)
                       or saved["frontier_status"] not in {"active", "boundary_expired"}
                       or type(saved.get("lookback_days")) is not int
                       or not 1 <= saved["lookback_days"] <= 180):
            raise ValueError("Invalid FinCEN pagination fields; refusing to reset the frontier")
        boundary_id = saved.get("boundary_id")
        boundary_date = saved.get("boundary_date")
        if exists:
            if boundary_id is None:
                if boundary_date is not None:
                    raise ValueError("Incomplete FinCEN boundary state; refusing to reset the frontier")
            elif not isinstance(boundary_id, str) or not _SLUG.fullmatch(boundary_id):
                raise ValueError("Invalid FinCEN boundary_id; refusing to reset the frontier")
            if boundary_date is not None:
                try:
                    parsed_date = date.fromisoformat(boundary_date)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid FinCEN boundary_date; refusing to reset the frontier") from exc
                if not isinstance(boundary_date, str) or parsed_date.isoformat() != boundary_date:
                    raise ValueError("Invalid FinCEN boundary_date; refusing to reset the frontier")
            if (boundary_id is not None) != (boundary_date is not None):
                raise ValueError("Incomplete FinCEN boundary state; refusing to reset the frontier")
            if boundary_id is None and (saved["deep_page_hint"] != 1
                                        or saved["frontier_status"] != "active"):
                raise ValueError("Inconsistent empty FinCEN frontier; refusing to reset the boundary")
        return saved, exists

    def collect(self, state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured",
                                   fetched_at=fetched_at)
        try:
            previous, has_state = self._saved_progress(state)
        except ValueError as exc:
            return DiscoveryResult(self.source_id, self.discovery_method, "failed",
                                   fetched_at=fetched_at, errors=[str(exc)])

        today = fetched_at.astimezone(timezone.utc).date()
        cutoff = today - timedelta(days=self.source["lookback_days"] - 1)
        candidates: dict[str, DiscoveryCandidate] = {}
        rows_by_page: dict[int, list[dict[str, Any]]] = {}
        state_updates: dict[str, dict[str, str]] = {}
        errors: list[str] = []
        page_fetches = 0
        items_processed = 0
        failed = False

        def request_page(page: int) -> tuple[bool, bool]:
            nonlocal page_fetches, items_processed, failed
            if page_fetches >= _MAX_PAGE_FETCHES:
                return False, False
            request_id = f"page_{page}"
            validators = JsonDiscoveryState.request_validators(state or {}, self.source_id, request_id)
            listing_url = self._page_url(page)
            page_fetches += 1
            try:
                response = self.http.get(
                    listing_url, etag=validators.get("etag", ""),
                    last_modified=validators.get("last_modified", ""),
                    expected_content_types=("text/html", "application/xhtml+xml"),
                )
                if response.status_code == 304:
                    state_updates[request_id] = {
                        key: response.headers.get(key, validators.get(key, ""))
                        for key in ("etag", "last_modified")
                        if response.headers.get(key, validators.get(key, ""))
                    }
                    return True, True
                rows, complete = _parse_page(response.content)
                remaining_items = self.source["max_items"] - items_processed
                if len(rows) > remaining_items:
                    rows = rows[:remaining_items]
                    complete = False
                    errors.append("FinCEN max_items limit exceeded; page was truncated")
                items_processed += len(rows)
                rows_by_page[page] = rows
                for row in rows:
                    if row["date"] < cutoff:
                        continue
                    existing = candidates.get(row["native_id"])
                    candidate = self._candidate(row, listing_url, fetched_at)
                    if existing is None:
                        candidates[row["native_id"]] = candidate
                    else:
                        existing.provenance = list(dict.fromkeys(existing.provenance + candidate.provenance))
                        if listing_url not in existing.provenance:
                            existing.provenance.append(listing_url)
                if not complete:
                    errors.append(f"Malformed FinCEN listing row on page {page}")
                    failed = True
                    return False, False
                etag = response.headers.get("etag", "")
                last_modified = response.headers.get("last-modified", "")
                state_updates[request_id] = {
                    key: value for key, value in (("etag", etag), ("last_modified", last_modified))
                    if value
                }
                return True, False
            except DiscoveryHttpError as exc:
                errors.append(f"page {page}: {exc.kind}" +
                              (f" (HTTP {exc.status_code})" if exc.status_code else ""))
            except (UnicodeError, ValueError, TypeError) as exc:
                errors.append(f"page {page}: {exc}")
            failed = True
            return False, False

        ok, not_modified = request_page(0)
        if not ok:
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "partial" if candidates else "failed",
                                   candidates=list(candidates.values()), fetched_at=fetched_at,
                                   errors=errors, state_updates=state_updates,
                                   pagination={"page_fetches": page_fetches})
        if not_modified:
            pagination: dict[str, Any] = {"page_fetches": page_fetches}
            if has_state:
                progress = dict(previous)
                if (progress["boundary_date"] is not None
                        and date.fromisoformat(progress["boundary_date"]) < cutoff):
                    progress["frontier_status"] = "boundary_expired"
                progress["lookback_days"] = self.source["lookback_days"]
                pagination["fincen_press_releases"] = progress
            return DiscoveryResult(self.source_id, self.discovery_method, "not_modified",
                                   fetched_at=fetched_at, state_updates=state_updates,
                                   pagination=pagination)

        first_rows = rows_by_page[0]
        if not has_state:
            eligible = [row for row in first_rows if row["date"] >= cutoff]
            oldest = min(eligible, key=lambda row: (row["date"], row["native_id"])) if eligible else None
            progress = {"boundary_id": oldest["native_id"] if oldest else None,
                        "boundary_date": oldest["date"].isoformat() if oldest else None,
                        "deep_page_hint": 1, "frontier_status": "active",
                        "lookback_days": self.source["lookback_days"]}
            pagination = {"page_fetches": page_fetches, "fincen_press_releases": progress}
            status = "partial" if errors and candidates else "failed" if errors else (
                "empty" if not candidates else "success")
            return DiscoveryResult(self.source_id, self.discovery_method, status,
                                   list(candidates.values()), fetched_at, errors,
                                   state_updates=state_updates, pagination=pagination)

        boundary_id = previous["boundary_id"]
        boundary_date = date.fromisoformat(previous["boundary_date"]) if previous["boundary_date"] else None
        hint = previous["deep_page_hint"]
        frontier_status = previous["frontier_status"]
        if errors:
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "partial" if candidates else "failed", list(candidates.values()),
                                   fetched_at, errors, state_updates=state_updates,
                                   pagination={"page_fetches": page_fetches})

        progress = dict(previous)
        if boundary_id is None:
            eligible = [row for row in first_rows if row["date"] >= cutoff]
            oldest = min(eligible, key=lambda row: (row["date"], row["native_id"])) if eligible else None
            progress.update({"boundary_id": oldest["native_id"] if oldest else None,
                             "boundary_date": oldest["date"].isoformat() if oldest else None,
                             "deep_page_hint": 1, "frontier_status": "active",
                             "lookback_days": self.source["lookback_days"]})
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "empty" if not candidates else "success", list(candidates.values()),
                                   fetched_at, state_updates=state_updates,
                                   pagination={"page_fetches": page_fetches,
                                               "fincen_press_releases": progress})

        if boundary_date < cutoff or (frontier_status == "boundary_expired"
                                      and not any(row["native_id"] == boundary_id for row in first_rows)):
            progress["frontier_status"] = "boundary_expired"
            progress["lookback_days"] = self.source["lookback_days"]
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "empty" if not candidates else "success", list(candidates.values()),
                                   fetched_at, state_updates=state_updates,
                                   pagination={"page_fetches": page_fetches,
                                               "fincen_press_releases": progress})

        boundary_page: int | None = 0 if any(row["native_id"] == boundary_id for row in first_rows) else None
        probe_pages: list[int] = []
        if boundary_page is None:
            for probe in range(hint, hint + 2):
                if page_fetches >= _MAX_PAGE_FETCHES:
                    break
                ok, unchanged = request_page(probe)
                if not ok or unchanged:
                    break
                probe_pages.append(probe)
                page_rows = rows_by_page[probe]
                if any(row["native_id"] == boundary_id for row in page_rows):
                    boundary_page = probe
                    break
                if not page_rows:
                    break
            if failed:
                return DiscoveryResult(self.source_id, self.discovery_method,
                                       "partial" if candidates else "failed", list(candidates.values()),
                                       fetched_at, errors, state_updates=state_updates,
                                       pagination={"page_fetches": page_fetches})
            if boundary_page is None:
                if probe_pages and rows_by_page[probe_pages[-1]]:
                    progress["deep_page_hint"] = probe_pages[-1] + 1
                progress["frontier_status"] = "active"
                progress["lookback_days"] = self.source["lookback_days"]
                return DiscoveryResult(self.source_id, self.discovery_method,
                                       "empty" if not candidates else "success", list(candidates.values()),
                                       fetched_at, state_updates=state_updates,
                                       pagination={"page_fetches": page_fetches,
                                                   "fincen_press_releases": progress})

        # Once the saved boundary is positively located, fetch consecutive pages
        # after it within the remaining request budget. Empty pages stop probing.
        next_page = boundary_page + 1
        while page_fetches < _MAX_PAGE_FETCHES:
            ok, unchanged = request_page(next_page)
            if not ok or unchanged or not rows_by_page[next_page]:
                break
            next_page += 1
        if failed:
            return DiscoveryResult(self.source_id, self.discovery_method,
                                   "partial" if candidates else "failed", list(candidates.values()),
                                   fetched_at, errors, state_updates=state_updates,
                                   pagination={"page_fetches": page_fetches})

        ordered_after_boundary: list[dict[str, Any]] = []
        found_rows = rows_by_page[boundary_page]
        index = next(row_index for row_index, row in enumerate(found_rows)
                     if row["native_id"] == boundary_id)
        ordered_after_boundary.extend(found_rows[index + 1:])
        for page in sorted(page for page in rows_by_page if page > boundary_page):
            ordered_after_boundary.extend(rows_by_page[page])
        usable_after = [row for row in ordered_after_boundary if row["date"] >= cutoff]
        if usable_after:
            new_boundary = usable_after[-1]
            progress.update({"boundary_id": new_boundary["native_id"],
                             "boundary_date": new_boundary["date"].isoformat(),
                             "deep_page_hint": next(page for page in sorted(rows_by_page, reverse=True)
                                                    if any(row["native_id"] == new_boundary["native_id"]
                                                           for row in rows_by_page[page])) + 1})
        else:
            progress["deep_page_hint"] = boundary_page + 1
        progress["frontier_status"] = "active"
        progress["lookback_days"] = self.source["lookback_days"]
        status = "empty" if not candidates else "success"
        return DiscoveryResult(self.source_id, self.discovery_method, status,
                               list(candidates.values()), fetched_at, state_updates=state_updates,
                               pagination={"page_fetches": page_fetches,
                                           "fincen_press_releases": progress})
