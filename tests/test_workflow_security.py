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
