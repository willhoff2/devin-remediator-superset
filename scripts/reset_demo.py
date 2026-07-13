"""Reset all pipeline state for a fresh demo run.

A rehearsal run accumulates state in three places; this script clears the
first two and verifies the third:

1. GitHub fork: remediation issues (deleted, not closed: the scanner dedupes
   by file path across issues in ALL states, so closed issues would block
   re-filing forever), Devin's PRs (closed), and devin/* branches (deleted).
2. Local runner state: data/state.db + data/events.jsonl.
3. Fork master: verified against the frozen SHA. Never auto-reset: if a
   rehearsal PR was merged, master moved and the candidates' line numbers may
   be stale; the script prints the force-push command and defers to you.

Devin-side sessions need no reset: the monitor archives them, and ACUs
already spent are spent either way.

Usage:
    python -m scripts.reset_demo           # dry run: show what would happen
    python -m scripts.reset_demo --yes     # actually do it
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from src.config import Config
from src.http_util import request_json

# The fork was verified current at this SHA (docs: spec.md "Fork drift");
# candidates.yaml line references assume it. Do not sync/merge past it
# until the demo is recorded.
FROZEN_MASTER_SHA = "2a18a556b0"

DELETE_ISSUE_MUTATION = """
mutation($id: ID!) { deleteIssue(input: {issueId: $id}) { clientMutationId } }
"""


class ResetClient:
    """GitHub operations that only a demo reset needs. Kept out of the
    runner's GitHubClient so the pipeline itself can never delete."""

    def __init__(self, token: str, repo: str) -> None:
        self.repo = repo
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def issues_with_any_label(self, labels: list[str]) -> list[dict[str, Any]]:
        seen: dict[int, dict[str, Any]] = {}
        for label in labels:
            items = await request_json(
                self._client,
                "GET",
                f"/repos/{self.repo}/issues",
                params={"labels": label, "state": "all", "per_page": 100},
            )
            for item in items:
                if "pull_request" not in item:
                    seen[item["number"]] = item
        return list(seen.values())

    async def delete_issue(self, node_id: str) -> None:
        result = await request_json(
            self._client,
            "POST",
            "/graphql",
            json={"query": DELETE_ISSUE_MUTATION, "variables": {"id": node_id}},
        )
        if result.get("errors"):
            raise RuntimeError(f"deleteIssue failed: {result['errors']}")

    async def devin_prs(self, state: str = "all") -> list[dict[str, Any]]:
        prs = await request_json(
            self._client,
            "GET",
            f"/repos/{self.repo}/pulls",
            params={"state": state, "per_page": 100},
        )
        return [pr for pr in prs if pr["head"]["ref"].startswith("devin/")]

    async def close_pr(self, number: int) -> None:
        await request_json(
            self._client,
            "PATCH",
            f"/repos/{self.repo}/pulls/{number}",
            json={"state": "closed"},
        )

    async def delete_branch(self, ref: str) -> None:
        await request_json(
            self._client, "DELETE", f"/repos/{self.repo}/git/refs/heads/{ref}"
        )

    async def master_sha(self) -> str:
        branch = await request_json(
            self._client, "GET", f"/repos/{self.repo}/branches/master"
        )
        return branch["commit"]["sha"]

    async def aclose(self) -> None:
        await self._client.aclose()


async def run(apply: bool) -> int:
    cfg = Config.from_env()
    gh = ResetClient(cfg.github_token, cfg.github_repo)
    mode = "APPLY" if apply else "DRY RUN (pass --yes to apply)"
    print(f"=== demo reset: {cfg.github_repo} - {mode} ===\n")
    problems = 0
    try:
        # 1. PRs + branches first (deleting an issue with an open PR that
        #    says "Fixes #n" is fine, but close/delete reads better in the UI)
        prs = await gh.devin_prs()
        for pr in prs:
            branch = pr["head"]["ref"]
            action = "close+delete-branch" if pr["state"] == "open" else "delete-branch"
            print(f"PR #{pr['number']} ({pr['state']}) {branch}: {action}")
            if apply:
                if pr["state"] == "open":
                    await gh.close_pr(pr["number"])
                try:
                    await gh.delete_branch(branch)
                except Exception as exc:  # noqa: BLE001, branch may be gone already
                    print(f"  [warn] branch delete failed: {exc}")

        # 2. Issues: deletion, because closed issues still block the
        #    scanner's file-path dedupe (it scans state="all")
        issues = await gh.issues_with_any_label([cfg.issue_label, cfg.done_label])
        for issue in issues:
            print(f"Issue #{issue['number']} ({issue['state']}): delete: {issue['title']}")
            if apply:
                try:
                    await gh.delete_issue(issue["node_id"])
                except Exception as exc:  # noqa: BLE001
                    problems += 1
                    print(
                        f"  [FAIL] {exc}\n"
                        "  (deleteIssue is verified to work with the runner's"
                        " fine-grained PAT when its owner is the repo admin;"
                        " if this fails, fall back to bumping ISSUE_LABEL/"
                        "DONE_LABEL, see README)"
                    )

        # 3. Local state
        for path in (Path(cfg.db_path), Path(cfg.events_path)):
            if path.exists():
                print(f"Local: delete {path}")
                if apply:
                    path.unlink()
        data_dir = Path(cfg.db_path).parent
        if apply and data_dir.exists() and not any(data_dir.iterdir()):
            shutil.rmtree(data_dir)

        # 4. Fork master check: report only, never force-push automatically
        sha = await gh.master_sha()
        if sha.startswith(FROZEN_MASTER_SHA):
            print(f"\nmaster OK at frozen SHA {sha[:10]}")
        else:
            problems += 1
            print(
                f"\n[ACTION NEEDED] master is at {sha[:10]}, expected"
                f" {FROZEN_MASTER_SHA}: a rehearsal PR was merged. To reset"
                " (destructive, your call):\n"
                f"  git -C ../superset push -f origin {FROZEN_MASTER_SHA}:master\n"
                "Or leave it and re-verify candidates.yaml line references."
            )
    finally:
        await gh.aclose()
    print(f"\n=== {'done' if apply else 'dry run complete'}"
          f"{f', {problems} item(s) need attention' if problems else ''} ===")
    return 1 if problems else 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="apply (default: dry run)")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.yes))


if __name__ == "__main__":
    sys.exit(main())
