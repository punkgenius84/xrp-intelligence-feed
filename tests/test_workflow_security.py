import re
from pathlib import Path


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_all_external_workflow_actions_are_pinned_to_full_commit_shas():
    references = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            if "uses:" not in line:
                continue
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if not match:
                continue
            reference = match.group(1)
            if reference.startswith(("./", "../", "$/")):
                continue
            references.append((workflow.name, line_number, reference))
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), (
                f"{workflow.name}:{line_number} uses a mutable or unpinned action: {reference}"
            )
    assert references, "expected to find external GitHub Actions references"


def test_production_workflow_verifies_pinned_ollama_archive_before_extracting():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    assert "releases/download/v0.40.3/ollama-linux-amd64.tar.zst" in workflow
    assert "sha256sum --check" in workflow
    assert "curl --connect-timeout 15 --max-time 300 -fsSL" in workflow
    assert "curl --connect-timeout 15 --max-time 120 -fsSL https://ollama.com/install.sh | sh" not in workflow


def test_headline_evaluation_workflow_is_manual_and_nonpublishing():
    workflow = (WORKFLOW_DIR / "intelligence_evaluation.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "\n  schedule:" not in workflow
    assert "scripts/evaluate_intelligence.py" in workflow
    assert "DISCORD_WEBHOOK_URL" not in workflow
    assert "python main.py" not in workflow


def test_headline_evaluation_workflow_verifies_ollama_before_extraction():
    workflow = (WORKFLOW_DIR / "intelligence_evaluation.yml").read_text(encoding="utf-8")
    assert "releases/download/v0.40.3/ollama-linux-amd64.tar.zst" in workflow
    assert "sha256sum --check" in workflow
    assert "curl --connect-timeout 15 --max-time 300 -fsSL" in workflow


def test_headline_evaluation_workflow_does_not_escape_shell_or_github_expressions():
    workflow = (WORKFLOW_DIR / "intelligence_evaluation.yml").read_text(encoding="utf-8")
    assert "\\${RUNNER_TEMP}" not in workflow
    assert "\\${{" not in workflow
    assert "${RUNNER_TEMP}" in workflow
    assert "${{ github.workspace }}" in workflow
    assert "${{ runner.os }}" in workflow


def test_production_inference_smoke_test_allows_slow_cold_model_startup():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    assert "timeout_seconds=90" in workflow
    assert 'OllamaProvider(model="qwen2.5:3b", timeout=90)' in workflow


def test_source_specific_parser_probes_are_parked_from_push_diagnostics():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    # Parser/access investigations are deferred; the scripts remain available for
    # a deliberate future run but should not slow or distract normal code pushes.
    parked_scripts = (
        "probe_coinbase_article_metadata.py",
        "probe_coinbase_official_sitemap.py",
        "probe_coinbase_blog_rss.py",
        "probe_coinbase_landing_index.py",
        "probe_coinbase_product_index.py",
        "probe_coinbase_yahoo_index.py",
        "probe_coinbase_investor_news.py",
        "probe_swift_press_index.py",
        "probe_imf_media_center.py",
        "probe_citi_production_sitemap_collector.py",
    )
    for script in parked_scripts:
        assert f"run: python scripts/{script}" not in workflow, f"parked probe still runs automatically: {script}"
        assert (Path(__file__).resolve().parents[1] / "scripts" / script).is_file(), f"keep probe for later: {script}"

    assert "Preview persisted Coinbase undated observations" not in workflow
    assert "Preview queued intelligence events" in workflow
    assert "Preview quarantined intelligence events" in workflow



def test_read_only_delivery_verification_never_runs_feed_or_publishes():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    assert "verify_intelligence_test_result:" in workflow
    assert "VERIFY-INTELLIGENCE-DELIVERY" in workflow
    assert "publish_intelligence_test=false" in workflow
    assert "if: ${{ !(github.event_name == 'workflow_dispatch' && inputs.verify_intelligence_test_result) }}" in workflow
    assert "if: github.event_name == 'workflow_dispatch' && (inputs.publish_intelligence_test || inputs.verify_intelligence_test_result) && success()" in workflow
    assert "python scripts/verify_intelligence_test_result.py --event-id \"$TEST_EVENT_ID\"" in workflow
    # Verification-only runs join the stateful concurrency group to avoid racing
    # scheduled cache writes, while the feed and model startup are skipped.
    assert "!inputs.verify_intelligence_test_result)) && 'diagnostic' || 'stateful'" in workflow
    assert workflow.count("&& !(github.event_name == 'workflow_dispatch' && inputs.verify_intelligence_test_result)") >= 5
