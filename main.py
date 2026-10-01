import html
import re
import unicodedata
from datetime import datetime
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
from intelligence.correlation import build_correlation_card, correlate
from intelligence.buried_signals import detect_buried_signals
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from sources.registry import SourceRegistryError, enabled_sources
from storage.correlation_state import CorrelationStateError, JsonCorrelationState
from storage.database import JsonState, StateFileError
from storage.outbox import JsonOutboxState, OutboxError
from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState


# Only these pagination keys represent durable continuation progress. Fetch counters
# and page-count telemetry must never become a false frontier.
_PAGINATION_PROGRESS_KEYS = frozenset({
    "federal_register_terms",
    "ofac_recent_actions",
    "fincen_press_releases",
    "fdic_press_releases",
    "treasury_press_releases",
    "next_issuer_index",
})


def _has_pagination_progress(pagination: object) -> bool:
    return isinstance(pagination, dict) and bool(
        _PAGINATION_PROGRESS_KEYS.intersection(pagination)
    )


@dataclass(slots=True)
class PipelineResult:
    collected: list = field(default_factory=list)
    fresh: list = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reports: list = field(default_factory=list)
    discovery_results: list[DiscoveryResult] = field(default_factory=list)
    health: dict[str, dict] = field(default_factory=dict)
    buried_signals: list = field(default_factory=list)
    publishable: list = field(default_factory=list)


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


