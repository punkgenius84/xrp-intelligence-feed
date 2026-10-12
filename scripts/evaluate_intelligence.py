#!/usr/bin/env python3
"""Run the fixed intelligence headline evaluation set against local Ollama.

This is a model-quality diagnostic, not a publishing switch or a replacement for
human review. A passing run is necessary evidence, not permission to enable publishing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from intelligence.llm.enrichment import analyze_item
from intelligence.llm.ollama import OllamaProvider
from models import NewsItem

DEFAULT_CASES = ROOT / "config" / "intelligence_evaluation.json"


def load_cases(path: str | Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("evaluation file must be a schema_version 1 object")
    cases = value.get("cases")
    if not isinstance(cases, list) or len(cases) < 10:
        raise ValueError("evaluation set must contain at least 10 cases")
    seen: set[str] = set()
    required = ("id", "source", "source_url", "title", "summary", "expected_event_types", "must_match", "must_not_match")
    for case in cases:
        if not isinstance(case, dict) or any(key not in case for key in required):
            raise ValueError("each evaluation case must contain all required fields")
        if not isinstance(case["id"], str) or not case["id"] or case["id"] in seen:
            raise ValueError("evaluation case IDs must be unique non-empty strings")
        seen.add(case["id"])
        if not case["source_url"].startswith("https://"):
            raise ValueError(f"{case['id']}: source_url must use HTTPS")
        if not isinstance(case["expected_event_types"], list) or not case["expected_event_types"]:
            raise ValueError(f"{case['id']}: expected_event_types must be a non-empty list")
        for key in ("must_match", "must_not_match"):
            if not isinstance(case[key], list) or any(not isinstance(pattern, str) or not pattern for pattern in case[key]):
                raise ValueError(f"{case['id']}: {key} must be a list of non-empty regex patterns")
    return cases


def analysis_text(analysis: Any) -> str:
    pieces = [
        getattr(analysis, "event_summary", ""),
        getattr(analysis, "significance", ""),
        " ".join(getattr(analysis, "entities", []) or []),
        " ".join(getattr(analysis, "conflicts", []) or []),
        " ".join(getattr(analysis, "uncertainties", []) or []),
    ]
    pieces.extend(claim.text for claim in (getattr(analysis, "claims", []) or []))
    return "\n".join(str(piece) for piece in pieces if piece)


def check_analysis(case: dict[str, Any], analysis: Any) -> list[str]:
    """Return deterministic expectation failures for one model analysis."""
    failures: list[str] = []
    if analysis.event_type not in case["expected_event_types"]:
        failures.append(
            f"event_type={analysis.event_type!r}; expected one of {case['expected_event_types']!r}"
        )
    output = analysis_text(analysis)
    for pattern in case["must_match"]:
        if not re.search(pattern, output, flags=re.IGNORECASE):
            failures.append(f"missing required pattern: {pattern}")
    for pattern in case["must_not_match"]:
        if re.search(pattern, output, flags=re.IGNORECASE):
            failures.append(f"contains forbidden pattern: {pattern}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="Path to evaluation JSON")
    parser.add_argument("--case", action="append", dest="selected", help="Run only this case ID; may be repeated")
    parser.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"), help="Installed Ollama model")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-case Ollama timeout in seconds")
    args = parser.parse_args()

    try:
        cases = load_cases(args.cases)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Evaluation setup error: {exc}", file=sys.stderr)
        return 2

    if args.selected:
        unknown = set(args.selected) - {case["id"] for case in cases}
        if unknown:
            print("Unknown case ID(s): " + ", ".join(sorted(unknown)), file=sys.stderr)
            return 2
        cases = [case for case in cases if case["id"] in set(args.selected)]

    provider = OllamaProvider(model=args.model, timeout=args.timeout)
    passed = 0
    failed = 0
    for case in cases:
        item = NewsItem(
            title=case["title"],
            url=case["source_url"],
            source=case["source"],
            source_id="evaluation-" + case["id"],
            candidate_id=case["id"],
            summary=case["summary"],
            source_quality="primary",
            relevance_score=90,
        )
        try:
            analysis = analyze_item(item, provider)
            failures = check_analysis(case, analysis)
            if failures:
                failed += 1
                print(f"FAIL {case['id']} [{analysis.event_type}]")
                for reason in failures:
                    print(f"  - {reason}")
            else:
                passed += 1
                print(f"PASS {case['id']} [{analysis.event_type}]")
        except Exception as exc:
            failed += 1
            print(f"ERROR {case['id']}: {exc.__class__.__name__}: {exc}")

    total = passed + failed
    print(f"\nEvaluation result: {passed}/{total} passed; {failed} failed; model={args.model}")
    print("A passing run does not enable publishing. Review every output and keep INTELLIGENCE_PUBLISH=false until shadow quality is accepted.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
