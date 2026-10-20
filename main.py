import html
import re
import unicodedata
import time
import os
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser

from collectors.rss import FeedCollectionError, RSSCollector
from discovery.base import DiscoveryResult
from discovery.dispatch import (DiscoveryDispatchError, DiscoveryRegistryError,
                                collect_source, load_discovery_sources)
from discovery.models import DiscoveryCandidate
from discovery.normalization import normalize_candidate
from discord.publisher import (DEFAULT_MAX_AGE_DAYS, publish, settings_from_env,
                               split_future_dated, split_stale)
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
from storage.outbox import JsonOutboxState, OutboxError, publication_key
from storage.discovery_state import DiscoveryStateError, JsonDiscoveryState
from storage.intelligence_state import IntelligenceStateError, JsonIntelligenceState
from storage.intelligence_outbox import IntelligenceOutboxError, JsonIntelligenceOutboxState
from storage.intelligence_delivery_history import IntelligenceDeliveryHistoryError, JsonIntelligenceDeliveryHistory
from storage.intelligence_quarantine import IntelligenceQuarantineError, JsonIntelligenceQuarantineState
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
        or bool(result.observations)
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
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
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
            source_id = source.get("source_id", "unknown-source")
            started_at = time.monotonic()
            print(f"Discovery start: {source_id}")
            result = collect_source(source, discovery_state_value)
            elapsed_ms = round((time.monotonic() - started_at) * 1000)
            print(
                f"Discovery complete: {source_id} status={result.status} "
                f"candidates={len(result.candidates)} elapsed_ms={elapsed_ms}"
            )
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
            for observation in result.observations:
                change = JsonDiscoveryState.record_observation(
                    discovery_state_value, result.source_id, observation, result.fetched_at
                )
                if change is not None:
                    previous_title = (
                        f" previous_title={change['previous_title']!r}"
                        if change.get("previous_title") else ""
                    )
                    print(
                        f"Discovery observation: {result.source_id} {change['change']} "
                        f"undated official route title={change['title']!r}"
                        f"{previous_title} "
                        f"first_seen={change['first_seen_time']} url={change['url']}"
                    )
            if result.errors:
                failures.extend(f"{result.source_id}: {error}" for error in result.errors)

    normalized = [
        normalize_candidate(item) if isinstance(item, DiscoveryCandidate) else normalize_item(item)
        for item in collected
    ]
    # Reject implausibly future-dated candidates before deduplication. Otherwise a
    # bad source date can poison seen.json and prevent the corrected item from ever
    # being reconsidered after its publication date is fixed.
    normalized, future_dated = split_future_dated(normalized)
    if future_dated:
        print(
            f"Discovery: ignored {len(future_dated)} candidate(s) with publication dates "
            "more than 24 hours in the future before deduplication"
        )
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
        queueable, too_old = split_stale(publishable, max_age_days)
        if too_old:
            print(f"Discord: {len(too_old)} relevant item(s) older than {max_age_days} days "
                  "were not queued for delivery")
        outbox_state.enqueue(queueable)

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
        # Observation tracking is opt-in per source. Clear transient records from any
        # source that is not explicitly opted in, including records from pre-opt-in runs.
        for configured_source in discovery_sources:
            if configured_source.get("track_undated_observations") is True:
                continue
            source_state = discovery_state_value["sources"].get(configured_source.get("source_id", ""))
            if isinstance(source_state, dict):
                source_state.pop("observations", None)
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
            max_age_days=getattr(discord_settings, "max_age_days", DEFAULT_MAX_AGE_DAYS),
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


