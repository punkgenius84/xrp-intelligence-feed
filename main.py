import html
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

from collectors.rss import FeedCollectionError, RSSCollector
from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryRegistryError, collect_source,
                                load_discovery_sources)
from discovery.models import DiscoveryCandidate
from discovery.normalization import normalize_candidate
from discord.publisher import publish, settings_from_env
from intelligence.deduplication import deduplicate
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from sources.registry import enabled_sources
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


def run_pipeline(sources=None, state=None, discovery_sources=None, discovery_state=None) -> PipelineResult:
    from intelligence.configuration import (read_object, validate_keyword_groups,
                                            validate_thresholds, validate_word_groups)

    configured_sources = enabled_sources() if sources is None else sources
    run_discovery = sources is None or discovery_sources is not None
    if run_discovery and discovery_sources is None:
        discovery_sources = load_discovery_sources()
    validate_word_groups(read_object("config/entities.json", "entities"),
                         ("assets", "companies", "regulators", "government", "finance"), "entities")
    validate_keyword_groups(read_object("config/keywords.json", "keywords"))
    validate_thresholds(read_object("config/thresholds.json", "thresholds"))
    state = state or JsonState()
    seen = state.load()
    discovery_store = None
    discovery_state_value = None
    discovery_results: list[DiscoveryResult] = []
    if run_discovery:
        discovery_store = discovery_state or JsonDiscoveryState()
        discovery_state_value = discovery_store.load()
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

    if run_discovery:
        for source in discovery_sources:
            result = collect_source(source, discovery_state_value)
            discovery_results.append(result)
            collected.extend(result.candidates)
            JsonDiscoveryState.record_health(
                discovery_state_value,
                result.source_id,
                result.fetched_at,
                result.status,
                len(result.candidates),
                "; ".join(result.errors),
            )
            if result.errors:
                failures.extend(f"{result.source_id}: {error}" for error in result.errors)

    normalized = [
        normalize_candidate(item) if isinstance(item, DiscoveryCandidate) else normalize_item(item)
        for item in collected
    ]
    fresh, seen = deduplicate(normalized, seen)
    for item in fresh:
        detect_entities(item)
        classify_source_quality(item)
        score_relevance(item)
    if discovery_store is not None and discovery_state_value is not None:
        for result in discovery_results:
            federal_register_progress = result.pagination.get("federal_register_terms")
            ofac_progress = result.pagination.get("ofac_recent_actions")
            fincen_progress = result.pagination.get("fincen_press_releases")
            treasury_progress = result.pagination.get("treasury_press_releases")
            fdic_progress = result.pagination.get("fdic_press_releases")
            if result.state_updates or (isinstance(federal_register_progress, dict)
                                        and bool(federal_register_progress)) or (
                    isinstance(ofac_progress, dict) and bool(ofac_progress)) or (
                    isinstance(fincen_progress, dict) and bool(fincen_progress)) or (
                    isinstance(treasury_progress, dict) and bool(treasury_progress)) or (
                    isinstance(fdic_progress, dict) and bool(fdic_progress)):
                source_update_time = result.fetched_at
                watermarks = {}
                for candidate in result.candidates:
                    metadata = candidate.source_native_metadata
                    cik = metadata.get("cik", "")
                    filing_date = metadata.get("filing_date", "")
                    accession = metadata.get("accession_number", "")
                    marker = f"{filing_date}:{accession}"
                    if cik and filing_date:
                        if cik not in watermarks or marker > watermarks[cik]:
                            watermarks[cik] = marker
                JsonDiscoveryState.record_success(
                    discovery_state_value, result.source_id, source_update_time,
                    result.state_updates,
                    watermark=watermarks,
                    pagination=result.pagination,
                )
            for candidate in result.candidates:
                JsonDiscoveryState.observe_candidate(
                    discovery_state_value, candidate.candidate_id,
                    candidate.content_hash, result.fetched_at,
                )
        discovery_store.save(discovery_state_value)
    # Discovery validators/candidate state must persist before discovery items
    # enter the shared seen set. If that save fails, a later run can rediscover them.
    # RSS-only runs retain the same save behavior and state shape as v0.2.
    state.save(seen)
    health = {
        result.source_id: discovery_state_value["sources"][result.source_id].get("health", {})
        for result in discovery_results
        if discovery_state_value is not None
    }
    return PipelineResult(collected, fresh, failures, reports, discovery_results, health)


class _NoSaveState:
    """Wraps a state store so a dry run reads state but never writes it."""

    def __init__(self, inner):
        self._inner = inner

    def load(self):
        return self._inner.load()

    def save(self, *args, **kwargs):
        return None


def main() -> None:
    from intelligence.configuration import read_object, validate_thresholds
    # Validate Discord settings before collecting: a bad setting must not fail the run after
    # state has been saved, because a later run would then re-post items that already went out.
    try:
        discord_settings = settings_from_env()
    except ValueError as exc:
        raise SystemExit(f"Discord configuration error: {exc}") from exc
    try:
        if discord_settings.dry_run:
            # A preview must not mark items as seen, or the live run would find nothing new.
            result = run_pipeline(
                state=_NoSaveState(JsonState()), discovery_state=_NoSaveState(JsonDiscoveryState()))
        else:
            result = run_pipeline()
    except (StateFileError, DiscoveryStateError) as exc:
        raise SystemExit(f"State error: {exc}") from exc
    except DiscoveryRegistryError as exc:
        raise SystemExit(f"Discovery configuration error: {exc}") from exc
    collected, fresh, failures = result.collected, result.fresh, result.failures
    reports, discovery_results = result.reports, result.discovery_results
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
    for result in discovery_results:
        print(f"Discovery source {result.source_id}: {result.status} "
              f"({len(result.candidates)} candidates)")
        health = result.health.get(result.source_id, {})
        if health:
            print(f"  Health: failures={health.get('consecutive_failures', 0)} "
                  f"empty={health.get('consecutive_empty', 0)}")
    # State was saved inside run_pipeline, so posting is at-most-once: a failed post is reported
    # but not retried on the next run.
    publish(relevant, discord_settings)


if __name__ == "__main__":
    main()
