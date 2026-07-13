"""Thin typed client for the GitHub REST API, scoped to one repo."""

from __future__ import annotations

from typing import Any

import httpx

from .http_util import APIError, request_json


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ) -> None:
        self.repo = repo
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    async def get_repo(self) -> dict[str, Any]:
        return await request_json(self._client, "GET", f"/repos/{self.repo}")

    async def list_issues(
        self, label: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            items = await request_json(
                self._client,
                "GET",
                f"/repos/{self.repo}/issues",
                params={
                    "labels": label,
                    "state": state,
                    "per_page": 100,
                    "page": page,
                },
            )
            # The issues endpoint also returns PRs; keep real issues only.
            issues.extend(item for item in items if "pull_request" not in item)
            if len(items) < 100:
                return issues
            page += 1

    async def get_pr(self, pr_number: int) -> dict[str, Any]:
        return await request_json(
            self._client, "GET", f"/repos/{self.repo}/pulls/{pr_number}"
        )

    async def create_issue(
        self, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        return await request_json(
            self._client,
            "POST",
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": body, "labels": labels},
        )

    async def comment(self, issue_number: int, body: str) -> dict[str, Any]:
        return await request_json(
            self._client,
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            json={"body": body},
        )

    async def add_labels(self, issue_number: int, labels: list[str]) -> Any:
        return await request_json(
            self._client,
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/labels",
            json={"labels": labels},
        )

    async def remove_label(self, issue_number: int, label: str) -> None:
        try:
            await request_json(
                self._client,
                "DELETE",
                f"/repos/{self.repo}/issues/{issue_number}/labels/{label}",
            )
        except APIError as exc:
            if exc.status != 404:  # label already absent is fine
                raise

    async def pr_check_runs(self, pr_number: int) -> list[dict[str, Any]]:
        """Check runs on a PR's head commit (empty if CI is disabled/absent)."""
        pr = await request_json(
            self._client, "GET", f"/repos/{self.repo}/pulls/{pr_number}"
        )
        result = await request_json(
            self._client,
            "GET",
            f"/repos/{self.repo}/commits/{pr['head']['sha']}/check-runs",
        )
        return result.get("check_runs", [])

    async def aclose(self) -> None:
        await self._client.aclose()