def _print_source_failures(failures: list[str], partial_source_ids: set[str]) -> None:
    """Distinguish recoverable partial-source warnings from complete source failures."""
    for failure in failures:
        source_id = failure.partition(":")[0].strip()
        if source_id in partial_source_ids:
            print(f"Source warning (partial): {failure}")
        else:
            print(f"Source failed: {failure}")


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

        below_minimum = sorted(
            (
                item for item in fresh
                if item.relevance_score < intelligence_config.min_relevance_score
            ),
            key=lambda item: (-item.relevance_score, item.title.casefold()),
        )
        if below_minimum:
            shown = below_minimum[:5]
            print(
                "Intelligence below-minimum candidates "
                f"(showing {len(shown)} of {len(below_minimum)}; capped at 5):"
            )
            for item in shown:
                print(f"  [{item.relevance_score}] {item.title} — {item.source}")
                for reason in item.score_reasons[:3]:
                    print(f"    - {reason}")
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
    partial_source_ids = {
        item.source_id for item in result.discovery_results
        if getattr(item, "status", "") == "partial"
    }
    _print_source_failures(result.failures, partial_source_ids)
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
    stale_after_days: int = 30,
    allowed_event_ids: set[str] | None = None,
    quarantine_store=None,
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
    history = JsonIntelligenceDeliveryHistory()
    try:
        delivered_event_ids = history.load()
        queued_before = queue.load()
        if allowed_event_ids is not None:
            queued_before = [
                item for item in queued_before
                if item.event.event_id in allowed_event_ids
            ]
    except (IntelligenceDeliveryHistoryError, IntelligenceOutboxError) as exc:
        raise SystemExit(f"Intelligence publish refused: delivery state is unreadable: {exc}") from exc

    # A prior send can succeed even if removing the outbox entry fails. Reconcile
    # the durable delivery ledger before enqueueing newly generated events so a
    # replayed candidate cannot repost an already-delivered intelligence event.
    already_delivered_keys = {
        item.key for item in queued_before if item.event.event_id in delivered_event_ids
    }
    if already_delivered_keys:
        queue.remove(already_delivered_keys)
        out(f"Intelligence: removed {len(already_delivered_keys)} already-delivered event(s) from outbox")

    new_events = [
        event for event in events
        if event.event_id not in delivered_event_ids
        and (allowed_event_ids is None or event.event_id in allowed_event_ids)
    ]
    if new_events:
        queue.enqueue(new_events)
    pending = queue.load()
    if allowed_event_ids is not None:
        pending = [
            item for item in pending
            if item.event.event_id in allowed_event_ids
        ]
    if not pending:
        return 0

    # The outbox may contain events generated during a long shadow-mode period.
    # When publication is explicitly enabled, discard only events whose newest
    # evidence is outside the configured freshness window. Unknown dates are held.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_after_days)
    stale_keys: set[str] = set()
    undated_keys: set[str] = set()
    for item in pending:
        if not item.event.evidence:
            continue  # Existing evidence-URL validation below handles this case.
        evidence_dates = []
        for evidence in item.event.evidence:
            stamp = evidence.published_at
            if not isinstance(stamp, str) or not stamp.strip():
                continue
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                evidence_dates.append(parsed.astimezone(timezone.utc))
            except ValueError:
                continue
        if not evidence_dates:
            undated_keys.add(item.key)
        elif max(evidence_dates) < cutoff:
            stale_keys.add(item.key)

    if stale_keys:
        stale_items = [item for item in pending if item.key in stale_keys]
        quarantine = quarantine_store or JsonIntelligenceQuarantineState()
        try:
            # Persist the full event and evidence before removing it from the active queue.
            # If quarantine storage fails, leave the outbox untouched and fail closed.
            quarantine.quarantine(
                stale_items,
                reason=f"latest evidence older than {stale_after_days} days",
                quarantined_at=now,
            )
        except IntelligenceQuarantineError as exc:
            raise SystemExit(
                "Intelligence publish refused: stale events could not be safely quarantined; "
                "active outbox was not changed"
            ) from exc
        queue.remove(stale_keys)
        out(
            f"Intelligence: quarantined {len(stale_keys)} stale event(s) with latest evidence "
            f"older than {stale_after_days} days; full records preserved for review"
        )
    if undated_keys:
        out(
            f"::warning::Intelligence: held {len(undated_keys)} queued event(s) "
            "because evidence timestamps are missing or invalid"
        )
    pending = [item for item in pending if item.key not in stale_keys | undated_keys]
    if not pending:
        return 0

    invalid_pending = [
        item for item in pending
        if not any(evidence.url.strip() for evidence in item.event.evidence)
    ]
    generic_titles = {
        "read more", "learn more", "click here", "view more", "more", "details",
    }
    generic_title_pending = [
        item for item in pending
        if item.event.evidence and all(
            str(evidence.title or "").strip().casefold() in generic_titles
            or not str(evidence.title or "").strip()
            for evidence in item.event.evidence
        )
    ]
    if generic_title_pending:
        generic_keys = {item.key for item in generic_title_pending}
        quarantine = quarantine_store or JsonIntelligenceQuarantineState()
        try:
            # Preserve the complete malformed event for audit before removing it
            # from the active queue. If quarantine persistence fails, fail closed.
            quarantine.quarantine(
                generic_title_pending,
                reason="all evidence titles are empty or generic navigation text",
                quarantined_at=now,
            )
        except IntelligenceQuarantineError as exc:
            raise SystemExit(
                "Intelligence publish refused: generic-title events could not be safely "
                "quarantined; active outbox was not changed"
            ) from exc
        queue.remove(generic_keys)
        pending = [item for item in pending if item.key not in generic_keys]
        out(
            f"Intelligence: quarantined {len(generic_keys)} malformed event(s) with "
            "only empty or generic navigation titles; full records preserved for review"
        )
    url_valid_pending = [item for item in pending if item not in invalid_pending]
    publishable_pending = [
        item for item in url_valid_pending if item not in generic_title_pending
    ]
    if invalid_pending:
        for item in invalid_pending:
            out(
                f"::warning::Intelligence event held from publication for missing evidence URL: "
                f"{item.event.event_id}"
            )
        if not url_valid_pending:
            raise SystemExit(
                "Intelligence publish refused: every pending event lacks an evidence URL"
            )
    if generic_title_pending:
        for item in generic_title_pending:
            out(
                f"::warning::Intelligence event held from publication because all evidence "
                f"titles are empty or generic navigation text: {item.event.event_id}"
            )

    limit = max(1, int(getattr(discord_settings, "max_posts", 5)))
    # A malformed event must never be published, but it also must not poison the
    # entire queue and prevent independently valid events behind it from delivery.
    selected = publishable_pending[:limit]
    hook = DiscordWebhook(discord_settings.webhook_url)
    posted_keys: set[str] = set()
    posted_event_ids: set[str] = set()
    for item in selected:
        try:
            hook.send(format_intelligence_event(item.event))
        except DiscordError as exc:
            out(f"::warning::Intelligence post failed for {item.event.event_id}: {exc}")
            continue
        posted_keys.add(item.key)
        posted_event_ids.add(item.event.event_id)

    if posted_event_ids:
        # Record success before removing the outbox entry. If the outbox write
        # fails after this point, the next run can reconcile against this ledger.
        try:
            history.record(posted_event_ids)
        except IntelligenceDeliveryHistoryError as exc:
            out(f"::error::Intelligence delivery history could not be saved: {exc}")
            raise SystemExit(
                "Intelligence publish stopped after Discord accepted a post but "
                "delivery history could not be persisted; outbox entries were retained"
            ) from exc
        queue.remove(posted_keys)
    out(f"Intelligence: posted {len(posted_event_ids)} of {len(selected)} queued events")
    return len(posted_event_ids)


