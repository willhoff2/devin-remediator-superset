"""The event producer: turns remediation candidates into labeled GitHub issues.

Static mode (default) files the verified entries in candidates.yaml —
deterministic, demo-safe. `--live-scan` is the production seam: it greps a
fresh shallow clone for describe-migration markers, so newly-added debt is
discovered without anyone curating a list.

Usage:
    python -m src.scanner [--live-scan] [--max-open N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .config import Config
from .github_client import GitHubClient
from .log import configure_logging, get_logger

log = get_logger(__name__)

MARKER = "TODO: Migrate from describe blocks"

DESCRIBE_CHANGE = """\
Unwrap the top-level `describe()` block per the repo convention (CLAUDE.md:
"Use `test()` instead of `describe()`"): dedent the `test()` calls to module
scope, hoist any describe-scoped setup, and delete the stale
`eslint-disable-next-line no-restricted-globals` TODO comment. Preserve test
semantics exactly.
"""

ISSUE_BODY = """\
## Remediation task (auto-filed by devin-remediator)

**File:** `{file}`
**Category:** {category}

### Required change

{change}
Touch only this file.

### Verification (all must pass before opening a PR)

{verification}
"""

_FILE_LINE_RE = re.compile(r"\*\*File:\*\* `([^`]+)`")


@dataclass(frozen=True)
class Candidate:
    file: str
    category: str
    title: str
    change: str
    effort: str = "default"

    @property
    def labels(self) -> list[str]:
        labels = [f"category:{self.category}"]
        if self.effort != "default":
            labels.append(f"effort:{self.effort}")
        return labels


def build_body(candidate: Candidate) -> str:
    checks = []
    if ".test." in candidate.file:
        rel = candidate.file.removeprefix("superset-frontend/")
        checks.append(f"- [ ] `cd superset-frontend && npm run test -- {rel}`")
    checks.append(f"- [ ] `pre-commit run --files {candidate.file}`")
    return ISSUE_BODY.format(
        file=candidate.file,
        category=candidate.category,
        change=candidate.change,
        verification="\n".join(checks),
    )


def load_static(path: Path) -> list[Candidate]:
    raw = yaml.safe_load(path.read_text())
    return [
        Candidate(
            file=entry["file"],
            category=entry["category"],
            title=entry["title"],
            change=entry.get("change", DESCRIBE_CHANGE),
            effort=entry.get("effort", "default"),
        )
        for entry in raw["candidates"]
    ]


def live_scan(repo: str, limit: int = 10) -> list[Candidate]:
    """Shallow-clone the repo and grep test files for the migration marker."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "clone", "--depth=1", "--filter=blob:none", "--sparse",
             f"https://github.com/{repo}.git", tmp],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", tmp, "sparse-checkout", "set", "superset-frontend/src"],
            check=True, capture_output=True,
        )
        found: list[Candidate] = []
        src = Path(tmp) / "superset-frontend" / "src"
        for test_file in sorted(src.rglob("*.test.ts*")):
            if MARKER not in test_file.read_text(errors="ignore"):
                continue
            rel = test_file.relative_to(Path(tmp))
            scope = rel.parts[2].lower()  # superset-frontend/src/<area>/...
            found.append(
                Candidate(
                    file=str(rel),
                    category="describe-migration",
                    title=f"test({scope}): migrate {test_file.name} off describe() block",
                    change=DESCRIBE_CHANGE,
                )
            )
            if len(found) >= limit:
                break
        return found


async def file_issues(
    candidates: list[Candidate],
    github: GitHubClient,
    label: str,
    max_open: int,
    dry_run: bool = False,
) -> int:
    """File candidates as issues; dedupe by file path across all past issues."""
    existing = await github.list_issues(label, state="all")
    known_files = {
        m.group(1)
        for issue in existing
        if (m := _FILE_LINE_RE.search(issue.get("body") or ""))
    }
    open_count = sum(1 for issue in existing if issue["state"] == "open")
    filed = 0
    for candidate in candidates:
        if candidate.file in known_files:
            log.info("candidate_already_filed", file=candidate.file)
            continue
        if open_count + filed >= max_open:
            log.info("open_issue_cap_reached", cap=max_open)
            break
        if dry_run:
            print(f"[dry-run] would file: {candidate.title}")
        else:
            issue = await github.create_issue(
                candidate.title, build_body(candidate), [label, *candidate.labels]
            )
            log.info(
                "issue_filed", number=issue["number"], title=candidate.title
            )
        filed += 1
    return filed


async def amain(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    candidates = (
        live_scan(cfg.github_repo, limit=args.max_open)
        if args.live_scan
        else load_static(Path(args.candidates))
    )
    log.info(
        "scan_complete", mode="live" if args.live_scan else "static",
        candidates=len(candidates),
    )
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    try:
        filed = await file_issues(
            candidates, github, cfg.issue_label, args.max_open, args.dry_run
        )
    finally:
        await github.aclose()
    print(f"{'Would file' if args.dry_run else 'Filed'} {filed} issue(s)")
    return 0


def main() -> int:
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-scan", action="store_true")
    parser.add_argument("--candidates", default="candidates.yaml")
    parser.add_argument("--max-open", type=int, default=6,
                        help="cap on simultaneously open remediation issues")
    parser.add_argument("--dry-run", action="store_true")
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
