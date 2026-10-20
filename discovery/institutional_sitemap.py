from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import xml.etree.ElementTree as ET
from typing import Any, Callable
from urllib.parse import urlsplit

from discovery.base import DiscoveryResult
from discovery.http import BoundedHttpClient, DiscoveryHttpError
from discovery.institutional_press import (
    METHOD as HTML_METHOD,
    _official_article,
    _parse_article_detail,
    validate_institutional_source,
)
from discovery.models import DiscoveryCandidate
from storage.discovery_state import JsonDiscoveryState


METHOD = "institutional_press_sitemap"
MAX_SITEMAP_BYTES = 2_000_000
MAX_SITEMAPS = 3
MAX_DETAIL_FETCHES = 10
def _sitemap_request_id(source_url: str) -> str:
    return "sitemap:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]


def _safe_sitemap_url(source: dict[str, Any], value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parts = urlsplit(value.strip())
    hosts = {host.casefold() for host in source["allowed_hosts"]}
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.casefold() not in hosts
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith("/global/")
        or parts.query
        or parts.fragment
    ):
        return None
    return value.strip()


def _parse_sitemap(content: bytes, source: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    if len(content) > MAX_SITEMAP_BYTES or b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        return "oversize_or_unsafe_xml", []
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return "invalid_xml", []
    kind = root.tag.rsplit("}", 1)[-1].casefold()
    if kind not in {"urlset", "sitemapindex"}:
        return "unexpected_root", []
    entries = []
    for node in list(root):
        fields = {
            child.tag.rsplit("}", 1)[-1].casefold(): (child.text or "").strip()
            for child in list(node)
        }
        loc = _safe_sitemap_url(source, fields.get("loc"))
        if loc:
            entries.append((loc, fields.get("lastmod", "")))
    return kind, entries


def _lastmod_key(value: str) -> datetime:
    """Parse sitemap lastmod for fetch ordering only; never use it as publication time."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _article_entries(source: dict[str, Any], entries: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    by_id: dict[str, tuple[str, str, str]] = {}
    for url, lastmod in entries:
        safe = _official_article(source, url)
        if safe is None:
            continue
        by_id.setdefault(safe[1], (safe[0], lastmod, safe[1]))
    return sorted(
        by_id.values(),
        key=lambda item: (_lastmod_key(item[1]), item[0]),
        reverse=True,
    )


def validate_institutional_sitemap_source(source: object) -> dict[str, Any]:
    if not isinstance(source, dict) or source.get("discovery_method") != METHOD:
        raise ValueError(f"discovery_method must be {METHOD!r}")
    if "sitemap_detail_limit" not in source:
        raise ValueError("sitemap_detail_limit is required")
    if (
        type(source["sitemap_detail_limit"]) is not int
        or not 1 <= source["sitemap_detail_limit"] <= MAX_DETAIL_FETCHES
    ):
        raise ValueError(f"sitemap_detail_limit must be an integer from 1 to {MAX_DETAIL_FETCHES}")
    base = {key: value for key, value in source.items() if key != "sitemap_detail_limit"}
    base["discovery_method"] = HTML_METHOD
    validate_institutional_source(base)
    sitemap_url = _safe_sitemap_url(source, source.get("source_url"))
    if sitemap_url is None or not urlsplit(sitemap_url).path.casefold().endswith(".xml"):
        raise ValueError("source_url must be an official HTTPS XML sitemap under /global/")
    return source


class InstitutionalPressSitemapDiscovery:
    """Bounded sitemap-backed press-release discovery with explicit article-date extraction."""

    discovery_method = METHOD

    def __init__(
        self,
        source: dict[str, Any],
        *,
        http: BoundedHttpClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.source = validate_institutional_sitemap_source(source)
        self.http = http or BoundedHttpClient(
            user_agent="XRPIntelligenceFeed/0.4",
            max_bytes=MAX_SITEMAP_BYTES,
        )
        self.now = now

    def _candidate(self, row: dict[str, Any], fetched_at: datetime) -> DiscoveryCandidate:
        return DiscoveryCandidate(
            title=row["title"],
            url=row["url"],
            source=self.source["name"],
            published_at=row["date"],
            summary=f"{self.source['name']} official release: {row['title']}",
            source_type="discovery",
            source_id=self.source["source_id"],
            authority_tier=1,
            category=self.source["category"],
            source_native_id=row["native_id"],
            candidate_id=f"{self.source['source_id']}:{row['native_id']}",
            discovery_method=METHOD,
            source_url=self.source["source_url"],
            document_type=f"{self.source['name']} Press Release",
            primary_url=row["url"],
            provenance=[self.source["source_url"], row["url"]],
            collected_at=fetched_at,
            first_seen_at=fetched_at,
            last_seen_at=fetched_at,
        )

    def collect(self, source_state: dict[str, Any] | None = None) -> DiscoveryResult:
        fetched_at = self.now()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if not self.source["enabled"]:
            return DiscoveryResult(self.source["source_id"], METHOD, "not_configured", fetched_at=fetched_at)

        state = source_state if isinstance(source_state, dict) else {}
        source_state_entry = state.get("sources", {}).get(self.source["source_id"], {})
        if not isinstance(source_state_entry, dict):
            source_state_entry = {}
        previous_pagination = source_state_entry.get("pagination", {})
        if not isinstance(previous_pagination, dict):
            previous_pagination = {}
        previous_digest = previous_pagination.get("citi_sitemap_sha256")
        request_id = _sitemap_request_id(self.source["source_url"])
        validators = JsonDiscoveryState.request_validators(state, self.source["source_id"], request_id)
        errors: list[str] = []
        updates: dict[str, dict[str, str]] = {}

        try:
            response = self.http.get(
                self.source["source_url"],
                etag=validators.get("etag", ""),
                last_modified=validators.get("last_modified", ""),
                expected_content_types=("application/xml", "text/xml", "application/octet-stream"),
            )
        except DiscoveryHttpError as exc:
            suffix = f" (HTTP {exc.status_code})" if exc.status_code else f" ({exc.kind})"
            return DiscoveryResult(
                self.source["source_id"], METHOD, "failed", fetched_at=fetched_at,
                errors=[f"{self.source['name']}: sitemap request failed{suffix}"],
            )

        if response.status_code == 304:
            updates[request_id] = {
                key: response.headers.get(key, validators.get(key, ""))
                for key in ("etag", "last_modified")
                if response.headers.get(key, validators.get(key, ""))
            }
            return DiscoveryResult(
                self.source["source_id"], METHOD, "not_modified",
                fetched_at=fetched_at, state_updates=updates,
            )

        if response.status_code != 200 or _safe_sitemap_url(self.source, response.url) is None:
            return DiscoveryResult(
                self.source["source_id"], METHOD, "failed", fetched_at=fetched_at,
                errors=[f"{self.source['name']}: sitemap returned invalid status or final URL"],
            )

        sitemap_urls = [(response.url, response.content)]
        sitemap_content_parts = [response.content]
        visited = {response.url}
        article_entries: list[tuple[str, str]] = []
        for sitemap_url, content in sitemap_urls:
            kind, entries = _parse_sitemap(content, self.source)
            if kind == "urlset":
                article_entries.extend(entries)
                continue
            if kind != "sitemapindex":
                errors.append(f"{self.source['name']}: invalid sitemap XML ({kind})")
                continue
            child_urls = sorted(
                entries,
                key=lambda item: (_lastmod_key(item[1]), item[0]),
                reverse=True,
            )
            for child_url, _ in child_urls:
                if child_url in visited or len(visited) >= MAX_SITEMAPS:
                    continue
                visited.add(child_url)
                try:
                    child = self.http.get(
                        child_url,
                        expected_content_types=("application/xml", "text/xml", "application/octet-stream"),
                    )
                except DiscoveryHttpError as exc:
                    suffix = f" (HTTP {exc.status_code})" if exc.status_code else f" ({exc.kind})"
                    errors.append(f"{self.source['name']}: child sitemap request failed{suffix}")
                    continue
                if child.status_code != 200 or _safe_sitemap_url(self.source, child.url) is None:
                    errors.append(f"{self.source['name']}: child sitemap returned invalid status or final URL")
                    continue
                sitemap_content_parts.append(child.content)
                child_kind, child_entries = _parse_sitemap(child.content, self.source)
                if child_kind == "urlset":
                    article_entries.extend(child_entries)
                else:
                    errors.append(f"{self.source['name']}: child sitemap has invalid XML ({child_kind})")
                if len(visited) >= MAX_SITEMAPS:
                    break

        digest = hashlib.sha256(b"\n".join(sitemap_content_parts)).hexdigest()
        if previous_digest == digest and not errors:
            return DiscoveryResult(
                self.source["source_id"], METHOD, "empty", fetched_at=fetched_at,
                pagination={"citi_sitemap_sha256": digest},
                state_updates={
                    request_id: {
                        key: response.headers[key]
                        for key in ("etag", "last_modified") if response.headers.get(key)
                    }
                },
            )

        selected = _article_entries(self.source, article_entries)[
            :min(self.source["sitemap_detail_limit"], self.source["max_items"], MAX_DETAIL_FETCHES)
        ]
        if not selected:
            errors.append(f"{self.source['name']}: sitemap contains no allowlisted press-release routes")

        cutoff = fetched_at.astimezone(timezone.utc) - timedelta(days=self.source["lookback_days"])
        future_cutoff = fetched_at.astimezone(timezone.utc) + timedelta(days=1)
        candidates: dict[str, DiscoveryCandidate] = {}
        for url, _lastmod, native_id in selected:
            try:
                detail = self.http.get(url, expected_content_types=("text/html", "application/xhtml+xml"))
            except DiscoveryHttpError as exc:
                suffix = f" (HTTP {exc.status_code})" if exc.status_code else f" ({exc.kind})"
                errors.append(f"{self.source['name']}: article detail request failed{suffix}")
                continue
            final_safe = _official_article(self.source, detail.url)
            requested_safe = _official_article(self.source, url)
            if (
                detail.status_code != 200
                or final_safe is None
                or requested_safe is None
                or final_safe[1] != requested_safe[1]
            ):
                errors.append(f"{self.source['name']}: article detail returned invalid status or final URL")
                continue
            row = _parse_article_detail(self.source, url, detail.content)
            if row is None or row["native_id"] != native_id:
                errors.append(f"{self.source['name']}: article detail lacked grounded title/date metadata")
                continue
            published_at = row["date"].astimezone(timezone.utc)
            if published_at > future_cutoff:
                errors.append(f"{self.source['name']}: rejected future-dated official article")
                continue
            if published_at < cutoff:
                continue
            candidate = self._candidate(row, fetched_at)
            candidates[candidate.source_native_id] = candidate

        ordered = sorted(
            candidates.values(),
            key=lambda item: (item.published_at or fetched_at, item.source_native_id),
            reverse=True,
        )[:self.source["max_items"]]
        status = "partial" if errors and ordered else "failed" if errors else "empty" if not ordered else "success"
        if status in {"success", "empty"}:
            updates[request_id] = {
                key: response.headers[key]
                for key in ("etag", "last_modified") if response.headers.get(key)
            }
        pagination = {"citi_sitemap_sha256": digest} if status in {"success", "empty"} else {}
        return DiscoveryResult(
            self.source["source_id"], METHOD, status, candidates=ordered,
            fetched_at=fetched_at, errors=errors, state_updates=updates,
            pagination=pagination,
        )