def publish_one_intelligence_test_event(
    event_id: str,
    discord_settings,
    *,
    out=print,
    outbox_store=None,
    history_store=None,
) -> int:
    """Publish one explicitly selected queued intelligence event; never publish the normal feed."""
    if not discord_settings.dry_run:
        raise SystemExit(
            "Intelligence test refused: DISCORD_DRY_RUN must remain true for the normal feed"
        )
    if not isinstance(event_id, str) or not event_id.strip():
        raise SystemExit("Intelligence test refused: an exact queued event ID is required")
    if not discord_settings.webhook_url:
        raise SystemExit("Intelligence test refused: DISCORD_WEBHOOK_URL is not set")

    queue = outbox_store or JsonIntelligenceOutboxState()
    history = history_store or JsonIntelligenceDeliveryHistory()
    try:
        delivered_ids = history.load()
        queued = queue.load()
    except (IntelligenceDeliveryHistoryError, IntelligenceOutboxError) as exc:
        raise SystemExit(
            f"Intelligence test refused: delivery state is unreadable: {exc}"
        ) from exc

    matches = [item for item in queued if item.event.event_id == event_id]
    if len(matches) != 1:
        raise SystemExit(
            "Intelligence test refused: the exact event ID must match one queued event; "
            f"matches={len(matches)}"
        )
    if event_id in delivered_ids:
        raise SystemExit(
            "Intelligence test refused: this event is already in delivery history"
        )

    # Require at least one real HTTP(S) evidence URL with a valid, recent timestamp.
    # Unknown, stale, malformed, or future-dated evidence must not be used for a test post.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    future_cutoff = now + timedelta(days=1)
    valid_evidence = []
    for evidence in matches[0].event.evidence:
        try:
            parsed_url = urlsplit(str(evidence.url or "").strip())
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                continue
            stamp = evidence.published_at
            if not isinstance(stamp, str) or not stamp.strip():
                continue
            published_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)
            published_at = published_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        if cutoff <= published_at <= future_cutoff:
            valid_evidence.append(evidence)
    if not valid_evidence:
        raise SystemExit(
            "Intelligence test refused: target needs at least one valid HTTP(S) evidence URL "
            "with a valid timestamp inside the 30-day freshness window"
        )

    # The overall feed settings stay dry-run. Only this explicitly selected
    # intelligence event receives a one-post, non-dry-run settings copy.
    test_settings = replace(discord_settings, dry_run=False, max_posts=1)
    posted = publish_intelligence_events(
        [],
        test_settings,
        publish_enabled=True,
        out=out,
        outbox_store=queue,
        stale_after_days=30,
        allowed_event_ids={event_id},
    )
    if posted != 1:
        raise SystemExit(
            "Intelligence test did not deliver exactly one event; inspect the run log. "
            "No other queued event was eligible for this test."
        )
    out(f"Intelligence one-event test completed: delivered_event_id={event_id}")
    return posted


