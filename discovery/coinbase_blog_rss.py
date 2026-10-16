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

METHOD = "coinbase_blog_rss"
SOURCE_ID = "coinbase-blog"
SOURCE_URL = "https://www.coinbase.com/blog/rss.xml"
HOSTS = {"www.coinbase.com", "coinbase.com"}
ARTICLE_PATH = re.compile(r"(?:/[a-z]{2}-[a-z]{2})?/blog/(?!landing(?:/|$))[^/?#]+", re.IGNORECASE)


def validate_coinbase_blog_rss_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"Coinbase RSS source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != SOURCE_ID:
        raise ValueError(f"source_id must be {SOURCE_ID!r}")
    if source["discovery_method"] != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if source["source_url"] != SOURCE_URL:
        raise ValueError(f"source_url must be the official Coinbase RSS endpoint {SOURCE_URL}")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("Coinbase authority_tier must be 1")
    if source["category"] != "institutional":
        raise ValueError("Coinbase category must be institutional")
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
    path = parts.path.rstrip("/") or "/"
    if (
        parts.scheme.lower() != "https" or parts.hostname is None
        or parts.hostname.casefold() not in HOSTS or port not in (None, 443)
        or parts.username is not None or parts.password is not None
        or not ARTICLE_PATH.fullmatch(path)
    ):
        return None
    return path.casefold(), f"https://{parts.hostname.casefold()}{path}"


def _published(entry: Any) -> datetime:
    raw = entry.get("published") or entry.get("updated")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                raise ValueError("publication date must include a timezone")
            return parsed.astimezone(timezone.utc)
    parsed_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_struct is not None:
        try:
            return datetime(*parsed_struct[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    raise ValueError("missing or invalid publication date")


class CoinbaseBlogRSSDiscovery:
    discovery_method = METHOD

    def __init__(
        self, source: dict[str, Any], *, http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_coinbase_blog_rss_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4")
        self.now = now

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(SOURCE_ID, METHOD, "not_configured", fetched_at=fetched_at)
        state = source_state if isinstance(source_state, dict) else {}
        validators = JsonDiscoveryState.request_validators(state, SOURCE_ID, "feed")
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        try:
            response = self.http.get(
                SOURCE_URL, etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/rss+xml", "application/atom+xml", "application/xml", "text/xml"),
            )
        except DiscoveryHttpError as exc:
            error = f"Coinbase: {exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else "")
            return DiscoveryResult(SOURCE_ID, METHOD, "failed", fetched_at=fetched_at, errors=[error])
        if response.status_code == 304:
            updates = {"feed": {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }}
            return DiscoveryResult(SOURCE_ID, METHOD, "not_modified", fetched_at=fetched_at, state_updates=updates)
        if response.status_code != 200:
            return DiscoveryResult(
                SOURCE_ID, METHOD, "failed", fetched_at=fetched_at,
                errors=[f"Coinbase: unexpected HTTP status {response.status_code}"],
            )

        parsed = feedparser.parse(response.content)
        errors: list[str] = []
        if getattr(parsed, "bozo", False):
            errors.append("Coinbase: malformed RSS structure")
        if getattr(parsed, "version", "") not in {"rss20", "atom10", "atom"}:
            errors.append("Coinbase: unsupported RSS document")
        entries = list(getattr(parsed, "entries", []))
        if len(entries) > self.source["max_items"]:
            entries = entries[:self.source["max_items"]]
            errors.append("Coinbase: item limit exceeded; response was truncated")

        candidates: dict[str, DiscoveryCandidate] = {}
        for index, entry in enumerate(entries):
            try:
                title = entry.get("title")
                identity = _identity(entry.get("link"))
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("missing or invalid title")
                if identity is None:
                    raise ValueError("missing or invalid official article URL")
                published = _published(entry)
                if published < cutoff:
                    continue
                native_id, url = identity
                title = " ".join(title.split())
                candidate = DiscoveryCandidate(
                    title=title, url=url, source=self.source["name"], published_at=published,
                    summary=f"Coinbase official blog article: {title}", source_type="discovery",
                    source_id=SOURCE_ID, authority_tier=1, category=self.source["category"],
                    source_native_id=native_id, candidate_id=f"{SOURCE_ID}:{native_id}",
                    discovery_method=METHOD, source_url=SOURCE_URL,
                    document_type="Coinbase Blog Article", primary_url=url,
                    provenance=[SOURCE_URL, url], collected_at=fetched_at,
                    first_seen_at=fetched_at, last_seen_at=fetched_at,
                )
                existing = candidates.get(native_id)
                if existing is None:
                    candidates[native_id] = candidate
                elif existing.title != candidate.title or existing.published_at != candidate.published_at:
                    raise ValueError("conflicting duplicate Coinbase article identity")
            except (ValueError, TypeError, AttributeError) as exc:
                errors.append(f"Coinbase: malformed RSS item {index + 1} ({exc})")

        updates = {}
        if not errors:
            updates["feed"] = {
                key: response.headers[key]
                for key in ("etag", "last_modified") if response.headers.get(key)
            }
        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.published_at or fetched_at, item.source_native_id),
            reverse=True,
        )[:self.source["max_items"]]
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        return DiscoveryResult(
            SOURCE_ID, METHOD, status, candidates=ordered, fetched_at=fetched_at,
            errors=errors, state_updates=updates,
        )
