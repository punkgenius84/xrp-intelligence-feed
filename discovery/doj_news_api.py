from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import re
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


DOJ_SOURCE_ID = "doj-news-api"
DOJ_METHOD = "doj_press_releases_api"
DOJ_API_URL = "https://www.justice.gov/api/v1/press_releases.json"
DOJ_HOSTS = {"www.justice.gov", "justice.gov"}
DOJ_NAME = "U.S. Department of Justice"
_MAX_TERMS_HARD = 12
_MAX_PAGES_HARD = 2
_DEFAULT_TERMS = (
    "cryptocurrency",
    "digital asset",
    "stablecoin",
    "blockchain",
    "Ripple",
    "XRP",
    "virtual currency",
    "money laundering",
)


def validate_doj_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "search_terms", "page_size",
        "max_pages", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"DOJ source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != DOJ_SOURCE_ID:
        raise ValueError(f"source_id must be {DOJ_SOURCE_ID!r}")
    if source["discovery_method"] != DOJ_METHOD:
        raise ValueError(f"discovery_method must be {DOJ_METHOD!r}")
    if source["source_url"] != DOJ_API_URL:
        raise ValueError("source_url must be the fixed official DOJ News API endpoint")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("DOJ authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 180:
        raise ValueError("lookback_days must be an integer from 1 to 180")
    terms = source["search_terms"]
    if (not isinstance(terms, list) or not terms or len(terms) > _MAX_TERMS_HARD
            or any(not isinstance(term, str) or not term.strip() for term in terms)):
        raise ValueError(f"search_terms must contain 1 to {_MAX_TERMS_HARD} non-empty strings")
    if len({term.casefold() for term in terms}) != len(terms):
        raise ValueError("search_terms must not contain duplicates")
    if type(source["page_size"]) is not int or not 1 <= source["page_size"] <= 50:
        raise ValueError("page_size must be an integer from 1 to 50")
    if type(source["max_pages"]) is not int or not 1 <= source["max_pages"] <= _MAX_PAGES_HARD:
        raise ValueError(f"max_pages must be an integer from 1 to {_MAX_PAGES_HARD}")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 200:
        raise ValueError("max_items must be an integer from 1 to 200")
    return source


def _official_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in DOJ_HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/")
    ):
        return None
    return f"https://www.justice.gov{parts.path}" + (f"?{parts.query}" if parts.query else "")


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())


def _published(value: object) -> datetime:
    try:
        stamp = int(str(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("missing or invalid DOJ publication timestamp") from exc
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("missing or invalid DOJ publication timestamp") from exc


def _parse(payload: bytes) -> tuple[list[dict[str, Any]], bool]:
    import json
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid DOJ JSON response") from exc
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("DOJ response is missing a results list")
    rows: list[dict[str, Any]] = []
    complete = True
    for raw in value["results"]:
        try:
            if not isinstance(raw, dict):
                raise ValueError("record is not an object")
            uuid = raw.get("uuid")
            title = _clean(raw.get("title"))
            url = _official_url(raw.get("url"))
            if not isinstance(uuid, str) or not uuid.strip():
                raise ValueError("missing uuid")
            if not title:
                raise ValueError("missing title")
            if not url:
                raise ValueError("missing official DOJ URL")
            rows.append({
                "uuid": uuid.strip(),
                "title": title,
                "url": url,
                "date": _published(raw.get("date")),
                "teaser": _clean(raw.get("teaser")),
                "topic": _clean(raw.get("topic")),
            })
        except (TypeError, ValueError):
            complete = False
    return rows, complete


class DOJNewsAPIDiscovery:
    source_id = DOJ_SOURCE_ID
    discovery_method = DOJ_METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_doj_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _url(self, term: str, page: int) -> str:
        query = urlencode({
            "fields": "date,title,url,uuid,teaser,topic",
            "parameters[title]": term,
            "sort": "created",
            "direction": "DESC",
            "pagesize": self.source["page_size"],
            "page": page,
        })
        return f"{DOJ_API_URL}?{query}"

    def _candidate(self, row: dict[str, Any], request_url: str, fetched_at: datetime) -> DiscoveryCandidate:
        native_id = row["uuid"]
        summary = row["teaser"] or row["title"]
        if row["topic"]:
            summary = f"{summary} | DOJ topic: {row['topic']}"
        return DiscoveryCandidate(
            title=row["title"],
            url=row["url"],
            source=self.source["name"],
            published_at=row["date"],
            summary=summary[:1000],
            source_type="discovery",
            source_id=self.source_id,
            authority_tier=1,
            category=self.source["category"],
            source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}",
            discovery_method=self.discovery_method,
            source_url=request_url,
            document_type="DOJ Press Release",
            primary_url=row["url"],
            provenance=[DOJ_API_URL, row["url"]],
            collected_at=fetched_at,
            first_seen_at=fetched_at,
            last_seen_at=fetched_at,
            source_native_metadata={
                "uuid": native_id,
                "topic": row["topic"],
            },
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
        requests_made = 0

        for term in self.source["search_terms"]:
            for page in range(self.source["max_pages"]):
                requests_made += 1
                request_id = f"{term}:page-{page}"
                validators = JsonDiscoveryState.request_validators(
                    source_state if isinstance(source_state, dict) else {},
                    self.source_id,
                    request_id,
                )
                url = self._url(term, page)
                try:
                    response = self.http.get(
                        url,
                        etag=validators.get("etag", ""),
                        last_modified=validators.get("last_modified", ""),
                        expected_content_types=("application/json", "application/json; charset=utf-8"),
                    )
                except DiscoveryHttpError as exc:
                    errors.append(
                        f"DOJ {term} page {page}: {exc.kind}"
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
                    errors.append(f"DOJ {term} page {page}: unexpected HTTP status {response.status_code}")
                    break

                try:
                    rows, complete = _parse(response.content)
                except ValueError as exc:
                    errors.append(f"DOJ {term} page {page}: malformed JSON ({exc})")
                    break
                if not complete:
                    errors.append(f"DOJ {term} page {page}: malformed record")
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
                            errors.append(f"DOJ: conflicting duplicate UUID {candidate.source_native_id}")
                        continue
                    candidates[candidate.source_native_id] = candidate

                if len(rows) < self.source["page_size"]:
                    break
                if rows and all(row["date"] < cutoff for row in rows):
                    break

            if len(candidates) >= self.source["max_items"]:
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
            pagination={"requests_made": requests_made},
        )
