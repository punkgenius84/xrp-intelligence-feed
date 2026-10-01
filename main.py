import html
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

from collectors.rss import FeedCollectionError, RSSCollector
from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryDispatchError, DiscoveryRegistryError,
                                collect_source, load_discovery_sources)
from discovery.models import DiscoveryCandidate
from discovery.normalization import normalize_candidate
from discord.publisher import publish, settings_from_env
from intelligence.deduplication import deduplicate
from intelligence.correlation import correlate
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from sources.registry import SourceRegistryError, enabled_sources
from storage.database import JsonState, StateFileError
from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState


@dataclass(slots=True)
class PipelineResult:
    collected: list = field(default_factory=list)
    fresh: list = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reports: list = field(default_factory=list)
    discovery_results: list[DiscoveryResult] = field(default_factory=list)
    health: dict[str, dict] = field(default_factory=dict)



class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    return " ".join(html.unescape(" ".join(parser.parts)).split())


def normalize_item(item):
    item.title = unicodedata.normalize("NFKC", " ".join(item.title.split()))
    if item.source:
        suffix = re.compile(r"(?:\s*[|—–-]\s*)" + re.escape(item.source) + r"\s*$", re.IGNORECASE)
        item.title = suffix.sub("", item.title).strip()
    item.url = item.url.strip()
    item.summary = unicodedata.normalize("NFKC", _plain_text(item.summary))
    return item


# Pagination keys that hold a resumable cursor. Other pagination entries (page_fetches,
# requests_made, ...) are per-run telemetry and must not be persisted on their own.
CURSOR_PAGINATION_KEYS = (
    "federal_register_terms", "ofac_recent_actions", "fincen_press_releases",
    "treasury_press_releases", "fdic_press_releases",
)


def _has_cursor_progress(pagination: dict) -> bool:
    return any(isinstance(pagination.get(key), dict) and bool(pagination.get(key))
               for key in CURSOR_PAGINATION_KEYS)

