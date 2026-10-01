from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from models import NewsItem


@dataclass(frozen=True, slots=True)
class EventCluster:
    cluster_id: str
    members: tuple[NewsItem, ...]

    @property
    def primary(self) -> NewsItem:
        # Deterministic processing representative only; this does not declare one
        # source more truthful than another.
        return sorted(
            self.members,
            key=lambda item: (
                -item.relevance_score,
                item.published_at or item.collected_at,
                item.source_id,
                item.url,
            ),
        )[0]


def cluster_id_for(items: list[NewsItem]) -> str:
    identities = sorted(
        item.candidate_id or item.fingerprint
        for item in items
    )
    raw = "|".join(identities)
    return "cluster-" + sha256(raw.encode("utf-8")).hexdigest()[:24]


def cluster_related_items(items: list[NewsItem]) -> list[EventCluster]:
    """Build connected event groups from already-established correlation links.

    This layer does not create new correlations. It only turns correlation metadata
    into deterministic groups while preserving every source as an independent member.
    """
    if not items:
        return []

    by_id = {
        item.candidate_id or item.fingerprint: item
        for item in items
    }
    parent = {identity: identity for identity in by_id}

    def find(identity: str) -> str:
        while parent[identity] != identity:
            parent[identity] = parent[parent[identity]]
            identity = parent[identity]
        return identity

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for item in items:
        identity = item.candidate_id or item.fingerprint
        for related_id in item.correlated_candidate_ids:
            if related_id in by_id:
                union(identity, related_id)

    groups: dict[str, list[NewsItem]] = {}
    for item in items:
        identity = item.candidate_id or item.fingerprint
        groups.setdefault(find(identity), []).append(item)

    clusters = [
        EventCluster(
            cluster_id=cluster_id_for(members),
            members=tuple(
                sorted(
                    members,
                    key=lambda item: (
                        item.published_at or item.collected_at,
                        item.source_id,
                        item.url,
                    ),
                )
            ),
        )
        for members in groups.values()
    ]
    return sorted(clusters, key=lambda cluster: cluster.cluster_id)
