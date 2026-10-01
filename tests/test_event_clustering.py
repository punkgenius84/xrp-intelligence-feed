from datetime import datetime, timezone

from intelligence.event_clustering import cluster_id_for, cluster_related_items
from models import NewsItem


def item(candidate_id, source_id, score=50):
    return NewsItem(
        title=f"Event {candidate_id}",
        url=f"https://example.test/{candidate_id}",
        source=source_id,
        source_id=source_id,
        candidate_id=candidate_id,
        relevance_score=score,
        published_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_clusters_connected_correlations_without_creating_new_links():
    first = item("a", "source-a", 80)
    second = item("b", "source-b", 70)
    third = item("c", "source-c", 60)
    first.correlated_candidate_ids = ["b"]
    second.correlated_candidate_ids = ["a", "c"]
    third.correlated_candidate_ids = ["b"]

    clusters = cluster_related_items([first, second, third])

    assert len(clusters) == 1
    assert [member.candidate_id for member in clusters[0].members] == ["a", "b", "c"]
    assert clusters[0].primary.candidate_id == "a"


def test_unrelated_items_remain_separate_singletons():
    first = item("a", "source-a")
    second = item("b", "source-b")

    clusters = cluster_related_items([first, second])

    assert len(clusters) == 2
    assert all(len(cluster.members) == 1 for cluster in clusters)


def test_cluster_id_is_order_independent():
    first = item("a", "source-a")
    second = item("b", "source-b")

    assert cluster_id_for([first, second]) == cluster_id_for([second, first])


def test_unknown_correlation_ids_do_not_create_phantom_members():
    first = item("a", "source-a")
    first.correlated_candidate_ids = ["missing"]

    clusters = cluster_related_items([first])

    assert len(clusters) == 1
    assert [member.candidate_id for member in clusters[0].members] == ["a"]


def test_empty_input_is_empty():
    assert cluster_related_items([]) == []
