import html
import re
import unicodedata
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from html.parser import HTMLParser

from collectors.rss import FeedCollectionError, RSSCollector
from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryDispatchError, DiscoveryRegistryError,
                                collect_source, load_discovery_sources)
from discovery.models import DiscoveryCandidate
from discovery.normalization import normalize_candidate
from discord.publisher import publish, settings_from_env
from discord.intelligence import format_intelligence_event
from discord.webhook import DiscordError, DiscordWebhook
from intelligence.deduplication import deduplicate
from intelligence.correlation import build_correlation_card, correlate
from intelligence.configuration import load_intelligence_config
from intelligence.llm.ollama import OllamaProvider
from intelligence.pipeline import enrich_clusters, select_items
from intelligence.events import event_to_dict

from intelligence.buried_signals import detect_buried_signals
from intelligence.entities import detect_entities
from intelligence.relevance import score_relevance
from intelligence.source_quality import classify_source_quality
from sources.registry import SourceRegistryError, enabled_sources
from storage.correlation_state import CorrelationStateError, JsonCorrelationState
from storage.database import JsonState, StateFileError
from storage.outbox import JsonOutboxState, OutboxError
from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState
from storage.intelligence_state import IntelligenceStateError, JsonIntelligenceState
from storage.intelligence_outbox import IntelligenceOutboxError, JsonIntelligenceOutboxState
from storage.delivery_history import DeliveryHistoryError, JsonDeliveryHistory



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

def _should_persist_discovery_progress(result: DiscoveryResult) -> bool:
    return result.status in {"success", "empty", "partial"} and (
        bool(result.candidates)
        or result.status == "empty"
        or bool(result.state_updates)
        or _has_pagination_progress(result.pagination)
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
    publishable: list | None = None
    intelligence_events: list = field(default_factory=list)
    intelligence_failures: list[str] = field(default_factory=list)


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
    intelligence_state=None,
    intelligence_outbox=None,
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

    intelligence_config = load_intelligence_config()
    intelligence_store = None
    intelligence_state_value = None
    if intelligence_config.enabled:
        intelligence_store = intelligence_state or JsonIntelligenceState()
        intelligence_state_value = intelligence_store.load()
        stale_ids = JsonIntelligenceState.mark_stale_older_than(
            intelligence_state_value,
            now=datetime.now().astimezone(),
            max_age=timedelta(days=intelligence_config.stale_after_days),
        )
        if stale_ids:
            print(f"Intelligence lifecycle: marked {len(stale_ids)} event(s) stale")

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
        except ValueError:
            # Configuration errors (for example an unsupported collection type)
            # must still abort the pipeline rather than being mislabeled as a
            # source outage.
            raise
        except Exception as exc:
            source_id = source.get("source_id", "unknown source")
            failures.append(f"{source_id}: unexpected collection error: {exc}")
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

    intelligence_events = []
    intelligence_failures: list[str] = []
    if intelligence_config.enabled:
        provider = OllamaProvider(
            model=intelligence_config.model,
            timeout=intelligence_config.timeout_seconds,
        )
        intelligence_events, intelligence_failures = enrich_clusters(
            fresh,
            provider,
            intelligence_config,
        )
        if intelligence_state_value is not None:
            now = datetime.now().astimezone().isoformat()
            for event in intelligence_events:
                for superseded_id in event.supersedes:
                    JsonIntelligenceState.mark_superseded(
                        intelligence_state_value,
                        superseded_id,
                        event.event_id,
                        now,
                    )
                JsonIntelligenceState.upsert(
                    intelligence_state_value,
                    event.event_id,
                    event_to_dict(event),
                    now,
                )
        if intelligence_outbox is not None and intelligence_events:
            intelligence_outbox.enqueue(intelligence_events)

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

    if intelligence_store is not None and intelligence_state_value is not None:
        intelligence_store.save(intelligence_state_value)

    if discovery_store is not None and discovery_state_value is not None:
        for result in discovery_results:
            # A successful/partial discovery attempt can make durable progress even
            # when the adapter has no validators or pagination frontier to persist.
            # Persisting only ETags/frontiers otherwise leaves last_successful_fetch
            # stale for candidate-only and empty successful sources.
            if _should_persist_discovery_progress(result):
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
    return PipelineResult(
        collected,
        fresh,
        failures,
        reports,
        discovery_results,
        health,
        buried_signals,
        publishable,
        intelligence_events,
        intelligence_failures,
    )


class _NoSaveState:
    """Wraps a state store so a dry run reads state but never writes it."""

    def __init__(self, inner):
        self._inner = inner

    def load(self):
        return self._inner.load()

    def save(self, *args, **kwargs):
        return None


def collect_feed(discord_settings):
    """Run collection/state processing with the same dry-run semantics as main()."""
    try:
        if discord_settings.dry_run:
            return (
                run_pipeline(
                    state=_NoSaveState(JsonState()),
                    discovery_state=_NoSaveState(JsonDiscoveryState()),
                    correlation_state=_NoSaveState(JsonCorrelationState()),
                    intelligence_state=_NoSaveState(JsonIntelligenceState()),
                ),
                None,
                None,
            )
        outbox_store = JsonOutboxState() if getattr(discord_settings, "webhook_url", "") else None
        intelligence_outbox_store = (
            JsonIntelligenceOutboxState()
            if getattr(discord_settings, "webhook_url", "")
            else None
        )
        result = run_pipeline(
            outbox_state=outbox_store,
            intelligence_outbox=intelligence_outbox_store,
        ) if outbox_store is not None else run_pipeline()
        return result, outbox_store, intelligence_outbox_store
    except (
        StateFileError,
        DiscoveryStateError,
        CorrelationStateError,
        IntelligenceStateError,
        OutboxError,
        IntelligenceOutboxError,
    ) as exc:
        raise SystemExit(f"State error: {exc}") from exc
    except SourceRegistryError as exc:
        raise SystemExit(f"Source configuration error: {exc}") from exc
    except (DiscoveryRegistryError, DiscoveryDispatchError) as exc:
        raise SystemExit(f"Discovery configuration error: {exc}") from exc
    except ValueError as exc:
        raise SystemExit(f"Pipeline configuration error: {exc}") from exc


def print_source_health_warnings(result: PipelineResult, *, now: datetime | None = None) -> None:
    current = now or datetime.now().astimezone()
    cutoff_seconds = 24 * 60 * 60
    for discovery_result in result.discovery_results:
        health = result.health.get(discovery_result.source_id, {})
        status = health.get("last_status")
        started_raw = health.get("status_started_at")
        if status not in {"failed", "empty", "partial"} or not started_raw:
            continue
        try:
            started = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=current.tzinfo)
        age = (current.astimezone() - started.astimezone()).total_seconds()
        if age < cutoff_seconds:
            continue
        hours = age / 3600
        label = {"failed": "failing", "empty": "empty", "partial": "partially failing"}[status]
        print(
            f"::warning::Source health — {discovery_result.source_id} is {label} "
            f"for {hours:.1f}h (last attempt {health.get('last_attempt', 'unknown')})"
        )
        error = health.get("last_error", "")
        if error:
            print(f"  Last error: {error}")


