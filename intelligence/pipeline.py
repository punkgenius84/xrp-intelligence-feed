from __future__ import annotations

from hashlib import sha256
import time

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


def _selection_priority(item: NewsItem) -> int:
    """Prefer direct XRP/Ripple developments over broader ecosystem context."""
    categories = set(getattr(item, "relevance_categories", []) or [])
    entities = set(getattr(item, "detected_entities", []) or [])

    if entities & {"XRP", "Ripple", "RLUSD"} or categories & {
        "direct_xrp_xrpl", "direct_ripple", "rlusd_stablecoin",
        "xrp_protocol_development", "xrp_ledger_tokenization_development",
        "direct_ripple_institutional_action",
    }:
        return 0

    if categories & {
        "relevant_regulatory_digital_asset_payments",
        "government_digital_asset_action",
        "institutional_digital_asset_payment",
        "regulatory_tokenization_action",
        "government_tokenization_action",
        "institutional_tokenization_action",
        "legislative_digital_asset_action",
        "crypto_ecosystem_action",
        "institutional_bank_charter_action",
    }:
        return 1

    if categories & {
        "institutional_payment_action",
        "relevant_regulatory_payment_action",
        "government_payment_action",
        "financial_infrastructure_disruption",
    }:
        return 2

    return 3


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
            _selection_priority(item),
            -item.relevance_score,
            0 if getattr(item, "source_quality", "") == "primary" else 1,
            -(item.published_at or item.collected_at).timestamp(),
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
    historical_theses: dict | None = None,
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    for item in select_items(items, config):
        try:
            analysis = analyze_item(item, provider, historical_events, historical_theses)
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
    historical_theses: dict | None = None,
) -> tuple[list[IntelligenceEvent], list[str]]:
    events: list[IntelligenceEvent] = []
    failures: list[str] = []
    selected = select_items(items, config)

    clusters = cluster_related_items(selected)
    print(
        "Intelligence enrichment: "
        f"selected_items={len(selected)} clusters={len(clusters)} "
        f"provider_timeout_seconds={config.timeout_seconds}"
    )
    for index, cluster in enumerate(clusters, start=1):
        started = time.monotonic()
        identity = cluster.cluster_id
        print(
            f"Intelligence cluster start: {index}/{len(clusters)} "
            f"id={identity} members={len(cluster.members)}"
        )
        for item in cluster.members:
            print(
                "Intelligence candidate: "
                f"id={item.candidate_id or item.fingerprint} "
                f"score={item.relevance_score} "
                f"categories={','.join(item.relevance_categories) or 'none'} "
                f"source={item.source[:100]!r} "
                f"title={item.title[:280]!r} url={item.url}"
            )
        try:
            if len(cluster.members) == 1:
                analysis = analyze_item(
                    cluster.members[0], provider, historical_events, historical_theses
                )
            else:
                analysis = analyze_cluster(cluster, provider, historical_events, historical_theses)
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
            for claim in analysis.claims:
                print(
                    "Intelligence claim accepted: "
                    f"cluster={identity} certainty={claim.certainty} "
                    f"evidence={','.join(claim.evidence)} text={claim.text[:500]!r}"
                )
            print(
                f"Intelligence cluster complete: {index}/{len(clusters)} "
                f"id={identity} status=success elapsed_ms={round((time.monotonic() - started) * 1000)}"
            )
        except LLMError as exc:
            failures.append(f"{identity}: {exc}")
            print(
                f"Intelligence cluster complete: {index}/{len(clusters)} "
                f"id={identity} status=rejected elapsed_ms={round((time.monotonic() - started) * 1000)} "
                f"reason={exc}"
            )
    return events, failures
