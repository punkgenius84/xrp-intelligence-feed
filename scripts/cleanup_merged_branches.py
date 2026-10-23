#!/usr/bin/env python3
"""Safely prune stale branches whose exact tip was merged into the default branch.

The selector is deliberately conservative: a branch is eligible only when its
current SHA exactly matches the head SHA of a merged PR into the default branch,
the merge is at least seven days old, the branch is not protected/default, and
no open PR uses that head branch. Dry-run is the default.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from typing import Any

API_ROOT = "https://api.github.com"
MIN_BRANCH_AGE_DAYS = 7
CONFIRMATION = "DELETE-MERGED-BRANCHES"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def select_candidates(
    branches: list[dict[str, Any]],
    closed_prs: list[dict[str, Any]],
    open_prs: list[dict[str, Any]],
    default_branch: str,
    *,
    now: datetime | None = None,
    min_age_days: int = MIN_BRANCH_AGE_DAYS,
    repository: str | None = None,
) -> list[dict[str, str]]:
    """Return only branches whose exact current tip was merged and is stale."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current_time - timedelta(days=min_age_days)
    open_heads = {
        pr.get("head", {}).get("ref")
        for pr in open_prs
        if isinstance(pr.get("head"), dict)
    }

    merged_tips: dict[str, set[str]] = {}
    for pr in closed_prs:
        if pr.get("merged_at") is None:
            continue
        base = pr.get("base")
        head = pr.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            continue
        if repository is not None:
            head_repo = head.get("repo")
            if not isinstance(head_repo, dict) or head_repo.get("full_name") != repository:
                continue
        if base.get("ref") != default_branch:
            continue
        merged_at = _parse_time(pr.get("merged_at"))
        if merged_at is None or merged_at > cutoff:
            continue
        branch_name = head.get("ref")
        head_sha = head.get("sha")
        if isinstance(branch_name, str) and isinstance(head_sha, str):
            merged_tips.setdefault(branch_name, set()).add(head_sha)

    candidates: list[dict[str, str]] = []
    for branch in branches:
        name = branch.get("name")
        commit = branch.get("commit")
        if not isinstance(name, str) or name == default_branch:
            continue
        if branch.get("protected") is True or name in open_heads:
            continue
        if not isinstance(commit, dict) or not isinstance(commit.get("sha"), str):
            continue
        sha = commit["sha"]
        if sha not in merged_tips.get(name, set()):
            continue
        candidates.append({"name": name, "sha": sha})
    return sorted(candidates, key=lambda item: item["name"].casefold())


class GitHubAPI:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    def request(self, method: str, path: str) -> Any:
        request = Request(
            f"{API_ROOT}{path}",
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "xrp-intelligence-feed-branch-cleanup",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
                if not body:
                    return None
                return json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"GitHub API {method} {path} returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"GitHub API request failed: {exc}") from exc

    def pages(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            url = f"{path}{separator}{urlencode({'per_page': 100, 'page': page})}"
            payload = self.request("GET", url)
            if not isinstance(payload, list):
                raise RuntimeError(f"Expected a list from GitHub API for {url}")
            rows.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return rows
            page += 1
            if page > 100:
                raise RuntimeError(f"Pagination safety limit exceeded for {path}")

    def get_ref_sha(self, branch_name: str) -> str | None:
        ref = quote(f"heads/{branch_name}", safe="/")
        payload = self.request("GET", f"/repos/{self.repository}/git/ref/{ref}")
        if not isinstance(payload, dict):
            return None
        obj = payload.get("object")
        return obj.get("sha") if isinstance(obj, dict) else None

    def delete_branch(self, branch_name: str) -> None:
        ref = quote(f"heads/{branch_name}", safe="/")
        self.request("DELETE", f"/repos/{self.repository}/git/refs/{ref}")


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() == "true"
    confirmation = os.environ.get("CONFIRM_DELETE", "").strip()

    if not repository or "/" not in repository:
        print("ERROR: GITHUB_REPOSITORY must be owner/repository.", file=sys.stderr)
        return 2
    if not token:
        print("ERROR: GITHUB_TOKEN is required.", file=sys.stderr)
        return 2
    if not dry_run and confirmation != CONFIRMATION:
        print(f"ERROR: deletion requires CONFIRM_DELETE={CONFIRMATION!r}. No branches changed.", file=sys.stderr)
        return 2

    api = GitHubAPI(repository, token)
    repo = api.request("GET", f"/repos/{repository}")
    if not isinstance(repo, dict) or not isinstance(repo.get("default_branch"), str):
        print("ERROR: Could not establish the repository default branch.", file=sys.stderr)
        return 2
    default_branch = repo["default_branch"]

    branches = api.pages(f"/repos/{repository}/branches")
    closed_prs = api.pages(f"/repos/{repository}/pulls?state=closed&sort=updated&direction=desc")
    open_prs = api.pages(f"/repos/{repository}/pulls?state=open")

    candidates = select_candidates(
        branches, closed_prs, open_prs, default_branch, repository=repository
    )
    mode = "DRY RUN" if dry_run else "DELETE MODE"
    print(f"{mode}: inspected {len(branches)} branches and {len(closed_prs)} closed / {len(open_prs)} open pull requests.")
    print(f"Default branch: {default_branch}; minimum age since merge: {MIN_BRANCH_AGE_DAYS} days.")
    print(f"Eligible branches: {len(candidates)}")
    for candidate in candidates:
        print(f"CANDIDATE {candidate['name']} sha={candidate['sha']}")

    if dry_run:
        print("Dry run only: no branches were deleted.")
        return 0
    if not candidates:
        print("No eligible branches to delete.")
        return 0

    deleted = 0
    skipped = 0
    failures = 0
    for candidate in candidates:
        name = candidate["name"]
        # Recheck the ref immediately before deletion so a branch updated after
        # inventory is never removed using stale eligibility data.
        try:
            current_sha = api.get_ref_sha(name)
            if current_sha != candidate["sha"]:
                print(f"SKIP {name}: tip changed after inventory.")
                skipped += 1
                continue
            api.delete_branch(name)
            print(f"DELETED {name}")
            deleted += 1
        except RuntimeError as exc:
            print(f"ERROR {name}: {exc}", file=sys.stderr)
            failures += 1

    print(f"Finished: deleted={deleted} skipped={skipped} failures={failures}.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
