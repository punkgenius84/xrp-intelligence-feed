from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import unescape
import json
import re
import time
from typing import Any, Callable
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


SGPC_SOURCE_ID = "sgpc-media-releases-api"
SGPC_METHOD = "sgpc_media_releases_api"
SGPC_API_URL = "https://www.sgpc.gov.sg/api/ApiMainListing/SubmitData"
SGPC_ORIGIN = "https://www.sgpc.gov.sg"
SGPC_HOSTS = {"www.sgpc.gov.sg", "sgpc.gov.sg"}
SINGAPORE_TZ = ZoneInfo("Asia/Singapore")
_NATIVE_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
_ARTICLE_PATH_RE = re.compile(r"^/media_releases/[a-z0-9-]{1,40}/[a-z0-9_-]{1,40}/[A-Za-z0-9-]{1,100}$", re.IGNORECASE)
_AGENCY_ABBR_RE = re.compile(r"""['"]AgencyAbbr['"]\s*:\s*['"]([A-Z0-9-]{2,20})['"]""")
_MAX_TERMS_HARD = 16
_MAX_TERMS_PER_RUN_HARD = 5
_MAX_ITEMS_HARD = 100


def validate_sgpc_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "search_terms",
        "max_terms_per_run", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"SGPC source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != SGPC_SOURCE_ID:
        raise ValueError(f"source_id must be {SGPC_SOURCE_ID!r}")
    if source["discovery_method"] != SGPC_METHOD:
        raise ValueError(f"discovery_method must be {SGPC_METHOD!r}")
    if source["source_url"] != SGPC_API_URL:
        raise ValueError("source_url must be the fixed official SGPC listing API")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("SGPC authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 90:
        raise ValueError("lookback_days must be an integer from 1 to 90")
    terms = source["search_terms"]
    if (
        not isinstance(terms, list) or not terms or len(terms) > _MAX_TERMS_HARD
        or any(not isinstance(term, str) or not term.strip() or len(term) > 100 for term in terms)
    ):
        raise ValueError(f"search_terms must contain 1 to {_MAX_TERMS_HARD} non-empty strings")
    if len({term.casefold() for term in terms}) != len(terms):
        raise ValueError("search_terms must not contain duplicates")
    per_run = source["max_terms_per_run"]
    if type(per_run) is not int or not 1 <= per_run <= min(_MAX_TERMS_PER_RUN_HARD, len(terms)):
        raise ValueError(f"max_terms_per_run must be from 1 to {min(_MAX_TERMS_PER_RUN_HARD, len(terms))}")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= _MAX_ITEMS_HARD:
        raise ValueError(f"max_items must be an integer from 1 to {_MAX_ITEMS_HARD}")
    return source


def _clean(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<[^>]*>", " ", value)
    return " ".join(unescape(value).split()).strip()


def _official_article_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing SGPC article URL")
    raw = value.strip()
    absolute = urljoin(SGPC_ORIGIN + "/", raw)
    parts = urlsplit(absolute)
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in SGPC_HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not _ARTICLE_PATH_RE.fullmatch(unquote(parts.path))
        or "\\" in unquote(parts.path)
        or any(segment in {".", ".."} for segment in unquote(parts.path).split("/"))
        or parts.query
        or parts.fragment
    ):
        raise ValueError("SGPC article URL is not an approved official media-release route")
    return f"{SGPC_ORIGIN}{parts.path}"


def _published(value: object) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing SGPC publication DateTime")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid SGPC publication DateTime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed.astimezone(timezone.utc), raw


def _agency_abbreviation(item: dict[str, Any], article_url: str) -> str:
    direct = item.get("AgencyAbbr")
    if isinstance(direct, str) and re.fullmatch(r"[A-Za-z0-9-]{2,20}", direct.strip()):
        return direct.strip().upper()
    nested = item.get("SgpcAgencyAgency")
    if isinstance(nested, dict):
        direct = nested.get("AgencyAbbr")
        if isinstance(direct, str) and re.fullmatch(r"[A-Za-z0-9-]{2,20}", direct.strip()):
            return direct.strip().upper()
    elif isinstance(nested, str):
        match = _AGENCY_ABBR_RE.search(nested)
        if match:
            return match.group(1).upper()
    segments = urlsplit(article_url).path.strip("/").split("/")
    if len(segments) >= 2 and re.fullmatch(r"[a-z0-9-]{2,30}", segments[1]):
        return segments[1].upper()
    return ""


def _parse_payload(payload: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid SGPC JSON response") from exc
    if not isinstance(value, dict) or not isinstance(value.get("result"), dict):
        raise ValueError("SGPC response is missing the result object")
    items = value["result"].get("items")
    if not isinstance(items, list):
        raise ValueError("SGPC response is missing result.items list")
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(items):
        try:
            if not isinstance(raw, dict):
                raise ValueError("item is not an object")
            title = _clean(raw.get("Title"))
            article_url = _official_article_url(raw.get("Url"))
            published_at, published_raw = _published(raw.get("DateTime"))
            native_id = raw.get("MediaReleaseId")
            if not isinstance(native_id, str) or not _NATIVE_ID_RE.fullmatch(native_id.strip()):
                # The official article path is also stable if a future API row omits its UUID.
                native_id = urlsplit(article_url).path.rsplit("/", 1)[-1]
            native_id = native_id.strip()
            if not title:
                raise ValueError("missing title")
            if not native_id or not _NATIVE_ID_RE.fullmatch(native_id):
                raise ValueError("missing stable media-release identity")
            agency = _agency_abbreviation(raw, article_url)
            summary = ""
            for key in ("Summary", "Description", "Abstract", "Teaser", "ShortDescription", "Content"):
                summary = _clean(raw.get(key))
                if summary:
                    break
            rows.append({
                "native_id": native_id,
                "title": title,
                "url": article_url,
                "published_at": published_at,
                "published_at_local": published_raw,
                "agency": agency,
                "summary": summary,
            })
        except (TypeError, ValueError) as exc:
            errors.append(f"item {index}: {exc}")
    return rows, errors


class SGPCMediaReleasesDiscovery:
    source_id = SGPC_SOURCE_ID
    discovery_method = SGPC_METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        request_interval: float = 1.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_sgpc_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4 (official SGPC listing API)")
        self.sleep = sleep
        self.request_interval = max(1.0, request_interval)
        self.now = now
        self._last_request: float | None = None

    def _pace(self) -> None:
        if self._last_request is not None:
            self.sleep(max(0.0, self.request_interval - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()

    def _query_url(self, start_date: date, end_date: date, term: str | None = None) -> str:
        params = {
            "StartDate": start_date.strftime("%d-%b-%Y"),
            "EndDate": end_date.strftime("%d-%b-%Y"),
            "page": "1",
        }
        if term:
            params["KeySearch"] = term
        return SGPC_API_URL + "?" + urlencode(params)

    def _fetch_query(
        self,
        request_id: str,
        url: str,
        source_state: dict[str, Any],
        fetched_at: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, str], list[str], str]:
        validators = JsonDiscoveryState.request_validators(source_state, self.source_id, request_id)
        self._pace()
        response = self.http.get(
            url,
            etag=validators.get("etag", ""),
            last_modified=validators.get("last_modified", ""),
            expected_content_types=("application/json", "text/json"),
        )
        final = urlsplit(response.url)
        if final.scheme != "https" or final.hostname not in SGPC_HOSTS or final.port not in (None, 443):
            raise ValueError("SGPC API response redirected outside the approved HTTPS host")
        updates = {}
        etag = response.headers.get("etag", validators.get("etag", ""))
        last_modified = response.headers.get("last-modified", validators.get("last_modified", ""))
        if etag:
            updates["etag"] = etag
        if last_modified:
            updates["last_modified"] = last_modified
        if response.status_code == 304:
            return [], updates, [], "not_modified"
        if response.status_code != 200:
            raise ValueError(f"unexpected HTTP status {response.status_code}")
        rows, errors = _parse_payload(response.content)
        if not errors:
            updates = {
                key: response.headers[key]
                for key in ("etag", "last-modified")
                if response.headers.get(key)
            }
            # JsonDiscoveryState stores normalized names, not raw HTTP header names.
            updates = {
                ("last_modified" if key == "last-modified" else "etag"): value
                for key, value in updates.items()
            }
        else:
            updates = {}
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        rows = [row for row in rows if row["published_at"] >= cutoff]
        return rows, updates, errors, "success"

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured", fetched_at=fetched_at)

        state = source_state if isinstance(source_state, dict) else {}
        prior = state.get("sources", {}).get(self.source_id, {})
        pagination = prior.get("pagination", {}) if isinstance(prior, dict) else {}
        start_index = pagination.get("next_term_index", 0) if isinstance(pagination, dict) else 0
        if type(start_index) is not int:
            start_index = 0
        terms = self.source["search_terms"]
        start_index %= len(terms)
        selected = [terms[(start_index + offset) % len(terms)]
                    for offset in range(self.source["max_terms_per_run"])]

        local_today = fetched_at.astimezone(SINGAPORE_TZ).date()
        start_date = local_today - timedelta(days=self.source["lookback_days"] - 1)
        end_date = local_today
        date_key = f"{start_date.isoformat()}:{end_date.isoformat()}"
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])

        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        state_updates: dict[str, dict[str, str]] = {}
        requests_made = 0

        # Always inspect the newest unfiltered page, then rotate through a bounded
        # set of targeted keyword searches to cover older but still-recent releases.
        queries: list[tuple[str, str | None, int | None]] = [
            (f"latest:{date_key}", None, None),
        ]
        queries.extend(
            (f"term:{term}:{date_key}", term, start_index + offset)
            for offset, term in enumerate(selected)
        )
        next_term_index = start_index
        for request_id, term, term_index in queries:
            url = self._query_url(start_date, end_date, term)
            requests_made += 1
            try:
                rows, validators, row_errors, result_status = self._fetch_query(
                    request_id, url, state, fetched_at
                )
            except (DiscoveryHttpError, ValueError, TypeError) as exc:
                label = "latest" if term is None else f"term {term!r}"
                errors.append(f"SGPC {label}: {type(exc).__name__}: {exc}")
                if term is not None and term_index is not None:
                    next_term_index = term_index % len(terms)
                    break
                continue
            if validators or result_status == "not_modified":
                state_updates[request_id] = validators
            if row_errors:
                errors.extend(f"SGPC {term or 'latest'}: {error}" for error in row_errors[:10])
                if term is not None and term_index is not None:
                    next_term_index = term_index % len(terms)
                    break
            if term is not None and term_index is not None:
                next_term_index = (term_index + 1) % len(terms)
            for row in rows:
                if row["published_at"] < cutoff:
                    continue
                native_id = row["native_id"]
                summary = row["summary"] or row["title"]
                if row["agency"]:
                    summary = f"{summary} | Official agency: {row['agency']}."
                summary = (summary + " Published by the Singapore Government Press Centre.")[:1200]
                candidate = DiscoveryCandidate(
                    title=row["title"],
                    url=row["url"],
                    source=self.source["name"],
                    published_at=row["published_at"],
                    summary=summary,
                    source_type="discovery",
                    source_id=self.source_id,
                    authority_tier=1,
                    category=self.source["category"],
                    source_native_id=native_id,
                    candidate_id=f"{self.source_id}:{native_id}",
                    discovery_method=self.discovery_method,
                    source_url=url,
                    document_type="Government Press Release",
                    primary_url=row["url"],
                    provenance=[SGPC_API_URL, row["url"]],
                    source_native_metadata={
                        "media_release_id": native_id,
                        "agency_abbreviation": row["agency"],
                        "published_at_local": row["published_at_local"],
                        "search_term": term or "",
                    },
                    collected_at=fetched_at,
                    first_seen_at=fetched_at,
                    last_seen_at=fetched_at,
                )
                previous = candidates.get(native_id)
                if previous is not None:
                    if previous.url != candidate.url or previous.title != candidate.title:
                        errors.append(f"conflicting duplicate SGPC media release ID {native_id}")
                    else:
                        previous.provenance = list(dict.fromkeys([*previous.provenance, url]))
                    continue
                candidates[native_id] = candidate

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
            pagination={
                "next_term_index": next_term_index,
                "requests_made": requests_made,
            },
        )
