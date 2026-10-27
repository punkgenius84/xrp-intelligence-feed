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
    assert "Validate intelligence inference" not in workflow
    assert "timeout_seconds=90" not in workflow

def test_source_specific_parser_probes_are_parked_from_push_diagnostics():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    parked_scripts = ("probe_coinbase_article_metadata.py", "probe_coinbase_official_sitemap.py", "probe_coinbase_blog_rss.py", "probe_coinbase_landing_index.py", "probe_coinbase_product_index.py", "probe_coinbase_yahoo_index.py", "probe_coinbase_investor_news.py", "probe_swift_press_index.py", "probe_imf_media_center.py", "probe_citi_production_sitemap_collector.py")
    for script in parked_scripts:
        assert f"run: python scripts/{script}" not in workflow
        assert (Path(__file__).resolve().parents[1] / "scripts" / script).is_file()
    assert "Preview queued intelligence events" not in workflow
    assert "Preview quarantined intelligence events" not in workflow

def test_read_only_delivery_verification_never_runs_feed_or_publishes():
    workflow = (WORKFLOW_DIR / "news_feed.yml").read_text(encoding="utf-8")
    for removed in ("verify_intelligence_test_result:", "VERIFY-INTELLIGENCE-DELIVERY", "publish_intelligence_test:", "POST-ONE-INTELLIGENCE-EVENT", "prepare_quarantined_intelligence_test.py", "verify_intelligence_test_result.py"):
        assert removed not in workflow