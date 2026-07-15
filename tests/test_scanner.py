"""Scanner: static candidate loading, issue-body assembly, filing dedupe."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.scanner import Candidate, build_body, file_issues, load_static

CANDIDATES_YAML = Path(__file__).parent.parent / "candidates.yaml"


class FakeGitHub:
    def __init__(
        self,
        existing: list[dict[str, Any]] | None = None,
        remediated: list[dict[str, Any]] | None = None,
    ) -> None:
        self.by_label = {
            "devin-remediate": existing or [],
            "remediated-pending-merge": remediated or [],
        }
        self.created: list[dict[str, Any]] = []

    async def list_issues(self, label: str, state: str = "open") -> list[dict[str, Any]]:
        return self.by_label.get(label, [])

    async def create_issue(
        self, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        self.created.append({"title": title, "body": body, "labels": labels})
        return {"number": 100 + len(self.created)}


def test_load_static_candidates() -> None:
    candidates = load_static(CANDIDATES_YAML)
    assert len(candidates) == 9
    assert sum(1 for c in candidates if c.category == "describe-migration") == 7
    medium = [c for c in candidates if c.effort == "medium"]
    assert [c.file.rsplit("/", 1)[-1] for c in medium] == ["standardizedFormData.ts"]
    assert all(c.title and c.change for c in candidates)


def test_build_body_includes_verification() -> None:
    candidates = {c.file.rsplit("/", 1)[-1]: c for c in load_static(CANDIDATES_YAML)}
    test_body = build_body(candidates["parseCookie.test.ts"])
    assert "npm run test -- src/utils/parseCookie.test.ts" in test_body
    assert "pre-commit run --files superset-frontend/src/utils/parseCookie.test.ts" in test_body
    # non-test files get pre-commit but no jest line
    types_body = build_body(candidates["types.ts"])
    assert "npm run test" not in types_body
    assert "pre-commit run" in types_body


def test_file_issues_dedupes_and_caps() -> None:
    candidates = [
        Candidate(file=f"src/f{i}.test.ts", category="describe-migration",
                  title=f"t{i}", change="c")
        for i in range(4)
    ]
    already_filed = {
        "state": "closed",
        "body": build_body(candidates[0]),  # f0 filed in the past
    }
    github = FakeGitHub(existing=[already_filed])
    filed = asyncio.run(
        file_issues(candidates, github, "devin-remediate",
                    "remediated-pending-merge", max_open=2)  # type: ignore[arg-type]
    )
    # f0 deduped; cap of 2 open admits f1 + f2 only
    assert filed == 2
    assert [c["title"] for c in github.created] == ["t1", "t2"]
    assert github.created[0]["labels"] == ["devin-remediate", "category:describe-migration"]


def test_file_issues_counts_existing_open_toward_cap() -> None:
    github = FakeGitHub(existing=[{"state": "open", "body": "unrelated"}])
    candidates = [
        Candidate(file="src/a.ts", category="any-cleanup", title="a", change="c"),
        Candidate(file="src/b.ts", category="any-cleanup", title="b", change="c"),
    ]
    filed = asyncio.run(
        file_issues(candidates, github, "devin-remediate",
                    "remediated-pending-merge", max_open=2)  # type: ignore[arg-type]
    )
    assert filed == 1  # one slot already taken by the open issue


def test_file_issues_dedupes_remediated_label() -> None:
    """Success swaps the scan label for the done label; the file must stay
    deduped or a re-scan would re-file (and re-spend) remediated work."""
    candidate = Candidate(file="src/f0.test.ts", category="describe-migration",
                          title="t0", change="c")
    github = FakeGitHub(
        remediated=[{"state": "open", "body": build_body(candidate)}]
    )
    filed = asyncio.run(
        file_issues([candidate], github, "devin-remediate",
                    "remediated-pending-merge", max_open=5)  # type: ignore[arg-type]
    )
    assert filed == 0
    assert github.created == []
