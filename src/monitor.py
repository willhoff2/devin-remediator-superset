"""Tracks dispatched Devin sessions to a terminal outcome.

Primary success signal is the session's structured_output (validated against
REMEDIATION_SCHEMA at session level) plus PR existence — NOT fork CI, which
is disabled by default on forks and too slow for this loop anyway.

The flag-gated CI path (CI_CHECKS_ENABLED) polls the PR's check runs after a
success and, on a red result, sends the session exactly one follow-up message
to fix it. Capped at one retry: unbounded agent retries are an ACU leak.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .config import Config
from .devin_client import TERMINAL_STATUSES, DevinClient
from .github_client import GitHubClient
from .log import get_logger
from .store import Store, TaskStatus

log = get_logger(__name__)

SUCCESS_COMMENT = """\
✅ **Devin opened a PR for this issue:** {pr_url}

- Checks run in-session: {checks}
- ACUs consumed: {acus} (cap {cap})
- Summary: {summary}

This issue will close automatically when the PR merges (`Fixes #{number}`).
"""

FAILURE_COMMENT = """\
❌ **Devin could not complete this issue.**

- Session: {session_url}
- Reason: {reason}

The issue stays open for human triage.
"""

RETRY_MESSAGE = """\
CI reported failing checks on your PR {pr_url}: {failed_checks}.
Investigate, fix, and push to the same branch. Keep the diff within the
file(s) named in the issue. This is the only retry this session gets — if the
failure is not yours to fix (e.g. flaky or unrelated), say so via structured
output instead of changing code.
"""

_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


class Monitor:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        github: GitHubClient,
        devin: DevinClient,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._github = github
        self._devin = devin

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 — loop must survive transient API errors
                log.error("monitor_tick_failed", error=str(exc))
            await asyncio.sleep(self._cfg.poll_interval_sessions)

    async def tick(self) -> None:
        watched = self._store.tasks_with_status(
            TaskStatus.DISPATCHED, TaskStatus.SESSION_RUNNING, TaskStatus.RETRYING
        )
        for task in watched:
            await self._check_session(task)
        if self._cfg.ci_checks_enabled:
            for task in self._store.tasks_with_status(TaskStatus.SUCCEEDED):
                if task["ci_status"] in (None, "pending"):
                    await self._check_ci(task)

    async def _check_session(self, task: dict[str, Any]) -> None:
        number = task["issue_number"]
        state = await self._devin.get_session(task["session_id"])
        status = state.get("status")
        acus = state.get("acus_consumed")
        if acus is not None:
            self._store.update_task(number, acus_consumed=acus)
        if (
            status in ("claimed", "running")
            and task["status"] == TaskStatus.DISPATCHED
        ):
            self._store.update_task(number, status=TaskStatus.SESSION_RUNNING)
            self._store.record_event("session_running", number, session_status=status)
        elif status in TERMINAL_STATUSES:
            await self._finalize(task, state)

    async def _finalize(self, task: dict[str, Any], state: dict[str, Any]) -> None:
        number = task["issue_number"]
        output: dict[str, Any] = state.get("structured_output") or {}
        session_prs = state.get("pull_requests") or []
        pr_url = output.get("pr_url") or (
            session_prs[0].get("pr_url") if session_prs else None
        )
        succeeded = (
            state.get("status") == "exit"
            and output.get("success") is True
            and bool(pr_url)
        )
        fields: dict[str, Any] = {
            "completed_at": time.time(),
            "summary": output.get("summary") or output.get("blockers"),
            "pr_url": pr_url,
        }
        if succeeded:
            fields["status"] = TaskStatus.SUCCEEDED
            if self._cfg.ci_checks_enabled:
                fields["ci_status"] = "pending"
            self._store.update_task(number, **fields)
            self._store.record_event(
                "task_succeeded", number, pr_url=pr_url, acus=task["acus_consumed"]
            )
            await self._github.comment(
                number,
                SUCCESS_COMMENT.format(
                    pr_url=pr_url,
                    checks=", ".join(output.get("checks_run") or []) or "see PR body",
                    acus=task["acus_consumed"] or "?",
                    cap=task["acu_cap"],
                    summary=output.get("summary", ""),
                    number=number,
                ),
            )
            await self._github.add_labels(number, [self._cfg.done_label])
        else:
            reason = (
                output.get("blockers")
                or output.get("summary")
                or f"session ended with status={state.get('status')}, "
                f"detail={state.get('status_detail')}"
            )
            fields["status"] = TaskStatus.FAILED
            self._store.update_task(number, **fields)
            self._store.record_event("task_failed", number, reason=reason)
            await self._github.comment(
                number,
                FAILURE_COMMENT.format(
                    session_url=task["session_url"], reason=reason
                ),
            )

    async def _check_ci(self, task: dict[str, Any]) -> None:
        """Flag-gated: reconcile fork CI onto the task; retry once on red."""
        number = task["issue_number"]
        match = _PR_NUMBER_RE.search(task["pr_url"] or "")
        if not match:
            self._store.update_task(number, ci_status="unknown")
            return
        checks = await self._github.pr_check_runs(int(match.group(1)))
        if not checks:
            return  # nothing reported yet, stay pending
        failed = [c["name"] for c in checks if c.get("conclusion") == "failure"]
        pending = [c for c in checks if c.get("status") != "completed"]
        if pending and not failed:
            return
        if not failed:
            self._store.update_task(number, ci_status="green")
            self._store.record_event("ci_green", number)
            return
        if task["retries"] >= 1:
            self._store.update_task(number, ci_status="red", status=TaskStatus.FAILED)
            self._store.record_event("ci_red_final", number, failed=failed)
            return
        # NOTE: messaging an exited session relies on Devin resuming it; if
        # the API refuses, we mark the task failed rather than looping.
        try:
            await self._devin.send_message(
                task["session_id"],
                RETRY_MESSAGE.format(
                    pr_url=task["pr_url"], failed_checks=", ".join(failed)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self._store.update_task(number, ci_status="red", status=TaskStatus.FAILED)
            self._store.record_event("ci_retry_undeliverable", number, error=str(exc))
            return
        self._store.update_task(
            number, ci_status="red", status=TaskStatus.RETRYING, retries=1
        )
        self._store.record_event("ci_retry_sent", number, failed=failed)
