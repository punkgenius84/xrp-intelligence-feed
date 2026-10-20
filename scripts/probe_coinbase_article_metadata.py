from __future__ import annotations

from datetime import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.http import BoundedHttpClient, DiscoveryHttpError


URL = "https://www.coinbase.com/blog/coinbase-opens-stock-trading-to-everyone-in-the-us-partners-with-yahoo-finance-to-power-discovery"
TARGET_PATH = urlsplit(URL).path
HOSTS = {"www.coinbase.com", "coinbase.com"}


def _official_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parts = urlsplit(value.strip())
    return (
        parts.scheme.lower() == "https"
        and parts.hostname is not None
        and parts.hostname.casefold() in HOSTS
        and parts.port in (None, 443)
        and parts.username is None
        and parts.password is None
        and parts.path.rstrip("/") == TARGET_PATH
    )


def _normalize_date(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    # An ISO calendar date is itself a valid official publication date; do not
    # invent a time or timezone for it. Full timestamps require an explicit zone.
    if len(candidate) == 10:
        return parsed.date().isoformat()
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


class _ArticleMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_dates: list[str] = []
        self.itemprop_dates: list[str] = []
        self.jsonld: list[str] = []
        self._jsonld_depth = 0
        self._jsonld_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = {key.casefold(): value for key, value in attrs if key and value is not None}
        if tag.casefold() == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").casefold()
            if key in {"article:published_time", "datepublished", "datepublishedtime"}:
                value = attributes.get("content")
                if value:
                    self.meta_dates.append(value)
            if attributes.get("itemprop", "").casefold() == "datepublished":
                value = attributes.get("content")
                if value:
                    self.itemprop_dates.append(value)
        elif tag.casefold() == "time" and attributes.get("itemprop", "").casefold() == "datepublished":
            value = attributes.get("datetime")
            if value:
                self.itemprop_dates.append(value)
        if tag.casefold() == "script" and attributes.get("type", "").casefold() == "application/ld+json":
            self._jsonld_depth = 1
            self._jsonld_parts = []

    def handle_endtag(self, tag):
        if tag.casefold() == "script" and self._jsonld_depth:
            self.jsonld.append("".join(self._jsonld_parts))
            self._jsonld_depth = 0
            self._jsonld_parts = []

    def handle_data(self, data):
        if self._jsonld_depth:
            self._jsonld_parts.append(data)


def _objects(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _objects(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _objects(graph)


def _jsonld_dates(parser: _ArticleMetadataParser) -> list[str]:
    dates: list[str] = []
    for raw in parser.jsonld:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _objects(value):
            kind = obj.get("@type", [])
            types = {kind.casefold()} if isinstance(kind, str) else {
                item.casefold() for item in kind if isinstance(item, str)
            } if isinstance(kind, list) else set()
            if not types.intersection({"article", "newsarticle", "blogposting", "report"}):
                continue
            url_value = obj.get("url")
            main_entity = obj.get("mainEntityOfPage")
            if isinstance(main_entity, dict):
                main_entity = main_entity.get("@id") or main_entity.get("@url")
            owned_urls = [url_value, main_entity, obj.get("@id")]
            if not any(_official_url(item) for item in owned_urls):
                continue
            published = obj.get("datePublished")
            if isinstance(published, str):
                dates.append(published)
    return dates


def probe_coinbase_article_metadata(http=None) -> str:
    """Read-only official article metadata probe; never writes feed state."""
    client = http or BoundedHttpClient(
        user_agent="XRPIntelligenceFeed/0.4 (+read-only Coinbase article metadata diagnostic)",
        timeout=(3.0, 8.0),
        max_bytes=2_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    try:
        response = client.get(URL, expected_content_types=("text/html",))
    except DiscoveryHttpError as exc:
        suffix = f" http_status={exc.status_code}" if exc.status_code else ""
        return f"status=unavailable reason={exc.kind}{suffix}"
    if response.status_code != 200:
        return f"status=unavailable reason=unexpected_http_status http_status={response.status_code}"
    final = urlsplit(response.url)
    if (
        final.scheme.lower() != "https"
        or final.hostname is None
        or final.hostname.casefold() not in HOSTS
        or final.port not in (None, 443)
        or final.username is not None
        or final.password is not None
        or final.path.rstrip("/") != TARGET_PATH
    ):
        return "status=rejected reason=non_official_final_url"
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/html":
        return f"status=rejected reason=unexpected_content_type content_type={content_type or 'missing'}"
    try:
        html = response.content.decode("utf-8-sig")
        parser = _ArticleMetadataParser()
        parser.feed(html)
        parser.close()
    except (UnicodeDecodeError, ValueError):
        return "status=invalid_page reason=html_decode_or_parse_error"

    # Prefer explicit article metadata; JSON-LD is accepted only when its canonical
    # URL identifies this exact official article. Arbitrary nearby dates are ignored.
    candidates = parser.meta_dates + parser.itemprop_dates + _jsonld_dates(parser)
    normalized = {_normalize_date(value) for value in candidates}
    normalized.discard(None)
    if len(normalized) == 1:
        return f"status=official_metadata_date date={next(iter(normalized))}"
    if len(normalized) > 1:
        return f"status=conflicting_official_metadata_date distinct_dates={len(normalized)}"
    return "status=official_page_date_unresolved"


def main() -> None:
    print(f"Coinbase official article metadata diagnostic: {probe_coinbase_article_metadata()}")


if __name__ == "__main__":
    main()
