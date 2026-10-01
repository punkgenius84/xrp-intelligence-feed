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


def enrich_items(
    items: list[NewsItem],
    provider: LLMProvider,
    config: IntelligenceRuntimeConfig,
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    for item in select_items(items, config):
        try:
            analysis = analyze_item(item, provider)
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
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    selected = select_items(items, config)

    for cluster in cluster_related_items(selected):
        try:
            analysis = analyze_cluster(cluster, provider)
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
