from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable
from urllib.parse import urlsplit

import feedparser

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


BIS_SOURCE_ID = "bis-media-releases"
BIS_METHOD = "bis_media_releases"
BIS_FEED_ID = "media_releases"
BIS_FEED_URL = "https://www.bis.org/doclist/all_pressrels.rss"
BIS_HOSTS = {"www.bis.org", "bis.org"}
BIS_NAME = "Bank for International Settlements"

# Official BIS release URLs look like /media-releases/20260923-some-slug. The slug,
# including its date prefix, is the native identity; feed GUIDs are never trusted.
_ARTICLE_PATH = re.compile(r"^/media-releases/(\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*)$")
_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_SUMMARY_LIMIT = 500


def validate_bis_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"BIS source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != BIS_SOURCE_ID:
        raise ValueError(f"source_id must be {BIS_SOURCE_ID!r}")
    if source["discovery_method"] != BIS_METHOD:
        raise ValueError(f"discovery_method must be {BIS_METHOD!r}")
    if source["source_url"] != BIS_FEED_URL:
        raise ValueError("source_url must be the official BIS media releases RSS feed")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("BIS authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    return source


def _article_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    match = _ARTICLE_PATH.fullmatch(parts.path)
    try:
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in BIS_HOSTS
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not match
    ):
        return None
    native_id = match.group(1)
    return native_id, f"https://www.bis.org/media-releases/{native_id}"


def _publication_time(value: object) -> datetime:
    """Parse the feed's dc:date exactly as given; never invent a time of day."""
    if not isinstance(value, str) or not _ISO_DATETIME.fullmatch(value.strip()):
        raise ValueError("missing or invalid publication date")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("missing or invalid publication date") from exc
    if parsed.tzinfo is None:
        raise ValueError("publication date must include a timezone")
    return parsed.astimezone(timezone.utc)


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


class BISMediaReleasesDiscovery:
    source_id = BIS_SOURCE_ID
    discovery_method = BIS_METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_bis_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.3")
        self.now = now

    def _candidate(self, entry: Any, fetched_at: datetime) -> DiscoveryCandidate:
        title = _clean(entry.get("title"))
        identity = _article_identity(entry.get("link"))
        if not title:
            raise ValueError("missing or invalid title")
        if identity is None:
            raise ValueError("missing or invalid official BIS release URL/identity")
        native_id, article_url = identity
        # feedparser exposes the RSS 1.0 dc:date element as "updated".
        published_at = _publication_time(entry.get("updated"))
        description = _clean(entry.get("summary"))[:_SUMMARY_LIMIT]
        summary = f"{BIS_NAME} media release: {description}" if description else (
            f"{BIS_NAME} media release {native_id}.")
        return DiscoveryCandidate(
            title=title,
            url=article_url,
            source=self.source["name"],
            published_at=published_at,
            summary=summary,
            source_type="discovery",
            source_id=self.source_id,
            authority_tier=1,
            category=self.source["category"],
            source_native_id=native_id,
            candidate_id=f"{self.source_id}:{native_id}",
            discovery_method=self.discovery_method,
            source_url=BIS_FEED_URL,
            primary_url=article_url,
            provenance=[BIS_FEED_URL, article_url],
            document_type="BIS Media Release",
            collected_at=fetched_at,
            source_native_metadata={
                "release_id": native_id,
                "feed_id": BIS_FEED_ID,
                "feed_name": "Media releases",
            },
        )

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(
                self.source_id, self.discovery_method, "not_configured",
                fetched_at=fetched_at,
            )

        state = source_state if isinstance(source_state, dict) else {}
        candidates: dict[str, DiscoveryCandidate] = {}
        errors: list[str] = []
        state_updates: dict[str, dict[str, str]] = {}
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        validators = JsonDiscoveryState.request_validators(state, self.source_id, BIS_FEED_ID)

        try:
            response = self.http.get(
                BIS_FEED_URL,
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/rss+xml", "application/xml", "text/xml"),
            )
        except DiscoveryHttpError as exc:
            errors.append(
                f"BIS media releases: {exc.kind}"
                + (f" (HTTP {exc.status_code})" if exc.status_code else "")
            )
            return DiscoveryResult(self.source_id, self.discovery_method, "failed",
                                   fetched_at=fetched_at, errors=errors)

        if response.status_code == 304:
            kept = {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }
            return DiscoveryResult(self.source_id, self.discovery_method, "empty",
                                   fetched_at=fetched_at, state_updates={BIS_FEED_ID: kept})
        if response.status_code != 200:
            errors.append(f"BIS media releases: unexpected HTTP status {response.status_code}")
            return DiscoveryResult(self.source_id, self.discovery_method, "failed",
                                   fetched_at=fetched_at, errors=errors)

        parsed = feedparser.parse(response.content)
        complete = not bool(getattr(parsed, "bozo", False))
        if not complete:
            errors.append("BIS media releases: malformed RSS structure")
        if getattr(parsed, "version", "") != "rss10":
            if complete:
                errors.append("BIS media releases: malformed RSS document")
            return DiscoveryResult(self.source_id, self.discovery_method, "failed",
                                   fetched_at=fetched_at, errors=errors)

        entries = list(parsed.entries)
        if len(entries) > self.source["max_items"]:
            entries = entries[:self.source["max_items"]]
            complete = False
            errors.append("BIS media releases: item limit exceeded; response was truncated")

        for entry in entries:
            try:
                candidate = self._candidate(entry, fetched_at)
                existing = candidates.get(candidate.source_native_id)
                if existing is not None:
                    if (existing.url != candidate.url or existing.title != candidate.title
                            or existing.published_at != candidate.published_at):
                        raise ValueError("conflicting duplicate BIS release identity")
                    continue
                if candidate.published_at < cutoff:
                    continue
                candidates[candidate.source_native_id] = candidate
            except (ValueError, TypeError, AttributeError) as exc:
                complete = False
                errors.append(f"BIS media releases: malformed item ({exc})")

        if complete:
            state_updates[BIS_FEED_ID] = {
                key: response.headers[key]
                for key in ("etag", "last_modified")
                if response.headers.get(key)
            }

        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.published_at or fetched_at, item.source_native_id),
            reverse=True,
        )
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
        )
