from __future__ import annotations

from datetime import timezone
from hashlib import sha256
import json
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


def _selection_specificity(item: NewsItem) -> int:
    """Use subject specificity only to break equal-score selection ties."""
    categories = set(item.relevance_categories)
    if categories & {
        "direct_xrp_xrpl", "direct_ripple", "rlusd_stablecoin",
        "ripple_payments_network", "direct_ripple_institutional_action",
    }:
        return 0
    if categories & {
        "xrp_protocol_development", "xrp_ledger_tokenization_development",
        "institutional_digital_asset_payment", "institutional_tokenization_action",
        "regulatory_tokenization_action", "government_tokenization_action",
        "government_digital_asset_action", "legislative_digital_asset_action",
    }:
        return 1
    return 2


def _published_timestamp(item: NewsItem) -> float:
    value = item.published_at or item.collected_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


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
            _selection_specificity(item),
            -_published_timestamp(item),
            item.title.casefold(),
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
    near_misses = sorted(
        (item for item in items if item.relevance_score < config.min_relevance_score),
        key=lambda item: (
            -item.relevance_score,
            -_published_timestamp(item),
            item.title.casefold(),
        ),
    )[:10]

    clusters = cluster_related_items(selected)
    print(
        "Intelligence enrichment: "
        f"selected_items={len(selected)} clusters={len(clusters)} "
        f"provider_timeout_seconds={config.timeout_seconds} candidate_items={len(items)}"
    )
    if near_misses:
        print(
            "Intelligence relevance near-misses: "
            f"threshold={config.min_relevance_score} showing={len(near_misses)} "
            f"of={sum(item.relevance_score < config.min_relevance_score for item in items)}"
        )
        for item in near_misses:
            print(
                f"  near_miss score={item.relevance_score} "
                f"source={item.source!r} source_id={item.source_id!r} "
                f"categories={','.join(item.relevance_categories) or 'none'} "
                f"title={item.title[:300]!r}"
            )
            if item.summary:
                print(f"    source_summary_excerpt={' '.join(item.summary.split())[:420]!r}")
            for reason in item.score_reasons[:4]:
                print(f"    score_reason={reason}")

    for rank, item in enumerate(selected, start=1):
        print(
            f"Intelligence selected candidate: rank={rank}/{len(selected)} "
            f"score={item.relevance_score} source={item.source!r} "
            f"source_id={item.source_id!r} "
            f"categories={','.join(item.relevance_categories) or 'none'} "
            f"title={item.title[:300]!r}"
        )
        print(f"  source_url={item.url[:500]!r}")
        if item.summary:
            print(f"  source_summary_excerpt={' '.join(item.summary.split())[:420]!r}")
        for reason in item.score_reasons[:6]:
            print(f"  score_reason={reason}")

    for index, cluster in enumerate(clusters, start=1):
        started = time.monotonic()
        identity = cluster.cluster_id
        member_titles = " | ".join(item.title[:180] for item in cluster.members)
        print(
            f"Intelligence cluster start: {index}/{len(clusters)} "
            f"id={identity} members={len(cluster.members)} titles={member_titles[:520]!r}"
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
            print(
                f"Intelligence cluster complete: {index}/{len(clusters)} "
                f"id={identity} status=success elapsed_ms={round((time.monotonic() - started) * 1000)}"
            )
            claim_audit = [
                {
                    "text": " ".join(claim.text.split())[:280],
                    "certainty": claim.certainty,
                    "evidence": list(claim.evidence),
                }
                for claim in analysis.claims[:5]
            ]
            print(
                f"Intelligence accepted analysis: id={identity} "
                f"event_type={analysis.event_type!r} "
                f"summary={analysis.event_summary[:240]!r} "
                f"claims={json.dumps(claim_audit, ensure_ascii=False)[:1600]}"
            )
        except LLMError as exc:
            failures.append(f"{identity}: {exc}")
            print(
                f"Intelligence cluster complete: {index}/{len(clusters)} "
                f"id={identity} status=rejected elapsed_ms={round((time.monotonic() - started) * 1000)} "
                f"reason={exc}"
            )
    return events, failures
