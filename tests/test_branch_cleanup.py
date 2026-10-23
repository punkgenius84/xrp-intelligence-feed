from datetime import datetime, timezone

from scripts.cleanup_merged_branches import select_candidates


NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
OLD_MERGE = "2026-10-01T12:00:00Z"
RECENT_MERGE = "2026-10-20T12:00:00Z"


def branch(name, sha, *, protected=False):
    return {"name": name, "commit": {"sha": sha}, "protected": protected}


def pr(name, sha, *, merged_at=OLD_MERGE, base="main"):
    return {
        "merged_at": merged_at,
        "base": {"ref": base},
        "head": {"ref": name, "sha": sha},
    }


def test_selects_only_old_merged_branch_with_exact_current_tip():
    branches = [
        branch("safe/merged", "sha-safe"),
        branch("unmerged", "sha-unmerged"),
        branch("advanced-after-merge", "sha-new"),
        branch("recent-merge", "sha-recent"),
        branch("protected", "sha-protected", protected=True),
        branch("main", "sha-main"),
        branch("open-pr", "sha-open"),
    ]
    closed_prs = [
        pr("safe/merged", "sha-safe"),
        pr("unmerged", "sha-unmerged", merged_at=None),
        pr("advanced-after-merge", "sha-old"),
        pr("recent-merge", "sha-recent", merged_at=RECENT_MERGE),
        pr("protected", "sha-protected"),
        pr("main", "sha-main"),
        pr("open-pr", "sha-open"),
    ]
    open_prs = [{"head": {"ref": "open-pr"}}]

    result = select_candidates(
        branches, closed_prs, open_prs, "main", now=NOW, min_age_days=7
    )

    assert result == [{"name": "safe/merged", "sha": "sha-safe"}]


def test_does_not_select_pull_request_merged_into_another_base_branch():
    result = select_candidates(
        [branch("release-only", "sha-release")],
        [pr("release-only", "sha-release", base="release")],
        [],
        "main",
        now=NOW,
    )
    assert result == []


def test_does_not_select_when_merge_timestamp_is_missing_or_invalid():
    result = select_candidates(
        [branch("missing-date", "sha-a"), branch("invalid-date", "sha-b")],
        [
            pr("missing-date", "sha-a", merged_at=None),
            pr("invalid-date", "sha-b", merged_at="not-a-date"),
        ],
        [],
        "main",
        now=NOW,
    )
    assert result == []


def test_candidates_are_sorted_case_insensitively():
    result = select_candidates(
        [branch("z-last", "sha-z"), branch("A-first", "sha-a")],
        [pr("z-last", "sha-z"), pr("A-first", "sha-a")],
        [],
        "main",
        now=NOW,
    )
    assert [item["name"] for item in result] == ["A-first", "z-last"]
