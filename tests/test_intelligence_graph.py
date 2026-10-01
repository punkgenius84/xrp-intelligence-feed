from intelligence.events import IntelligenceEvent
from intelligence.evidence import Evidence
from intelligence.graph import (
    build_intelligence_graph,
    claim_node_id,
    entity_node_id,
    event_node_id,
    source_node_id,
)
from intelligence.llm.schemas import Claim


def event(event_id="evt-1"):
    return IntelligenceEvent(
        event_id=event_id,
        event_type="partnership",
        summary="Ripple and a bank announced a partnership.",
        significance="Source-backed.",
        entities=["Ripple", "Bank A"],
        claims=[
            Claim(
                text="The source reports a partnership.",
                claim_type="reported_fact",
                certainty="high",
                evidence=["source-1 title"],
            )
        ],
        evidence=[
            Evidence(
                source_id="source-1",
                source="Source One",
                url="https://example.test/one",
                published_at="2026-10-01T00:00:00+00:00",
                title="Partnership announced",
                source_quality="primary",
            )
        ],
    )


def test_graph_contains_event_entity_source_and_claim_nodes():
    graph = build_intelligence_graph([event()])

    node_types = {node.node_type for node in graph.nodes}
    assert node_types == {"event", "entity", "source", "claim"}

    node_ids = {node.node_id for node in graph.nodes}
    assert event_node_id("evt-1") in node_ids
    assert entity_node_id("Ripple") in node_ids
    assert source_node_id("source-1") in node_ids
    assert claim_node_id("evt-1", 0) in node_ids


def test_graph_edges_only_represent_explicit_event_membership():
    graph = build_intelligence_graph([event()])
    relationships = {
        edge.relationship
        for edge in graph.edges
    }
    assert relationships == {
        "entity_of_event",
        "source_for_event",
        "claim_about_event",
    }


def test_graph_is_deterministic_and_order_independent():
    first = event("evt-1")
    second = event("evt-2")
    assert build_intelligence_graph([first, second]) == build_intelligence_graph([second, first])


def test_graph_deduplicates_repeated_entity_and_source_membership():
    first = event("evt-1")
    first.entities = ["Ripple", "Ripple"]
    first.evidence = first.evidence * 2

    graph = build_intelligence_graph([first])

    entity_edges = [edge for edge in graph.edges if edge.relationship == "entity_of_event"]
    source_edges = [edge for edge in graph.edges if edge.relationship == "source_for_event"]
    assert len(entity_edges) == 1
    assert len(source_edges) == 1


def test_graph_does_not_create_nodes_for_blank_entities_or_sources():
    first = event("evt-1")
    first.entities = ["", "   ", "Ripple"]
    first.evidence = [
        Evidence(
            source_id="",
            source="Unknown",
            url="https://example.test/blank",
            published_at="2026-10-01T00:00:00+00:00",
            title="Blank",
            source_quality="unknown",
        )
    ]

    graph = build_intelligence_graph([first])
    assert entity_node_id("Ripple") in {node.node_id for node in graph.nodes}
    assert all(node.node_type != "source" for node in graph.nodes)
