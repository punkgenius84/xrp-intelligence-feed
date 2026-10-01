from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from .events import IntelligenceEvent


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    label: str


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    source_id: str
    target_id: str
    relationship: str


@dataclass(frozen=True, slots=True)
class IntelligenceGraph:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]


def _id(prefix: str, value: str) -> str:
    return f"{prefix}-" + sha256(value.encode("utf-8")).hexdigest()[:24]


def event_node_id(event_id: str) -> str:
    return _id("event", event_id)


def entity_node_id(entity: str) -> str:
    return _id("entity", entity.casefold().strip())


def source_node_id(source_id: str) -> str:
    return _id("source", source_id.strip())


def claim_node_id(event_id: str, index: int) -> str:
    return _id("claim", f"{event_id}|{index}")


def _edge(source_id: str, target_id: str, relationship: str) -> GraphEdge:
    return GraphEdge(
        edge_id=_id("edge", f"{source_id}|{relationship}|{target_id}"),
        source_id=source_id,
        target_id=target_id,
        relationship=relationship,
    )


def build_intelligence_graph(events: list[IntelligenceEvent]) -> IntelligenceGraph:
    """Build a deterministic relationship graph from already-established evidence.

    This function is a projection only. It never infers new facts or relationships.
    Entities come from event analysis, sources come from event evidence, and claims
    belong only to the event that contains them.
    """
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}

    for event in events:
        event_id = event_node_id(event.event_id)
        nodes[event_id] = GraphNode(event_id, "event", event.summary)

        for entity in sorted({value.strip() for value in event.entities if value.strip()}, key=str.casefold):
            entity_id = entity_node_id(entity)
            nodes.setdefault(entity_id, GraphNode(entity_id, "entity", entity))
            edge = _edge(entity_id, event_id, "entity_of_event")
            edges[edge.edge_id] = edge

        for evidence in sorted(
            event.evidence,
            key=lambda value: (value.source_id, value.url, value.title),
        ):
            if not evidence.source_id.strip():
                continue
            source_id = source_node_id(evidence.source_id)
            nodes.setdefault(source_id, GraphNode(source_id, "source", evidence.source))
            edge = _edge(source_id, event_id, "source_for_event")
            edges[edge.edge_id] = edge

        for index, claim in enumerate(event.claims):
            claim_id = claim_node_id(event.event_id, index)
            nodes[claim_id] = GraphNode(claim_id, "claim", claim.text)
            edge = _edge(claim_id, event_id, "claim_about_event")
            edges[edge.edge_id] = edge

    return IntelligenceGraph(
        nodes=tuple(sorted(nodes.values(), key=lambda node: (node.node_type, node.node_id))),
        edges=tuple(sorted(edges.values(), key=lambda edge: edge.edge_id)),
    )
