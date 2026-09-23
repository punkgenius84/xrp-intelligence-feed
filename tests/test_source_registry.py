import json
import pytest
from sources.registry import SourceRegistryError, enabled_sources, load_registry, validate_sources


def test_registry_has_valid_metadata_and_enabled_sources():
    sources = load_registry()
    assert len(sources) == 4
    assert len(enabled_sources()) == 4
    assert all(set(source) == {
        "source_id", "name", "authority_tier", "category", "url",
        "entity_coverage", "enabled", "collection_type",
    } for source in sources)


def test_registry_rejects_duplicate_source_ids():
    source = {
        "source_id": "x", "name": "Example", "authority_tier": 1,
        "category": "regulatory", "url": "https://example.gov/feed.xml",
        "entity_coverage": ["XRP"], "enabled": True, "collection_type": "rss",
    }
    with pytest.raises(SourceRegistryError, match="duplicate source_id"):
        validate_sources({"schema_version": 1, "sources": [source, source]})


def test_registry_rejects_invalid_url():
    source = {
        "source_id": "x", "name": "Example", "authority_tier": 1,
        "category": "regulatory", "url": "not-a-url",
        "entity_coverage": ["XRP"], "enabled": True, "collection_type": "rss",
    }
    with pytest.raises(SourceRegistryError, match="absolute HTTP"):
        validate_sources({"schema_version": 1, "sources": [source]})


def test_enabled_sources_filters_disabled_entries(tmp_path):
    source = {
        "source_id": "x", "name": "Example", "authority_tier": 1,
        "category": "regulatory", "url": "https://example.gov/feed.xml",
        "entity_coverage": ["XRP"], "enabled": False, "collection_type": "rss",
    }
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"schema_version": 1, "sources": [source]}), encoding="utf-8")
    assert load_registry(path) == [source]
    assert enabled_sources(path) == []
