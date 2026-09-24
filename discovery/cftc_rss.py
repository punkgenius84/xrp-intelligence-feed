from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import re
from typing import Any, Callable
from urllib.parse import urlsplit

import feedparser

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


CFTC_SOURCE_ID = "cftc-press-releases"
CFTC_METHOD = "cftc_rss"
CFTC_HOSTS = {"www.cftc.gov", "cftc.gov"}
CFTC_FEEDS = {
    "general": {
        "name": "General Press Releases",
        "category": "general",
        "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
    },
    "enforcement": {
        "name": "Enforcement Press Releases",
        "category": "enforcement",
        "url": "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
    },
}
_RELEASE_ID = re.compile(r"^\d{4,5}-\d{2}$")
_RELEASE_PATH = re.compile(r"^/PressRoom/PressReleases/(\d{4,5}-\d{2})/?$")


def validate_cftc_source(source: object) -> dict[str, Any]:
    required = {"source_id", "name", "authority_tier", "category", "discovery_method",
                "source_url", "enabled", "lookback_days", "max_items_per_feed"}
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"CFTC source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != CFTC_SOURCE_ID:
        raise ValueError(f"source_id must be {CFTC_SOURCE_ID!r}")
    if source["discovery_method"] != CFTC_METHOD:
        raise ValueError(f"discovery_method must be {CFTC_METHOD!r}")
    if source["source_url"] != "https://www.cftc.gov/RSS/index.htm":
        raise ValueError("source_url must be the official CFTC RSS index")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("CFTC authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 180:
        raise ValueError("lookback_days must be an integer from 1 to 180")
    if type(source["max_items_per_feed"]) is not int or not 1 <= source["max_items_per_feed"] <= 100:
        raise ValueError("max_items_per_feed must be an integer from 1 to 100")
    return source


def _article_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    match = _RELEASE_PATH.fullmatch(parts.path)
    try:
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme.lower() != "https" or parts.hostname is None
            or parts.hostname.casefold() not in CFTC_HOSTS or port not in (None, 443)
            or parts.username is not None or parts.password is not None or not match):
        return None
    release_id = match.group(1)
    if not _RELEASE_ID.fullmatch(release_id):
        return None
    return release_id, f"https://www.cftc.gov/PressRoom/PressReleases/{release_id}"


def _publication_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing or invalid publication date")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("missing or invalid publication date") from exc
    if parsed.tzinfo is None:
        raise ValueError("publication date must include a timezone")
    return parsed.astimezone(timezone.utc)


class CFTCRSSDiscovery:
    source_id = CFTC_SOURCE_ID
    discovery_method = CFTC_METHOD

    def __init__(self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.source = validate_cftc_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _candidate(self, entry: Any, feed_id: str, fetched_at: datetime) -> DiscoveryCandidate:
        title = entry.get("title")
        identity = _article_identity(entry.get("link"))
        if not isinstance(title, str) or not title.strip():
            raise ValueError("missing or invalid title")
        if identity is None:
            raise ValueError("missing or invalid official CFTC release URL/identity")
        release_id, article_url = identity
        published_at = _publication_time(entry.get("published"))
        feed = CFTC_FEEDS[feed_id]
        return DiscoveryCandidate(
            title=title.strip(), url=article_url, source=self.source["name"],
            published_at=published_at,
            summary=f"CFTC {feed['name']} release {release_id}.",
            source_type="discovery", source_id=self.source_id, authority_tier=1,
            category=self.source["category"], source_native_id=release_id,
            candidate_id=f"{self.source_id}:{release_id}",
            discovery_method=self.discovery_method, source_url=feed["url"],
            primary_url=article_url, provenance=[feed["url"], article_url],
            document_type="CFTC Press Release",
            collected_at=fetched_at,
            source_native_metadata={"release_number": release_id,
                                    "feed_id": feed_id,
                                    "feed_category": feed["category"],
                                    "feed_name": feed["name"]},
        )

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, self.discovery_method, "not_configured",
                                   fetched_at=fetched_at)

        state = source_state if isinstance(source_state, dict) else {}
        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        state_updates: dict[str, dict[str, str]] = {}
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])

        for feed_id, feed in CFTC_FEEDS.items():
            validators = JsonDiscoveryState.request_validators(state, self.source_id, feed_id)
            try:
                response = self.http.get(
                    feed["url"], etag=validators.get("etag", ""),
                    last_modified=validators.get("last_modified", ""),
                    expected_content_types=("application/rss+xml", "application/xml", "text/xml"),
                )
            except DiscoveryHttpError as exc:
                errors.append(f"{feed['name']}: {exc.kind}"
                              + (f" (HTTP {exc.status_code})" if exc.status_code else ""))
                continue

            if response.status_code == 304:
                state_updates[feed_id] = {
                    key: response.headers.get(key, validators.get(key, ""))
                    for key in ("etag", "last_modified")
                    if response.headers.get(key, validators.get(key, ""))
                }
                continue
            if response.status_code != 200:
                errors.append(f"{feed['name']}: unexpected HTTP status {response.status_code}")
                continue

            parsed = feedparser.parse(response.content)
            malformed_structure = bool(getattr(parsed, "bozo", False))
            complete = not malformed_structure
            if malformed_structure:
                errors.append(f"{feed['name']}: malformed RSS structure")
            if getattr(parsed, "version", "") != "rss20":
                if not malformed_structure:
                    errors.append(f"{feed['name']}: malformed RSS document")
                continue
            entries = list(parsed.entries)
            if len(entries) > self.source["max_items_per_feed"]:
                entries = entries[:self.source["max_items_per_feed"]]
                complete = False
                errors.append(f"{feed['name']}: item limit exceeded; response was truncated")
            for entry in entries:
                try:
                    candidate = self._candidate(entry, feed_id, fetched_at)
                    if candidate.published_at < cutoff:
                        continue
                    existing = candidates.get(candidate.source_native_id)
                    if existing is None:
                        candidates[candidate.source_native_id] = candidate
                    else:
                        if (existing.url != candidate.url or existing.title != candidate.title
                                or existing.published_at != candidate.published_at):
                            raise ValueError("conflicting duplicate CFTC release identity")
                        existing.provenance = list(dict.fromkeys(
                            existing.provenance + candidate.provenance
                        ))
                        existing.source_native_metadata["feed_ids"] = ",".join(dict.fromkeys(
                            filter(None, (existing.source_native_metadata.get("feed_id", ""),
                                          existing.source_native_metadata.get("feed_ids", ""), feed_id))
                        ))
                        existing.source_native_metadata["feed_categories"] = ",".join(dict.fromkeys(
                            filter(None, (existing.source_native_metadata.get("feed_category", ""),
                                          existing.source_native_metadata.get("feed_categories", ""),
                                          feed["category"]))
                        ))
                except (ValueError, TypeError, AttributeError) as exc:
                    complete = False
                    errors.append(f"{feed['name']}: malformed item ({exc})")

            if complete:
                state_updates[feed_id] = {
                    key: response.headers[key]
                    for key in ("etag", "last_modified") if response.headers.get(key)
                }

        ordered = sorted(candidates.values(), key=lambda item: (
            item.published_at or fetched_at, item.source_native_id,
        ), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else (
            "empty" if not ordered else "success"
        )
        return DiscoveryResult(
            self.source_id, self.discovery_method, status, candidates=ordered,
            fetched_at=fetched_at, errors=errors, state_updates=state_updates,
        )
