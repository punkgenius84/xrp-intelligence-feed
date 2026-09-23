import json
from pathlib import Path

import pytest

from main import run_pipeline
import main
from models import NewsItem
from storage.database import MAX_STATE_ENTRIES, JsonState, StateFileError


def test_valid_state_loads_normally(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text('["first", "second"]', encoding="utf-8")
    assert JsonState(str(path)).load() == {"first", "second"}


def test_missing_state_starts_empty(tmp_path):
    assert JsonState(str(tmp_path / "missing.json")).load() == set()


def test_malformed_state_fails_closed_and_is_preserved(tmp_path):
    path = tmp_path / "seen.json"
    original = "[not valid json"
    path.write_text(original, encoding="utf-8")
    state = JsonState(str(path))
    with pytest.raises(StateFileError, match="malformed JSON"):
        state.load()
    with pytest.raises(StateFileError, match="malformed JSON"):
        state.save({"new-fingerprint"})
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("content", ['{"fingerprints": []}', '["valid", 42]', 'null'])
def test_unexpected_state_shape_fails_closed_and_is_preserved(tmp_path, content):
    path = tmp_path / "seen.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(StateFileError, match="unexpected shape"):
        JsonState(str(path)).load()
    with pytest.raises(StateFileError, match="unexpected shape"):
        JsonState(str(path)).save({"new-fingerprint"})
    assert path.read_text(encoding="utf-8") == content


def test_unreadable_state_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "seen.json"
    path.write_text('["existing"]', encoding="utf-8")
    original_read_text = Path.read_text

    def deny_read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("access denied")
        return original_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_read)
    with pytest.raises(StateFileError, match="Cannot read deduplication state"):
        JsonState(str(path)).load()


def test_main_reports_state_error_and_stops(monkeypatch):
    def fail_pipeline():
        raise StateFileError("Deduplication state is malformed")

    monkeypatch.setattr(main, "run_pipeline", fail_pipeline)
    with pytest.raises(SystemExit, match="State error: Deduplication state is malformed"):
        main.main()


def test_state_is_atomically_retained_in_deterministic_recency_order(tmp_path):
    path = tmp_path / "nested" / "seen.json"
    state = JsonState(str(path))
    state.save({"old", "recent"})
    state.save({"recent", "new"})
    assert json.loads(path.read_text(encoding="utf-8")) == ["recent", "new"]
    assert state.load() == {"recent", "new"}
    assert not path.with_suffix(".json.tmp").exists()


def test_state_retention_is_bounded(tmp_path):
    path = tmp_path / "seen.json"
    JsonState(str(path)).save({f"key-{i:05d}" for i in range(MAX_STATE_ENTRIES + 7)})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == MAX_STATE_ENTRIES
    assert saved[0] == "key-00007"


def test_pipeline_does_not_save_state_if_intelligence_fails(tmp_path, monkeypatch):
    state = JsonState(str(tmp_path / "seen.json"))
    item = NewsItem("XRP update", "https://example.test/item", "Example")
    source = {"source_id": "example", "name": "Example", "url": "https://example.test/feed",
              "authority_tier": 1, "category": "official", "entity_coverage": ["XRP"],
              "collection_type": "rss"}
    class Collector:
        last_report = None
        def __init__(self, source):
            pass
        def collect(self):
            return [item]
    monkeypatch.setattr("main.RSSCollector", Collector)
    monkeypatch.setattr("main.detect_entities", lambda item: (_ for _ in ()).throw(RuntimeError("intelligence failure")))
    with pytest.raises(RuntimeError, match="intelligence failure"):
        run_pipeline([source], state)
    assert state.load() == set()