def publish_feed(
    relevant,
    intelligence_events,
    discord_settings,
    outbox_store,
    intelligence_outbox_store=None,
) -> None:
    """Publish normal and intelligence items through durable delivery paths."""
    max_age_days = getattr(discord_settings, "max_age_days", DEFAULT_MAX_AGE_DAYS)
    if discord_settings.dry_run:
        relevant, future_relevant = split_future_dated(relevant)
        if future_relevant:
            print(
                f"Discord: {len(future_relevant)} relevant item(s) with publication dates "
                "more than 24 hours in the future would be skipped"
            )
        relevant, too_old = split_stale(relevant, max_age_days)
        if too_old:
            print(f"Discord: {len(too_old)} relevant item(s) older than {max_age_days} days "
                  "would be skipped")
        publish(relevant, discord_settings)
        return

    pending = outbox_store.load() if outbox_store is not None else relevant
    pending, future_pending = split_future_dated(pending)
    future_pending_keys = {
        item.key if hasattr(item, "key") else publication_key(item)
        for item in future_pending
    }
    if future_pending:
        if outbox_store is not None:
            outbox_store.remove(future_pending_keys)
            print(
                f"Discord: dropped {len(future_pending)} queued item(s) with publication dates "
                "more than 24 hours in the future"
            )
        else:
            print(
                f"Discord: skipped {len(future_pending)} item(s) with publication dates "
                "more than 24 hours in the future"
            )
    pending, stale_pending = split_stale(pending, max_age_days)
    queue_count_after_stale_filter = len(pending)
    already_delivered_count = 0
    delivery_history_count = 0
    if stale_pending:
        # Too old to be news. Drop them from the queue permanently instead of letting them
        # block or crowd out fresh items.
        if outbox_store is not None:
            outbox_store.remove({item.key if hasattr(item, "key") else publication_key(item)
                                 for item in stale_pending})
        print(f"Discord: dropped {len(stale_pending)} queued item(s) older than "
              f"{max_age_days} days")
    delivered_keys: set[str] = set()
    if outbox_store is not None:
        # A prior run may have successfully posted an item but failed to persist
        # the normal seen/outbox state afterward. Delivery history is the durable
        # evidence that prevents that recovery path from posting the same item again.
        delivery_history = JsonDeliveryHistory()
        delivered_items = delivery_history.load()
        delivery_history_count = len(delivered_items)
        delivered_keys = {item.key for item in delivered_items}
        delivered_urls = {
            item.url.strip().rstrip("/")
            for item in delivered_items
            if item.url.strip()
        }
        posted_keys = delivered_keys
        if posted_keys or delivered_urls:
            already_delivered = {
                item.key for item in pending
                if item.key in posted_keys
                or (getattr(item, "url", "").strip().rstrip("/") in delivered_urls)
            }
            if already_delivered:
                already_delivered_count = len(already_delivered)
                outbox_store.remove(already_delivered)
                pending = [item for item in pending if item.key not in already_delivered]
                print(
                    f"Discord: removed {len(already_delivered)} already-delivered "
                    "item(s) from the recovery outbox"
                )
        if not pending:
            # Do not reintroduce a just-reconciled item from this run's fresh
            # candidate list when the seen-state cache was the part that failed.
            fallback_candidates = [
                item for item in relevant
                if publication_key(item) not in delivered_keys
                and getattr(item, "url", "").strip().rstrip("/") not in delivered_urls
            ]
            fallback_candidates, future_fallback = split_future_dated(fallback_candidates)
            unreported_future = [
                item for item in future_fallback
                if (item.key if hasattr(item, "key") else publication_key(item))
                not in future_pending_keys
            ]
            if unreported_future:
                print(
                    f"Discord: skipped {len(unreported_future)} future-dated fresh item(s) "
                    "instead of reintroducing them from the current run"
                )
            pending, _ = split_stale(fallback_candidates, max_age_days)
    if outbox_store is not None:
        print(
            "Discord delivery state: "
            f"queued_after_stale_filter={queue_count_after_stale_filter} "
            f"history_entries={delivery_history_count} "
            f"already_delivered_removed={already_delivered_count} "
            f"pending_after_reconciliation={len(pending)}"
        )
    report = publish(pending, discord_settings)
    if report is not None and report.posted_keys:
        try:
            JsonDeliveryHistory().record(
                pending,
                set(report.posted_keys),
            )
        except DeliveryHistoryError as exc:
            print(f"::error::Discord delivery history could not be saved: {exc}")
            raise SystemExit(
                "Discord delivery stopped after a successful post but delivery history "
                "could not be persisted; recovery outbox entries were retained"
            ) from exc
    if outbox_store is not None and report is not None and report.posted_keys:
        outbox_store.remove(set(report.posted_keys))
    if outbox_store is not None:
        remaining_count = len(outbox_store.load())
        posted_count = len(report.posted_keys) if report is not None else 0
        print(
            "Discord delivery result: "
            f"posted_this_run={posted_count} queue_remaining={remaining_count}"
        )

    intelligence_config = load_intelligence_config()
    publish_intelligence_events(
        intelligence_events,
        discord_settings,
        publish_enabled=intelligence_config.enabled and intelligence_config.publish_enabled,
        outbox_store=intelligence_outbox_store,
        stale_after_days=getattr(intelligence_config, "stale_after_days", 30),
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

    test_only = os.environ.get("INTELLIGENCE_TEST_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if test_only:
        if not discord_settings.dry_run:
            raise SystemExit(
                "Intelligence test refused: set dry_run=true so normal feed publishing stays disabled"
            )
        target_event_id = os.environ.get("INTELLIGENCE_TEST_EVENT_ID", "").strip()
        publish_one_intelligence_test_event(target_event_id, discord_settings)
        return

    publish_feed(
        relevant,
        result.intelligence_events,
        discord_settings,
        outbox_store,
        intelligence_outbox_store,
    )


if __name__ == "__main__":
    main()
