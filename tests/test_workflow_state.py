from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "news_feed.yml"


def _step_block(contents: str, marker: str) -> str:
    start = contents.index(marker)
    start = contents.rfind("      - ", 0, start)
    end = contents.find("\n      - ", start + 1)
    return contents[start:] if end < 0 else contents[start:end]


def test_workflow_uses_staggered_quarter_hour_schedule():
    contents = WORKFLOW.read_text(encoding="utf-8")
    assert 'cron: "7,22,37,52 * * * *"' in contents


def test_workflow_restores_and_saves_both_persistent_state_files():
    contents = WORKFLOW.read_text(encoding="utf-8")
    restore = _step_block(contents, "uses: actions/cache/restore@v5")
    save = _step_block(contents, "name: Save persistent state")

    for block in (restore, save):
        assert "state/seen.json" in block
        assert "state/discovery.json" in block
        assert "state/correlation.json" in block
        assert "state/intelligence_outbox.json" in block


def test_workflow_uses_branch_scoped_content_keys_and_immutable_cache_pattern():
    contents = WORKFLOW.read_text(encoding="utf-8")
    restore = _step_block(contents, "uses: actions/cache/restore@v5")
    save = _step_block(contents, "name: Save persistent state")

    assert "actions/cache/restore@v5" in restore
    assert "key: xrp-state-${{ github.ref_name }}-v1-bootstrap" in restore
    assert "xrp-state-${{ github.ref_name }}-v1-" in restore
    assert "actions/cache/save@v5" in save
    assert "key: xrp-state-${{ github.ref_name }}-v1-${{ hashFiles('state/seen.json', 'state/discovery.json', 'state/correlation.json', 'state/intelligence.json', 'state/intelligence_outbox.json', 'state/outbox.json', 'state/delivery_history.json') }}" in save
    assert "github.run_id" not in contents
    assert "github.run_attempt" not in contents
    assert "cancel-in-progress: false" in contents
    assert "group: xrp-intelligence-feed" in contents
    assert "if: success()" in save


def test_state_restore_follows_tests_and_precedes_execution_without_dependency_cache():
    contents = WORKFLOW.read_text(encoding="utf-8")
    restore_at = contents.index("uses: actions/cache/restore@v5")
    setup_at = contents.index("uses: actions/setup-python@v6")
    install_at = contents.index("python -m pip install -r requirements-dev.txt")
    test_at = contents.index("python -m pytest -q")

    # The install command must be an actual YAML step, not text swallowed by a comment.
    install_lines = [line.strip() for line in contents.splitlines() if "python -m pip install -r requirements-dev.txt" in line]
    assert install_lines == ["- run: python -m pip install -r requirements-dev.txt"]
    app_at = contents.index("python main.py")
    save_at = contents.index("uses: actions/cache/save@v5")

    assert setup_at < install_at < test_at < restore_at < app_at < save_at
    assert "cache: pip" not in contents
    assert "requirements.txt" not in _step_block(contents, "uses: actions/cache/restore@v5")


def test_workflow_enables_intelligence_in_shadow_mode_with_local_model():
    contents = WORKFLOW.read_text(encoding="utf-8")
    run = _step_block(contents, "run: python main.py")
    assert 'INTELLIGENCE_ENABLED: "true"' in run
    assert 'INTELLIGENCE_PUBLISH: "false"' in run
    assert 'OLLAMA_ENDPOINT: "http://127.0.0.1:11434/api/chat"' in run
    assert 'OLLAMA_MODEL: "qwen2.5:3b"' in run


def test_workflow_prepares_ollama_before_feed_execution():
    contents = WORKFLOW.read_text(encoding="utf-8")
    ollama = contents.index("name: Ensure intelligence model is available")
    app = contents.index("run: python main.py")
    install = _step_block(contents, "name: Install Ollama")
    assert "timeout 180s bash -o pipefail -c" in install
    assert "curl --connect-timeout 15 --max-time 120 -fsSL https://ollama.com/install.sh | sh" in install
    assert "name: Install Ollama" in contents
    assert "name: Start Ollama" in contents
    assert "sudo systemctl stop ollama || true" in contents
    assert 'OLLAMA_MODELS="$OLLAMA_MODELS" ollama serve' in contents
    assert "name: Ensure intelligence model is available" in contents
    assert "already available from the restored model cache" in contents
    assert ollama < app
    assert "ollama-${{ runner.os }}-qwen2.5-3b-v1" in contents


def test_workflow_runs_real_intelligence_inference_before_feed():
    contents = WORKFLOW.read_text(encoding="utf-8")
    smoke = _step_block(contents, "name: Validate intelligence inference")
    app = contents.index("run: python main.py")

    assert "OllamaProvider(model=\"qwen2.5:3b\", timeout=45)" in smoke
    assert "enrich_clusters([item]" in smoke
    assert "if failures or len(events) != 1:" in smoke
    assert "INTELLIGENCE_PUBLISH" not in smoke
    assert contents.index("name: Validate intelligence inference") < app


def test_workflow_sets_intelligence_min_score_to_relevance_threshold():
    contents = WORKFLOW.read_text(encoding="utf-8")
    assert 'INTELLIGENCE_MIN_SCORE: "35"' in contents



def test_source_changes_trigger_safe_shadow_validation_without_state_saves():
    contents = WORKFLOW.read_text(encoding="utf-8")
    trigger = contents.split("  schedule:", 1)[0]

    assert "  push:" in trigger
    assert "      - main" in trigger
    assert '      - ".github/workflows/news_feed.yml"' in trigger
    assert '      - "config/discovery_sources.json"' in trigger
    assert '      - "discovery/**"' in trigger
    assert '      - "storage/**"' in trigger
    assert '      - "intelligence/**"' in trigger
    assert '      - "main.py"' in trigger

    run = _step_block(contents, "run: python main.py")
    assert "DISCORD_DRY_RUN: ${{ github.event_name != 'schedule' && (github.event_name != 'workflow_dispatch' || inputs.dry_run) }}" in run
    assert 'INTELLIGENCE_PUBLISH: "false"' in run

    save = _step_block(contents, "name: Save persistent state")
    assert "if: success() && (github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && !inputs.dry_run))" in save