def score_feed(result: PipelineResult) -> list:
    """Select and report publishable scored items without changing scoring behavior."""
    from intelligence.configuration import read_object, validate_thresholds

    publish_score = validate_thresholds(
        read_object("config/thresholds.json", "thresholds")
    )["publish_score"]
    relevant = result.publishable if result.publishable is not None else [
        item for item in result.fresh if item.relevance_score >= publish_score
    ]

    collected, fresh = result.collected, result.fresh
    buried_signals = result.buried_signals
    print(
        f"Collected: {len(collected)} | New: {len(fresh)} | Relevant: {len(relevant)} "
        f"| Buried signals: {len(buried_signals)} | Intelligence events: {len(result.intelligence_events)}"
    )
    intelligence_config = load_intelligence_config()
    if intelligence_config.enabled:
        eligible_count = sum(
            item.relevance_score >= intelligence_config.min_relevance_score
            for item in fresh
        )
        selected_count = len(select_items(fresh, intelligence_config))
        print(
            "Intelligence eligibility: "
            f"{len(fresh)} fresh candidates; {eligible_count} meet minimum relevance "
            f"score {intelligence_config.min_relevance_score}; "
            f"{selected_count} selected (limit {intelligence_config.max_items_per_run})"
        )
        if not fresh:
            print("Intelligence note: no fresh candidates were available for enrichment this run.")
        elif not eligible_count:
            print("Intelligence note: fresh candidates existed, but none met the configured minimum relevance score.")
    for item in relevant:
        print(
            f"[{item.relevance_score}] {item.title} — {item.source} "
            f"| entities: {', '.join(item.detected_entities) or 'none'} "
            f"| quality: {item.source_quality}"
        )
        for reason in item.score_reasons:
            print(f"  - {reason}")
    for event in result.intelligence_events:
        print("[intelligence preview]")
        print(format_intelligence_event(event))
        print()
    for item in buried_signals:
        print(
            f"[buried {item.buried_signal_score}] {item.title} — {item.source} "
            f"| correlation: {item.correlation_score} "
            f"| entities: {', '.join(item.detected_entities) or 'none'}"
        )
        for reason in item.buried_signal_reasons:
            print(f"  - {reason}")
    for failure in result.intelligence_failures:
        print(f"Intelligence failed: {failure}")
    for failure in result.failures:
        print(f"Source failed: {failure}")
    for report in result.reports:
        if report is not None:
            print(
                f"Source {report.source_name}: {report.status} ({report.item_count} items)"
                + (f"; HTTP {report.http_status}" if report.http_status else "")
                + (f"; {report.error}" if report.error else "")
            )
    for discovery_result in result.discovery_results:
        print(
            f"Discovery source {discovery_result.source_id}: "
            f"{discovery_result.status} ({len(discovery_result.candidates)} candidates)"
        )
        if discovery_result.source_id == "fdic-press-releases":
            pagination = discovery_result.pagination
            frontier = pagination.get("fdic_press_releases", {})
            print(
                "  FDIC frontier: "
                f"pages={pagination.get('fdic_pages_probed', [])} "
                f"boundary={frontier.get('boundary_id', 'unavailable')} "
                f"hint={frontier.get('deep_page_hint', 'unavailable')} "
                f"state={frontier.get('frontier_status', 'unavailable')}"
            )
        health = result.health.get(discovery_result.source_id, {})
        if health:
            print(
                f"  Health: failures={health.get('consecutive_failures', 0)} "
                f"incomplete/empty={health.get('consecutive_empty', 0)}"
            )
    print_source_health_warnings(result)
    return relevant


