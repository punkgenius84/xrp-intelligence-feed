from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import feedparser

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.models import DiscoveryCandidate


METHOD = "google_news_official_rss"
GOOGLE_HOSTS = {"news.google.com"}
MAX_SUMMARY = 500


def validate_google_news_source(source: object) -> dict[str, Any]:
    required = {
        "source_id", "name", "authority_tier", "category", "discovery_method",
        "source_url", "enabled", "lookback_days", "max_items", "publisher_host",
        "publisher_name",
    }
    allowed = required | {"context_hint"}
    if (not isinstance(source, dict) or not required <= set(source)
            or set(source) - allowed):
        raise ValueError(f"Google News official RSS source must contain {sorted(required)} and optional context_hint")
    for key in ("source_id", "name", "category", "discovery_method", "source_url",
                "publisher_host", "publisher_name"):
        if not isinstance(source[key], str) or not source[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if source["discovery_method"] != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if "context_hint" in source:
        hint = source["context_hint"]
        if not isinstance(hint, str) or not hint.strip() or len(hint.strip()) > 200:
            raise ValueError("context_hint must be a non-empty string of at most 200 characters")
    if type(source["authority_tier"]) is not int or source["authority_tier"] != 1:
        raise ValueError("Google News official RSS authority_tier must be 1")
    if type(source["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    if type(source["lookback_days"]) is not int or not 1 <= source["lookback_days"] <= 365:
        raise ValueError("lookback_days must be an integer from 1 to 365")
    if type(source["max_items"]) is not int or not 1 <= source["max_items"] <= 100:
        raise ValueError("max_items must be an integer from 1 to 100")
    host = source["publisher_host"].casefold()
    if not re.fullmatch(r"[a-z0-9.-]+", host) or "." not in host or ".." in host:
        raise ValueError("publisher_host must be a valid hostname")
    parts = urlsplit(source["source_url"])
    query = parse_qs(parts.query).get("q", [""])[0].casefold()
    site_filters = re.findall(r"(?<!\\S)site:([a-z0-9.-]+)(?:/|\\s|$)", query)
    site_operator_count = len(re.findall(r"site:", query))
    broadened_query = bool(re.search(r"\\bor\\b|-site:", query))
    if (parts.scheme != "https" or parts.hostname not in GOOGLE_HOSTS
            or parts.path != "/rss/search" or site_filters != [host]
            or site_operator_count != 1 or broadened_query):
        raise ValueError("source_url must be a Google News RSS search restricted only to publisher_host")
    return source


def _google_link(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.hostname is None
            or parts.hostname.casefold() not in GOOGLE_HOSTS
            or port not in (None, 443) or parts.username is not None or parts.password is not None
            or not parts.path.startswith("/rss/articles/")):
        return None
    return value.strip()


def _published(entry: Any) -> datetime:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if value is None or len(value) < 6:
        raise ValueError("missing or invalid publication date")
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("missing or invalid publication date") from exc


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


class GoogleNewsOfficialRSSDiscovery:
    """Bounded discovery via Google News RSS, restricted to a named official publisher.

    The Google News URL is deliberately retained as the lead/click-through URL.
    source.href is recorded as metadata only; it is not treated as a canonical article URL.
    """

    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_google_news_source(source)
        self.http = http or BoundedHttpClient(user_agent="XRPIntelligenceFeed/0.4 (+bounded Google News RSS discovery)")
        self.now = now

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        fetched_at = fetched_at.astimezone(timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source["source_id"], METHOD, "not_configured", fetched_at=fetched_at)

        try:
            response = self.http.get(
                self.source["source_url"],
                expected_content_types=("application/rss+xml", "application/xml", "text/xml", "application/atom+xml"),
            )
        except DiscoveryHttpError as exc:
            error = f"{self.source['name']}: {exc.kind}" + (
                f" (HTTP {exc.status_code})" if exc.status_code else ""
            )
            return DiscoveryResult(self.source["source_id"], METHOD, "failed", fetched_at=fetched_at, errors=[error])
        if response.status_code != 200:
            return DiscoveryResult(
                self.source["source_id"], METHOD, "failed", fetched_at=fetched_at,
                errors=[f"{self.source['name']}: unexpected HTTP status {response.status_code}"],
            )

        parsed = feedparser.parse(response.content)
        errors: list[str] = []
        complete = not bool(getattr(parsed, "bozo", False))
        if not complete:
            errors.append(f"{self.source['name']}: malformed Google News RSS structure")
        if getattr(parsed, "version", "") not in {"rss20", "rss10", "atom10", "atom"}:
            return DiscoveryResult(self.source["source_id"], METHOD, "failed", fetched_at=fetched_at,
                                   errors=errors + [f"{self.source['name']}: unsupported RSS document"])
        entries = list(getattr(parsed, "entries", []))
        if len(entries) > self.source["max_items"]:
            entries = entries[:self.source["max_items"]]
            complete = False
            errors.append(f"{self.source['name']}: item limit exceeded; response was truncated")

        cutoff = fetched_at - timedelta(days=self.source["lookback_days"])
        candidates: dict[str, DiscoveryCandidate] = {}
        for entry in entries:
            try:
                title = _clean(entry.get("title"))
                link = _google_link(entry.get("link"))
                if not title:
                    raise ValueError("missing title")
                if link is None:
                    raise ValueError("missing valid Google News article link")
                published = _published(entry)
                if published > fetched_at + timedelta(hours=24):
                    raise ValueError("publication date is in the future")
                if published < cutoff:
                    continue
                source_meta = entry.get("source") or {}
                attributed_name = _clean(source_meta.get("title"))
                attributed_url = _clean(source_meta.get("href"))
                if not attributed_name:
                    raise ValueError("missing publisher attribution")
                native_id = link.rstrip("/").rsplit("/", 1)[-1]
                summary_text = _clean(entry.get("summary"))[:MAX_SUMMARY]
                summary = f"Google News discovery; publisher attribution: {attributed_name}."
                context_hint = _clean(self.source.get("context_hint"))
                if context_hint:
                    summary += f" Search context (not article text): {context_hint}."
                if summary_text:
                    summary += f" {summary_text}"
                candidate = DiscoveryCandidate(
                    title=title,
                    url=link,
                    source=self.source["publisher_name"],
                    published_at=published,
                    summary=summary,
                    source_type="discovery",
                    source_id=self.source["source_id"],
                    authority_tier=1,
                    category=self.source["category"],
                    source_native_id=native_id,
                    candidate_id=f"{self.source['source_id']}:{native_id}",
                    discovery_method=METHOD,
                    source_url=self.source["source_url"],
                    document_type="Google News RSS discovery",
                    primary_url=link,
                    discovery_lead_url=link,
                    provenance=[self.source["source_url"], link],
                    collected_at=fetched_at,
                    first_seen_at=fetched_at,
                    last_seen_at=fetched_at,
                    source_native_metadata={
                        "publisher_attribution": attributed_name,
                        "publisher_href": attributed_url[:500],
                        "article_url_status": "google_news_clickthrough_unverified",
                        "query_publisher_host": self.source["publisher_host"],
                        **({"query_context_hint": context_hint[:200]} if context_hint else {}),
                    },
                )
                existing = candidates.get(native_id)
                if existing is not None:
                    if (existing.title != candidate.title or existing.published_at != candidate.published_at
                            or existing.url != candidate.url):
                        raise ValueError("conflicting duplicate Google News article identity")
                    continue
                candidates[native_id] = candidate
            except (ValueError, TypeError, AttributeError) as exc:
                complete = False
                errors.append(f"{self.source['name']}: malformed item ({exc})")

        ordered = sorted(candidates.values(), key=lambda item: (item.published_at, item.source_native_id), reverse=True)
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        return DiscoveryResult(self.source["source_id"], METHOD, status, candidates=ordered,
                               fetched_at=fetched_at, errors=errors)
