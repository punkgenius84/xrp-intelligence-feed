from dataclasses import dataclass
from datetime import datetime, timezone
import re
import feedparser
import requests

from models import NewsItem



def _parse_response_feed(content: bytes, response_headers: dict[str, str]):
    """Parse RSS while repairing an ASCII XML declaration only when bytes prove valid UTF-8."""
    head = content[:512].lower()
    stale_ascii = (
        b'encoding="us-ascii"' in head
        or b"encoding='us-ascii'" in head
    )
    if stale_ascii:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            decoded = re.sub(
                r"encoding\s*=\s*([\"'])us-ascii\1",
                'encoding="utf-8"',
                decoded,
                count=1,
                flags=re.IGNORECASE,
            )
            normalized_headers = dict(response_headers)
            content_type = str(normalized_headers.get("content-type", ""))
            if re.search(r";\s*charset\s*=", content_type, re.IGNORECASE):
                content_type = re.sub(
                    r"(;\s*charset\s*=\s*)[\"']?[^;\"'\\s]+[\"']?",
                    r"\1utf-8",
                    content_type,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                content_type = (content_type + "; charset=utf-8").strip("; ")
            normalized_headers["content-type"] = content_type
            return feedparser.parse(decoded, response_headers=normalized_headers)
    return feedparser.parse(content, response_headers=response_headers)


@dataclass(slots=True)
class CollectionReport:
    source_id: str
    source_name: str
    status: str
    item_count: int = 0
    http_status: int | None = None
    error: str | None = None


class FeedCollectionError(RuntimeError):
    def __init__(self, message: str, *, status: str, http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class RSSCollector:
    def __init__(self, source: dict | str, url: str | None = None,
                 source_type: str = "discovery", *,
                 timeout: tuple[float, float] = (5.0, 20.0)):
        # Retain the original constructor for v0.1.1 callers.
        if isinstance(source, dict):
            self.metadata = dict(source)
        else:
            self.metadata = {
                "source_id": "", "name": source, "url": url or "",
                "authority_tier": 3, "category": source_type,
                "entity_coverage": [], "collection_type": "rss",
            }
        self.name = self.metadata["name"]
        self.url = self.metadata["url"]
        self.source_type = self.metadata.get("category", source_type)
        self.timeout = timeout
        self.last_report = CollectionReport(
            self.metadata.get("source_id", ""), self.name, "not_started"
        )

    def _fail(self, message: str, *, status: str, http_status: int | None = None):
        self.last_report = CollectionReport(
            self.metadata.get("source_id", ""), self.name, status,
            http_status=http_status, error=message,
        )
        raise FeedCollectionError(message, status=status, http_status=http_status)

    def collect(self) -> list[NewsItem]:
        try:
            response = requests.get(
                self.url,
                timeout=self.timeout,
                headers={"User-Agent": "XRPIntelligenceFeed/0.2"},
            )
        except requests.Timeout as exc:
            self._fail(f"{self.name}: request timed out: {exc}", status="timeout")
        except requests.ConnectionError as exc:
            self._fail(f"{self.name}: connection failed: {exc}", status="request_error")
        except requests.RequestException as exc:
            self._fail(f"{self.name}: request failed: {exc}", status="request_error")

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            self._fail(
                f"{self.name}: HTTP {response.status_code}: {exc}",
                status="http_error",
                http_status=response.status_code,
            )

        # feedparser expects normalized lower-case response header names when
        # the document is supplied as bytes rather than fetched by feedparser.
        headers = {str(key).lower(): value for key, value in response.headers.items()}
        headers["content-location"] = response.url
        try:
            feed = _parse_response_feed(response.content, headers)
        except Exception as exc:
            self._fail(f"{self.name}: malformed RSS response: {exc}", status="malformed_feed")

        entries = getattr(feed, "entries", [])
        items = []
        for entry in entries:
            published = None
            parsed_date = (getattr(entry, "published_parsed", None)
                           or getattr(entry, "updated_parsed", None))
            if parsed_date:
                published = datetime(*parsed_date[:6], tzinfo=timezone.utc)
            items.append(NewsItem(
                title=getattr(entry, "title", "").strip(),
                url=getattr(entry, "link", "").strip(),
                source=self.name,
                published_at=published,
                summary=getattr(entry, "summary", "").strip(),
                source_type=self.source_type,
                source_id=self.metadata.get("source_id", ""),
                authority_tier=self.metadata.get("authority_tier", 3),
                category=self.metadata.get("category", self.source_type),
                entity_coverage=list(self.metadata.get("entity_coverage", [])),
            ))
        items = [item for item in items if item.title and item.url]

        http_status = getattr(response, "status_code", None)
        if getattr(feed, "bozo", False):
            error = str(getattr(feed, "bozo_exception", "malformed RSS response"))
            if items:
                self.last_report = CollectionReport(
                    self.metadata.get("source_id", ""), self.name, "partial_malformed",
                    item_count=len(items), http_status=http_status, error=error,
                )
                return items
            self._fail(
                f"{self.name}: malformed RSS response: {error}",
                status="malformed_feed",
                http_status=http_status,
            )

        status = "success" if items else "empty"
        self.last_report = CollectionReport(
            self.metadata.get("source_id", ""), self.name, status,
            item_count=len(items), http_status=http_status,
        )
        return items
