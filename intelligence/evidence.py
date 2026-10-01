from __future__ import annotations

from dataclasses import dataclass
from models import NewsItem

@dataclass(frozen=True, slots=True)
class Evidence:
    source_id: str
    source: str
    url: str
    published_at: str
    title: str
    source_quality: str

def evidence_from_item(item: NewsItem) -> Evidence:
    published = item.published_at or item.collected_at
    return Evidence(source_id=item.source_id, source=item.source, url=item.url, published_at=published.isoformat(), title=item.title, source_quality=item.source_quality)

def build_evidence_bundle(items: list[NewsItem]) -> list[Evidence]:
    seen: set[tuple[str, str]] = set()
    bundle: list[Evidence] = []
    for item in items:
        key = (item.source_id or item.source, item.url)
        if key in seen:
            continue
        seen.add(key)
        bundle.append(evidence_from_item(item))
    return bundle
