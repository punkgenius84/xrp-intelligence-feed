from __future__ import annotations

from hashlib import sha256

from models import NewsItem

from .configuration import IntelligenceRuntimeConfig
from .evidence import build_evidence_bundle, evidence_from_item
from .event_clustering import EventCluster, cluster_related_items
from .events import IntelligenceEvent, event_from_analysis
from .llm.base import LLMError, LLMProvider
from .llm.enrichment import analyze_cluster, analyze_item


def event_id_for(item: NewsItem) -> str:
    identity = item.candidate_id or item.fingerprint
    return "evt-" + sha256(identity.encode("utf-8")).hexdigest()[:24]


def select_items(
    items: list[NewsItem],
    config: IntelligenceRuntimeConfig,
) -> list[NewsItem]:
    eligible = [
        item
        for item in items
        if item.relevance_score >= config.min_relevance_score
    ]
    eligible.sort(
        key=lambda item: (
            -item.relevance_score,
            item.published_at or item.collected_at,
        )
    )
    return eligible[:config.max_items_per_run]


def restore_failed_items_for_retry(
    items: list[NewsItem],
    events: list[IntelligenceEvent],
    failures: list[str],
    seen: set[str],
    config: IntelligenceRuntimeConfig,
) -> list[str]:
    """Keep failed high-relevance enrichment candidates eligible for a later run.

    Deduplication adds candidates to the in-memory seen set before enrichment. If
    enrichment fails and that set is persisted unchanged, a transient model timeout
    permanently hides the candidate from future enrichment. Successful event member
    IDs remain seen; selected candidates with no successful event are released.
    """
    if not failures:
        return []

    successful_member_ids = {
        member_id
        for event in events
        for member_id in getattr(event, "member_ids", [])
    }
    # Single-item events may not populate member_ids; their stable event ID is based
    # on the same candidate identity, so account for that representation too.
    successful_member_ids.update(
        item.candidate_id or item.fingerprint
        for item in items
        if any(event.event_id == event_id_for(item) for event in events)
    )

    retry_ids: list[str] = []
    for item in select_items(items, config):
        identity = item.candidate_id or item.fingerprint
        if identity in successful_member_ids:
            continue
        seen.discard(item.fingerprint)
        seen.discard(item.event_fingerprint)
        retry_ids.append(identity)
    return retry_ids


def enrich_items(
    items: list[NewsItem],
    provider: LLMProvider,
    config: IntelligenceRuntimeConfig,
    historical_events: dict | None = None,
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    for item in select_items(items, config):
        try:
            analysis = analyze_item(item, provider, historical_events)
            events.append(
                event_from_analysis(
                    event_id_for(item),
                    analysis,
                    [evidence_from_item(item)],
                )
            )
        except LLMError as exc:
            failures.append(f"{item.candidate_id or item.fingerprint}: {exc}")
    return events, failures


def cluster_event_id(cluster: EventCluster) -> str:
    if len(cluster.members) == 1:
        return event_id_for(cluster.members[0])
    identities = sorted(
        item.candidate_id or item.fingerprint
        for item in cluster.members
    )
    raw = "|".join(identities)
    return "evt-cluster-" + sha256(raw.encode("utf-8")).hexdigest()[:24]


def enrich_clusters(
    items: list[NewsItem],
    provider: LLMProvider,
    config: IntelligenceRuntimeConfig,
    historical_events: dict | None = None,
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    selected = select_items(items, config)

    for cluster in cluster_related_items(selected):
        try:
            analysis = analyze_cluster(cluster, provider, historical_events)
            member_ids = [
                item.candidate_id or item.fingerprint
                for item in cluster.members
            ]
            supersedes = (
                [event_id_for(item) for item in cluster.members]
                if len(cluster.members) > 1
                else []
            )
            event = event_from_analysis(
                cluster_event_id(cluster),
                analysis,
                build_evidence_bundle(list(cluster.members)),
            )
            event.member_ids = member_ids
            event.supersedes = supersedes
            events.append(event)
        except LLMError as exc:
            identity = cluster.cluster_id
            failures.append(f"{identity}: {exc}")
    return events, failures
