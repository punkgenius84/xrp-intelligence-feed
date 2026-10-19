from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from storage.discovery_state import JsonDiscoveryState
from scripts.preview_discovery_observations import render_preview


STAMP = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def _observation(native_id: str, title: str, *, url: str | None = None):
    return {
        "native_id": native_id,
        "url": url or f"https://www.coinbase.com{native_id}",
        "title": title,
    }


def test_preview_shows_first_seen_and_last_seen_without_mutating_state(tmp_path):
    path = tmp_path / "discovery.json"
    store = JsonDiscoveryState(path)
    state = store.load()
    JsonDiscoveryState.record_observation(
        state, "coinbase-blog", _observation("/blog/example", "Example announcement"), STAMP
    )
    changed_at = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    JsonDiscoveryState.record_observation(
        state, "coinbase-blog", _observation("/blog/example", "Updated announcement"), changed_at
    )
    JsonDiscoveryState.record_observation(
        state, "other-source", _observation("/blog/other", "Other official announcement"), STAMP
    )
    store.save(state, now=changed_at)
    before = path.read_bytes()

    output = render_preview(path, source_id="coinbase-blog", limit=10)

    assert "total=1 showing=1" in output
    assert "source=coinbase-blog" in output
    assert "Updated announcement" in output
    assert "previous_title: Example announcement" in output
    assert "first_seen=2026-10-15T12:00:00+00:00" in output
    assert "last_seen=2026-10-17T12:00:00+00:00" in output
    assert "not the article publication date" in output
    assert "Read-only preview" in output
    assert "other-source" not in output
    assert path.read_bytes() == before


def test_preview_cli_runs_outside_repository_root(tmp_path):
    path = tmp_path / "discovery.json"
    state = JsonDiscoveryState(path).load()
    JsonDiscoveryState.record_observation(
        state, "coinbase-blog", _observation("/blog/cli", "CLI observation"), STAMP
    )
    JsonDiscoveryState(path).save(state, now=STAMP)
    script = Path(__file__).resolve().parents[1] / "scripts" / "preview_discovery_observations.py"
    result = subprocess.run(
        [sys.executable, str(script), "--path", str(path), "--source-id", "coinbase-blog", "--limit", "1"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CLI observation" in result.stdout
    assert "Read-only preview" in result.stdout


@pytest.mark.parametrize("limit", [0, -1, 201, True, 1.5])
def test_preview_rejects_invalid_limit(tmp_path, limit):
    with pytest.raises(ValueError, match="between 1 and 200"):
        render_preview(tmp_path / "missing.json", limit=limit)
