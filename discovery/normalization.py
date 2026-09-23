from __future__ import annotations

import html
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from discovery.models import DiscoveryCandidate, stable_candidate_id


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def canonicalize_url(value: str) -> str:
    """Normalize only harmless URL differences; retain query parameters and path case."""
    parts = urlsplit(value.strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("discovery candidate URL must use HTTPS")
    host = parts.hostname.lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit(("https", host, parts.path or "/", parts.query, ""))


def normalize_candidate(candidate: DiscoveryCandidate) -> DiscoveryCandidate:
    candidate.title = unicodedata.normalize("NFKC", " ".join(candidate.title.split()))
    candidate.summary = unicodedata.normalize("NFKC", plain_text(candidate.summary))
    candidate.url = canonicalize_url(candidate.url)
    if candidate.primary_url:
        candidate.primary_url = canonicalize_url(candidate.primary_url)
    if candidate.source_url:
        candidate.source_url = canonicalize_url(candidate.source_url)
    if candidate.discovery_lead_url:
        candidate.discovery_lead_url = canonicalize_url(candidate.discovery_lead_url)
    candidate.provenance = list(dict.fromkeys(
        canonicalize_url(url) for url in candidate.provenance
    ))
    if not candidate.candidate_id:
        candidate.candidate_id = stable_candidate_id(
            candidate.source_id, candidate.source_native_id,
            canonical_url=candidate.url, content_hash=candidate.content_hash,
        )
    return candidate