def run_pipeline(
    sources=None,
    state=None,
    discovery_sources=None,
    discovery_state=None,
    correlation_state=None,
    outbox_state=None,
) -> PipelineResult:
    from intelligence.configuration import (read_object, validate_keyword_groups,
                                            validate_thresholds, validate_word_groups)

    configured_sources = enabled_sources() if sources is None else sources
    run_discovery = sources is None or discovery_sources is not None
    if run_discovery and discovery_sources is None:
        discovery_sources = load_discovery_sources()
    validate_word_groups(read_object("config/entities.json", "entities"),
                         ("assets", "companies", "regulators", "government", "finance"), "entities")
    validate_keyword_groups(read_object("config/keywords.json", "keywords"))
    thresholds = validate_thresholds(read_object("config/thresholds.json", "thresholds"))
    state = state or JsonState()
    seen = state.load()

    correlation_store = correlation_state or JsonCorrelationState()
    correlation_state_value = correlation_store.load()

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
                raise ValueError(
                    f"Unsupported collection_type {collection_type!r} for "
                    f"{source.get('source_id', 'unknown source')}"
                )
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

    # Refresh cards already in memory even when deduplication says an item is not fresh.
    # A stable candidate ID with a new content hash is an update to the same event, not
    # a second event. Existing cards are never recreated merely because they were observed.
    for item in normalized:
        candidate_id = item.candidate_id or item.fingerprint
        existing = correlation_state_value["cards"].get(candidate_id)
        if existing is None:
            continue
        if existing["content_hash"] != item.content_hash:
            detect_entities(item)
            card = build_correlation_card(item)
            JsonCorrelationState.upsert_card(
                correlation_state_value,
                candidate_id=card["candidate_id"],
                source_id=card["source_id"],
                published_at=datetime.fromisoformat(
                    card["published_at"].replace("Z", "+00:00")
                ),
                high_value_entities=card["high_value_entities"],
                title_tokens=card["title_tokens"],
                content_hash=card["content_hash"],
                last_seen=datetime.fromisoformat(
                    card["last_seen"].replace("Z", "+00:00")
                ),
            )
        else:
            JsonCorrelationState.touch_card(
                correlation_state_value,
                candidate_id=candidate_id,
                last_seen=item.collected_at,
            )

    # Correlation is deliberately downstream of relevance scoring: it enriches context
    # without changing whether an item qualifies for publication.
    correlate(fresh, history=list(correlation_state_value["cards"].values()))
    buried_signals = detect_buried_signals(
        fresh, publish_score=thresholds["publish_score"]
    )
    publishable = [item for item in fresh if item.relevance_score >= thresholds["publish_score"]]
    if outbox_state is not None:
        outbox_state.enqueue(publishable)

    # Persist only the thin, bounded correlation memory after enrichment. This must
    # succeed before the shared seen set is saved, so a failed memory write leaves
    # candidates eligible for pairing on the next run.
    for item in fresh:
        if item.source_id:
            card = build_correlation_card(item)
            JsonCorrelationState.upsert_card(
                correlation_state_value,
                candidate_id=card["candidate_id"],
                source_id=card["source_id"],
                published_at=datetime.fromisoformat(
                    card["published_at"].replace("Z", "+00:00")
                ),
                high_value_entities=card["high_value_entities"],
                title_tokens=card["title_tokens"],
                content_hash=card["content_hash"],
                last_seen=datetime.fromisoformat(
                    card["last_seen"].replace("Z", "+00:00")
                ),
            )
    correlation_store.save(correlation_state_value)

    if discovery_store is not None and discovery_state_value is not None:
        for result in discovery_results:
            if result.state_updates or _has_pagination_progress(result.pagination):
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

    # Discovery validators/candidate state and correlation memory must persist before
    # discovery items enter the shared seen set. If either save fails, a later run
    # can rediscover them.
    state.save(seen)
    health = {
        result.source_id: discovery_state_value["sources"].get(result.source_id, {}).get("health", {})
        for result in discovery_results
        if discovery_state_value is not None
    }
    return PipelineResult(collected, fresh, failures, reports, discovery_results, health, buried_signals, publishable)


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
                state=_NoSaveState(JsonState()),
                discovery_state=_NoSaveState(JsonDiscoveryState()),
                correlation_state=_NoSaveState(JsonCorrelationState()),
            )
        else:
            outbox_store = JsonOutboxState() if getattr(discord_settings, "webhook_url", "") else None
            if outbox_store is None:
                result = run_pipeline()
            else:
                result = run_pipeline(outbox_state=outbox_store)
    except (StateFileError, DiscoveryStateError, CorrelationStateError, OutboxError) as exc:
        raise SystemExit(f"State error: {exc}") from exc
    except SourceRegistryError as exc:
        raise SystemExit(f"Source configuration error: {exc}") from exc
    except (DiscoveryRegistryError, DiscoveryDispatchError) as exc:
        raise SystemExit(f"Discovery configuration error: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"Pipeline configuration error: {exc}") from exc

    collected, fresh, failures = result.collected, result.fresh, result.failures
    reports, discovery_results = result.reports, result.discovery_results
    buried_signals = result.buried_signals
    publish_score = validate_thresholds(read_object("config/thresholds.json", "thresholds"))["publish_score"]
    relevant = result.publishable or [item for item in fresh if item.relevance_score >= publish_score]
    print(
        f"Collected: {len(collected)} | New: {len(fresh)} | Relevant: {len(relevant)} "
        f"| Buried signals: {len(buried_signals)}"
    )
    for item in relevant:
        print(f"[{item.relevance_score}] {item.title} — {item.source} "
              f"| entities: {', '.join(item.detected_entities) or 'none'} "
              f"| quality: {item.source_quality}")
        for reason in item.score_reasons:
            print(f"  - {reason}")
    for item in buried_signals:
        print(f"[buried {item.buried_signal_score}] {item.title} — {item.source} "
              f"| correlation: {item.correlation_score} "
              f"| entities: {', '.join(item.detected_entities) or 'none'}")
        for reason in item.buried_signal_reasons:
            print(f"  - {reason}")
    for failure in failures:
        print(f"Source failed: {failure}")
    for report in reports:
        if report is not None:
            print(f"Source {report.source_name}: {report.status} ({report.item_count} items)"
                  + (f"; HTTP {report.http_status}" if report.http_status else "")
                  + (f"; {report.error}" if report.error else ""))
    for discovery_result in discovery_results:
        print(f"Discovery source {discovery_result.source_id}: {discovery_result.status} "
              f"({len(discovery_result.candidates)} candidates)")
        health = result.health.get(discovery_result.source_id, {})
        if health:
            print(f"  Health: failures={health.get('consecutive_failures', 0)} "
                  f"empty={health.get('consecutive_empty', 0)}")
    if discord_settings.dry_run:
        publish(relevant, discord_settings)
        return

    outbox_store = JsonOutboxState() if getattr(discord_settings, "webhook_url", "") else None
    pending = outbox_store.load() if outbox_store is not None else relevant
    if outbox_store is not None and not pending:
        pending = relevant
    report = publish(pending, discord_settings)
    if outbox_store is not None and report is not None and report.posted_keys:
        outbox_store.remove(set(report.posted_keys))


if __name__ == "__main__":
    main()
