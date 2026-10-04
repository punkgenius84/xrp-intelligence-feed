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


METHOD = "bny_rss"
HOSTS = {"www.bny.com", "bny.com"}
ARTICLE_PATH = r"/(?:corporate/global|content/bnymellon/global)/en/about-us/newsroom/(?:press-release|company-news)/[^/?#]+"

def validate_bny_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise ValueError(f"BNY RSS source must have exactly {sorted(required)}")
    for key in ("source_id", "name", "category", "discovery_method", "source_url"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["source_id"] != "bny-newsroom":
        raise ValueError("source_id must be 'bny-newsroom'")
    if source["discovery_method"] != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if source["source_url"] != "https://www.bny.com/bin/bnymellon/rssFeedGeneratorServlet.report":
        raise ValueError("source_url must be the official BNY newsroom RSS endpoint")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("BNY authority_tier must be 1")
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
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in HOSTS
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not re.fullmatch(ARTICLE_PATH, parts.path, re.IGNORECASE)
    ):
        return None
    path = parts.path
    if path.casefold().startswith("/content/bnymellon/global/"):
        path = "/corporate/global/" + path[len("/content/bnymellon/global/"):]
    return path.casefold(), f"https://{parts.hostname}{path}"

def _published(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing or invalid publication date")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("missing or invalid publication date") from exc
    if parsed.tzinfo is None:
        raise ValueError("publication date must include a timezone")
    return parsed.astimezone(timezone.utc)

class BNYRSSDiscovery:
    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_bny_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4")
        self.now = now

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source["source_id"], METHOD, "not_configured", fetched_at=fetched_at)

        state = source_state if isinstance(source_state, dict) else {}
        validators = JsonDiscoveryState.request_validators(state, self.source["source_id"], "feed")
        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        try:
            response = self.http.get(
                self.source["source_url"],
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/rss+xml", "application/xml", "text/xml"),
            )
        except DiscoveryHttpError as exc:
            error = f"{self.source['name']}: {exc.kind}" + (f" (HTTP {exc.status_code})" if exc.status_code else "")
            return DiscoveryResult(self.source["source_id"], METHOD, "failed", fetched_at=fetched_at, errors=[error])

        if response.status_code == 304:
            updates = {"feed": {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }}
            return DiscoveryResult(self.source["source_id"], METHOD, "empty", fetched_at=fetched_at, state_updates=updates)
        if response.status_code != 200:
            return DiscoveryResult(
                self.source["source_id"], METHOD, "failed", fetched_at=fetched_at,
                errors=[f"{self.source['name']}: unexpected HTTP status {response.status_code}"],
            )

        parsed = feedparser.parse(response.content)
        errors: list[str] = []
        if getattr(parsed, "bozo", False):
            errors.append(f"{self.source['name']}: malformed RSS structure")
        if getattr(parsed, "version", "") not in {"rss20", "atom10", "atom"}:
            errors.append(f"{self.source['name']}: unsupported RSS document")
        entries = list(getattr(parsed, "entries", []))
        if len(entries) > self.source["max_items"]:
            entries = entries[:self.source["max_items"]]
            errors.append(f"{self.source['name']}: item limit exceeded; response was truncated")

        candidates: dict[str, DiscoveryCandidate] = {}
        for entry in entries:
            try:
                title = entry.get("title")
                raw_link = entry.get("link")
                identity = _identity(raw_link)
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("missing or invalid title")
                if identity is None:
                    diagnostic_link = repr(raw_link)[:320]
                    raise ValueError(f"missing or invalid official BNY article URL: {diagnostic_link}")
                published = _published(entry.get("published") or entry.get("updated"))
                if published < cutoff:
                    continue
                native_id, url = identity
                candidate = DiscoveryCandidate(
                    title=title.strip(),
                    url=url,
                    source=self.source["name"],
                    published_at=published,
                    summary=f"{self.source['name']} official newsroom release: {title.strip()}",
                    source_type="discovery",
                    source_id=self.source["source_id"],
                    authority_tier=1,
                    category=self.source["category"],
                    source_native_id=native_id,
                    candidate_id=f"{self.source['source_id']}:{native_id}",
                    discovery_method=METHOD,
                    source_url=self.source["source_url"],
                    document_type="BNY Newsroom Release",
                    primary_url=url,
                    provenance=[self.source["source_url"], url],
                    collected_at=fetched_at,
                    first_seen_at=fetched_at,
                    last_seen_at=fetched_at,
                )
                existing = candidates.get(native_id)
                if existing is None:
                    candidates[native_id] = candidate
                elif existing.title != candidate.title or existing.published_at != candidate.published_at:
                    raise ValueError("conflicting duplicate BNY article identity")
            except (ValueError, TypeError, AttributeError) as exc:
                errors.append(f"{self.source['name']}: malformed item ({exc})")

        updates = {}
        if not errors:
            updates["feed"] = {
                key: response.headers[key]
                for key in ("etag", "last_modified") if response.headers.get(key)
            }
        ordered = sorted(candidates.values(), key=lambda item: (item.published_at, item.source_native_id), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        return DiscoveryResult(
            self.source["source_id"], METHOD, status, candidates=ordered,
            fetched_at=fetched_at, errors=errors, state_updates=updates,
        )
