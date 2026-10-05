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
    save = _step_block(contents, "uses: actions/cache/save@v5")

    for block in (restore, save):
        assert "state/seen.json" in block
        assert "state/discovery.json" in block
        assert "state/correlation.json" in block
        assert "state/intelligence_outbox.json" in block


def test_workflow_uses_branch_scoped_content_keys_and_immutable_cache_pattern():
    contents = WORKFLOW.read_text(encoding="utf-8")
    restore = _step_block(contents, "uses: actions/cache/restore@v5")
    save = _step_block(contents, "uses: actions/cache/save@v5")

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
    install_at = contents.index("python -m pip install -r requirements.txt")
    test_at = contents.index("python -m pytest -q")
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
    ollama = contents.index("name: Pull intelligence model")
    app = contents.index("run: python main.py")
    assert "name: Install Ollama" in contents
    assert "name: Start Ollama" in contents
    assert ollama < app
    assert "ollama-${{ runner.os }}-qwen2.5-3b-v1" in contents