def publish_intelligence_events(
    events: list,
    discord_settings,
    *,
    publish_enabled: bool,
    out=print,
    outbox_store=None,
) -> int:
    if not events and outbox_store is None:
        return 0
    if not publish_enabled:
        return 0
    if discord_settings.dry_run:
        out("Intelligence publish: dry run; nothing posted")
        return 0
    if not discord_settings.webhook_url:
        raise SystemExit("Intelligence publish refused: DISCORD_WEBHOOK_URL is not set")

    queue = outbox_store or JsonIntelligenceOutboxState()
    if events:
        queue.enqueue(events)
    pending = queue.load()
    if not pending:
        return 0

    invalid_pending = [
        item for item in pending
        if not any(evidence.url.strip() for evidence in item.event.evidence)
    ]
    publishable_pending = [item for item in pending if item not in invalid_pending]
    if invalid_pending:
        for item in invalid_pending:
            out(
                f"::warning::Intelligence event held from publication for missing evidence URL: "
                f"{item.event.event_id}"
            )
        if not publishable_pending:
            raise SystemExit(
                "Intelligence publish refused: every pending event lacks an evidence URL"
            )

    limit = max(1, int(getattr(discord_settings, "max_posts", 5)))
    # A malformed event must never be published, but it also must not poison the
    # entire queue and prevent independently valid events behind it from delivery.
    selected = publishable_pending[:limit]
    hook = DiscordWebhook(discord_settings.webhook_url)
    posted_keys: set[str] = set()
    for item in selected:
        try:
            hook.send(format_intelligence_event(item.event))
        except DiscordError as exc:
            out(f"::warning::Intelligence post failed for {item.event.event_id}: {exc}")
            continue
        posted_keys.add(item.key)

    if posted_keys:
        queue.remove(posted_keys)
    out(f"Intelligence: posted {len(posted_keys)} of {len(selected)} queued events")
    return len(posted_keys)


def publish_feed(
    relevant,
    intelligence_events,
    discord_settings,
    outbox_store,
    intelligence_outbox_store=None,
) -> None:
    """Publish normal and intelligence items through durable delivery paths."""
    if discord_settings.dry_run:
        publish(relevant, discord_settings)
        return

    pending = outbox_store.load() if outbox_store is not None else relevant
    if outbox_store is not None and not pending:
        pending = relevant
    report = publish(pending, discord_settings)
    if report is not None and report.posted_keys:
        try:
            JsonDeliveryHistory().record(
                pending,
                set(report.posted_keys),
            )
        except DeliveryHistoryError as exc:
            print(f"WARNING: Discord delivery history could not be saved: {exc}")
    if outbox_store is not None and report is not None and report.posted_keys:
        outbox_store.remove(set(report.posted_keys))

    intelligence_config = load_intelligence_config()
    publish_intelligence_events(
        intelligence_events,
        discord_settings,
        publish_enabled=intelligence_config.enabled and intelligence_config.publish_enabled,
        outbox_store=intelligence_outbox_store,
    )


def main() -> None:
    try:
        discord_settings = settings_from_env()
    except ValueError as exc:
        raise SystemExit(f"Discord configuration error: {exc}") from exc

    # Keep the top-level orchestration deliberately boring:
    # collect -> score/report -> publish. Each stage can now be tested/replaced independently.
    result, outbox_store, intelligence_outbox_store = collect_feed(discord_settings)
    relevant = score_feed(result)
    publish_feed(
        relevant,
        result.intelligence_events,
        discord_settings,
        outbox_store,
        intelligence_outbox_store,
    )


if __name__ == "__main__":
    main()
