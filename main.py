import html
import re
import unicodedata
from html.parser import HTMLParser

from collectors.rss import FeedCollectionError, RSSCollector
from intelligence.deduplication import deduplicate
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from sources.registry import enabled_sources
from storage.database import JsonState, StateFileError


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


def run_pipeline(sources=None, state=None):
    from intelligence.configuration import (read_object, validate_keyword_groups,
                                            validate_thresholds, validate_word_groups)

    configured_sources = enabled_sources() if sources is None else sources
    validate_word_groups(read_object("config/entities.json", "entities"),
                         ("assets", "companies", "regulators", "government", "finance"), "entities")
    validate_keyword_groups(read_object("config/keywords.json", "keywords"))
    validate_thresholds(read_object("config/thresholds.json", "thresholds"))
    state = state or JsonState()
    seen = state.load()
    collected = []
    failures = []
    reports = []
    for source in configured_sources:
        collector = None
        try:
            collection_type = source.get("collection_type", "rss")
            collector_factory = {"rss": RSSCollector}.get(collection_type)
            if collector_factory is None:
                raise ValueError(f"Unsupported collection_type {collection_type!r} for {source.get('source_id', 'unknown source')}")
            collector = collector_factory(source)
            items = collector.collect()
            collected.extend(items)
            reports.append(collector.last_report)
        except FeedCollectionError as exc:
            failures.append(str(exc))
            reports.append(collector.last_report if collector is not None else None)

    normalized = [normalize_item(item) for item in collected]
    fresh, seen = deduplicate(normalized, seen)
    for item in fresh:
        detect_entities(item)
        classify_source_quality(item)
        score_relevance(item)
    # Persist only after all intelligence processing completed successfully.
    state.save(seen)
    run_pipeline.last_reports = reports
    return collected, fresh, failures


def main() -> None:
    from intelligence.configuration import read_object, validate_thresholds
    try:
        collected, fresh, failures = run_pipeline()
    except StateFileError as exc:
        raise SystemExit(f"State error: {exc}") from exc
    reports = getattr(run_pipeline, "last_reports", [])
    publish_score = validate_thresholds(read_object("config/thresholds.json", "thresholds"))["publish_score"]
    relevant = [item for item in fresh if item.relevance_score >= publish_score]
    print(f"Collected: {len(collected)} | New: {len(fresh)} | Relevant: {len(relevant)}")
    for item in relevant:
        print(f"[{item.relevance_score}] {item.title} — {item.source} "
              f"| entities: {', '.join(item.detected_entities) or 'none'} "
              f"| quality: {item.source_quality}")
        for reason in item.score_reasons:
            print(f"  - {reason}")
    for failure in failures:
        print(f"Source failed: {failure}")
    for report in reports:
        if report is not None:
            print(f"Source {report.source_name}: {report.status} ({report.item_count} items)"
                  + (f"; HTTP {report.http_status}" if report.http_status else "")
                  + (f"; {report.error}" if report.error else ""))


if __name__ == "__main__":
    main()
