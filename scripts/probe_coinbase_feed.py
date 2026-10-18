from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discovery.http import BoundedHttpClient, DiscoveryHttpError


BASE = "https://www.coinbase.com/blog"
CANDIDATES = (BASE + "/rss.xml", BASE + "/feed", BASE)


class _AlternateFeedParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        rel = {part.casefold() for part in values.get("rel", "").split()}
        kind = values.get("type", "").split(";", 1)[0].strip().casefold()
        if "alternate" in rel and (
            kind in {"application/rss+xml", "application/atom+xml", "application/rdf+xml"}
            or values.get("href", "").lower().endswith((".rss", ".xml", "/feed"))
        ):
            self.hrefs.append(values.get("href", ""))


def _is_coinbase_https_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and parts.hostname is not None
            and parts.hostname.casefold() in {"coinbase.com", "www.coinbase.com"}
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def inspect_feed(payload: bytes, content_type: str) -> dict[str, int | str | bool]:
    """Classify a response as a real RSS/Atom/RDF feed without trusting its URL."""
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if len(payload) > 1_000_000:
        return {"valid": False, "reason": "body_too_large", "items": 0, "dated_items": 0}
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, ValueError):
        return {"valid": False, "reason": "invalid_xml", "items": 0, "dated_items": 0}

    root_name = root.tag.rsplit("}", 1)[-1].casefold()
    if root_name not in {"rss", "feed", "rdf"}:
        return {"valid": False, "reason": "unexpected_xml_root", "items": 0, "dated_items": 0}
    # Reject XML wrappers that are actually HTML or generic error payloads.
    if media_type in {"text/html", "application/xhtml+xml"}:
        return {"valid": False, "reason": "html_content_type", "items": 0, "dated_items": 0}

    entries = [
        node for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].casefold() in {"item", "entry"}
    ]
    dated = 0
    for entry in entries:
        fields = {
            node.tag.rsplit("}", 1)[-1].casefold()
            for node in entry.iter()
        }
        if fields.intersection({"pubdate", "published", "updated", "date", "issued"}):
            dated += 1
    return {
        "valid": True,
        "reason": root_name,
        "items": len(entries),
        "dated_items": dated,
    }


def probe() -> int:
    client = BoundedHttpClient(
        user_agent="XRPIntelligenceFeed-ReadOnlyProbe/1.0 (GitHub Actions)",
        timeout=(4.0, 8.0),
        max_bytes=1_000_000,
        max_redirects=2,
        max_attempts=1,
    )
    pending = list(CANDIDATES)
    checked: set[str] = set()
    while pending and len(checked) < 8:
        url = pending.pop(0)
        if url in checked or not _is_coinbase_https_url(url):
            continue
        checked.add(url)
        try:
            response = client.get(url)
        except DiscoveryHttpError as exc:
            print(f"probe url={url} result=unavailable kind={exc.kind} status={exc.status_code or 'n/a'}")
            continue

        content_type = response.headers.get("content-type", "")
        parsed = inspect_feed(response.content, content_type)
        print(
            f"probe url={url} status={response.status_code} "
            f"content_type={content_type.split(';', 1)[0] or 'missing'} "
            f"valid_feed={parsed['valid']} format={parsed['reason']} "
            f"items={parsed['items']} dated_items={parsed['dated_items']}"
        )
        if parsed["valid"]:
            continue

        if "html" in content_type.casefold() or response.content.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            parser = _AlternateFeedParser()
            try:
                parser.feed(response.content.decode("utf-8", errors="replace"))
                parser.close()
            except (ValueError, AssertionError):
                continue
            for href in parser.hrefs:
                discovered = urljoin(url, href)
                if _is_coinbase_https_url(discovered) and discovered not in checked and discovered not in pending:
                    pending.append(discovered)

    print(f"probe_complete checked={len(checked)} mode=read_only publishing=disabled state_write=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())
