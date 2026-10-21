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


SOURCE_ID = "ecb-press-releases"
METHOD = "ecb_rss"
FEED_URL = "https://www.ecb.europa.eu/rss/press.html"
HOSTS = {"www.ecb.europa.eu", "ecb.europa.eu"}
MAX_SUMMARY = 500


def validate_ecb_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"ECB source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != SOURCE_ID:
        raise ValueError(f"source_id must be {SOURCE_ID!r}")
    if source["discovery_method"] != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if source["source_url"] != FEED_URL:
        raise ValueError("source_url must be the official ECB press RSS feed")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("ECB authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    return source


def _identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.casefold() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in HOSTS
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not re.fullmatch(r"/press/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+", parts.path)
        or "//" in parts.path
        or "/../" in parts.path
        or parts.path.endswith("/..")
    ):
        return None
    path = parts.path.rstrip("/")
    if not path:
        return None
    return path.casefold(), f"https://www.ecb.europa.eu{path}"


def _published(entry: Any) -> datetime:
    # feedparser normalizes recognized RSS/Atom date tuples to UTC.
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if value is None or len(value) < 6:
        raise ValueError("missing or invalid publication date")
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("missing or invalid publication date") from exc


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


class ECBRSSDiscovery:
    source_id = SOURCE_ID
    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_ecb_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4 (+official ECB RSS reader)")
        self.now = now

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        fetched_at = fetched_at.astimezone(timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source_id, METHOD, "not_configured", fetched_at=fetched_at)

        state = source_state if isinstance(source_state, dict) else {}
        validators = JsonDiscoveryState.request_validators(state, self.source_id, "press_releases")
        cutoff = fetched_at - timedelta(days=self.source["lookback_days"])
        try:
            response = self.http.get(
                FEED_URL,
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/rss+xml", "application/xml", "text/xml", "application/atom+xml"),
            )
        except DiscoveryHttpError as exc:
            error = "ECB press releases: " + exc.kind + (f" (HTTP {exc.status_code})" if exc.status_code else "")
            return DiscoveryResult(self.source_id, METHOD, "failed", fetched_at=fetched_at, errors=[error])

        if response.status_code == 304:
            kept = {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }
            return DiscoveryResult(self.source_id, METHOD, "not_modified", fetched_at=fetched_at,
                                   state_updates={"press_releases": kept})
        if response.status_code != 200:
            return DiscoveryResult(self.source_id, METHOD, "failed", fetched_at=fetched_at,
                                   errors=[f"ECB press releases: unexpected HTTP status {response.status_code}"])

        parsed = feedparser.parse(response.content)
        errors: list[str] = []
        complete = not bool(getattr(parsed, "bozo", False))
        if not complete:
            errors.append("ECB press releases: malformed feed structure")
        if getattr(parsed, "version", "") not in {"rss20", "rss10", "atom10", "atom"}:
            return DiscoveryResult(self.source_id, METHOD, "failed", fetched_at=fetched_at,
                                   errors=errors + ["ECB press releases: unsupported feed document"])

        entries = list(getattr(parsed, "entries", []))
        if len(entries) > self.source["max_items"]:
            entries = entries[:self.source["max_items"]]
            complete = False
            errors.append("ECB press releases: item limit exceeded; response was truncated")

        candidates: dict[str, DiscoveryCandidate] = {}
        for entry in entries:
            try:
                title = _clean(entry.get("title"))
                identity = _identity(entry.get("link"))
                if not title:
                    raise ValueError("missing title")
                if identity is None:
                    raw_link = entry.get("link")
                    raise ValueError(f"missing official ECB press article URL: {str(raw_link)[:240]!r}")
                published = _published(entry)
                if published > fetched_at + timedelta(hours=24):
                    raise ValueError("publication date is in the future")
                if published < cutoff:
                    continue
                native_id, url = identity
                summary_text = _clean(entry.get("summary"))[:MAX_SUMMARY]
                summary = f"European Central Bank press feed: {summary_text}" if summary_text else title
                candidate = DiscoveryCandidate(
                    title=title,
                    url=url,
                    source=self.source["name"],
                    published_at=published,
                    summary=summary,
                    source_type="discovery",
                    source_id=self.source_id,
                    authority_tier=1,
                    category=self.source["category"],
                    source_native_id=native_id,
                    candidate_id=f"{self.source_id}:{native_id}",
                    discovery_method=METHOD,
                    source_url=FEED_URL,
                    document_type="ECB Press Release or Official Communication",
                    primary_url=url,
                    provenance=[FEED_URL, url],
                    collected_at=fetched_at,
                    first_seen_at=fetched_at,
                    last_seen_at=fetched_at,
                )
                existing = candidates.get(native_id)
                if existing is not None:
                    if (existing.title != candidate.title or existing.published_at != candidate.published_at
                            or existing.url != candidate.url):
                        raise ValueError("conflicting duplicate ECB article identity")
                    continue
                candidates[native_id] = candidate
            except (ValueError, TypeError, AttributeError) as exc:
                complete = False
                errors.append(f"ECB press releases: malformed item ({exc})")

        updates = {}
        if complete:
            updates["press_releases"] = {
                key: response.headers[key]
                for key in ("etag", "last_modified")
                if response.headers.get(key)
            }
        ordered = sorted(candidates.values(), key=lambda item: (item.published_at, item.source_native_id), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        return DiscoveryResult(self.source_id, METHOD, status, candidates=ordered,
                               fetched_at=fetched_at, errors=errors, state_updates=updates)
