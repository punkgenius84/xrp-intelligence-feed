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
