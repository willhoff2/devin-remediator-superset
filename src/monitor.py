"""Tracks dispatched Devin sessions to a terminal outcome.

Primary success signal is the session's structured_output (validated against
REMEDIATION_SCHEMA at session level) plus PR existence — NOT fork CI, which
is disabled by default on forks and too slow for this loop anyway.

The flag-gated CI path (CI_CHECKS_ENABLED) polls the PR's check runs after a
success and, on a red result, sends the session one follow-up message to fix
it, capped at a single retry to bound ACU spend.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .config import Config
from .devin_client import DevinClient, session_reached_outcome
from .github_client import GitHubClient
from .log import get_logger
from .schemas import REMEDIATION_SCHEMA
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
            except Exception as exc:  # noqa: BLE001, loop must survive transient API errors
                log.error("monitor_tick_failed", error=str(exc))
            await asyncio.sleep(self._cfg.poll_interval_sessions)

    async def tick(self) -> None:
        watched = self._store.tasks_with_status(
            TaskStatus.DISPATCHED, TaskStatus.SESSION_RUNNING, TaskStatus.RETRYING
        )
        for task in watched:
            try:
                await self._check_session(task)
            except Exception as exc:  # noqa: BLE001, one bad session must not starve the rest
                log.error(
                    "session_check_failed",
                    issue=task["issue_number"],
                    error=str(exc),
                )
        if self._cfg.ci_checks_enabled:
            for task in self._store.tasks_with_status(TaskStatus.SUCCEEDED):
                if task["ci_status"] in (None, "pending"):
                    try:
                        await self._check_ci(task)
                    except Exception as exc:  # noqa: BLE001
                        log.error(
                            "ci_check_failed",
                            issue=task["issue_number"],
                            error=str(exc),
                        )

    async def _check_session(self, task: dict[str, Any]) -> None:
        number = task["issue_number"]
        state = await self._devin.get_session(task["session_id"])
        status = state.get("status")
        acus = state.get("acus_consumed")
        if acus is not None:
            self._store.update_task(number, acus_consumed=acus)
        if session_reached_outcome(state, REMEDIATION_SCHEMA["required"]):
            await self._finalize(task, state)
        elif (
            status in ("claimed", "running")
            and task["status"] == TaskStatus.DISPATCHED
        ):
            self._store.update_task(number, status=TaskStatus.SESSION_RUNNING)
            self._store.record_event(
                "session_running",
                number,
                session_status=status,
                session_url=task["session_url"],
            )

    async def _finalize(self, task: dict[str, Any], state: dict[str, Any]) -> None:
        number = task["issue_number"]
        output: dict[str, Any] = state.get("structured_output") or {}
        session_prs = state.get("pull_requests") or []
        pr_url = output.get("pr_url") or (
            session_prs[0].get("pr_url") if session_prs else None
        )
        # No status=="exit" requirement: finished sessions idle at
        # running/waiting_for_user (see devin_client.session_reached_outcome).
        succeeded = output.get("success") is True and bool(pr_url)
        if succeeded and self._cfg.pr_verify_enabled:
            # Don't take the agent's word for it: the PR must exist in our
            # repo and be open (or already merged).
            succeeded = await self._pr_verified(pr_url)
            if not succeeded:
                output = {**output, "blockers": f"reported PR failed verification: {pr_url}"}
        # Read ACUs from the fresh session state, not the task row fetched at
        # tick start (0.0 is a legitimate value on unmetered orgs).
        acus = state.get("acus_consumed")
        fields: dict[str, Any] = {
            "completed_at": time.time(),
            "summary": output.get("summary") or output.get("blockers"),
            "pr_url": pr_url,
        }
        if acus is not None:
            fields["acus_consumed"] = acus
        if succeeded:
            fields["status"] = TaskStatus.SUCCEEDED
            if self._cfg.ci_checks_enabled:
                fields["ci_status"] = "pending"
            self._store.update_task(number, **fields)
            self._store.record_event(
                "task_succeeded", number, pr_url=pr_url, acus=acus
            )
            await self._github.comment(
                number,
                SUCCESS_COMMENT.format(
                    pr_url=pr_url,
                    checks=", ".join(output.get("checks_run") or []) or "see PR body",
                    acus=acus if acus is not None else "?",
                    cap=task["acu_cap"],
                    summary=output.get("summary", ""),
                    number=number,
                ),
            )
            await self._github.add_labels(number, [self._cfg.done_label])
            await self._github.remove_label(number, self._cfg.issue_label)
            await self._wrap_up_session(task, pr_url, succeeded=True)
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
            await self._wrap_up_session(task, pr_url, succeeded=False)

    async def _pr_verified(self, pr_url: str) -> bool:
        match = _PR_NUMBER_RE.search(pr_url)
        if not match or f"github.com/{self._github.repo}/pull/" not in pr_url:
            return False
        try:
            pr = await self._github.get_pr(int(match.group(1)))
        except Exception:  # noqa: BLE001, missing PR and API error both fail closed
            return False
        return pr.get("state") == "open" or bool(pr.get("merged_at"))

    async def _wrap_up_session(
        self, task: dict[str, Any], pr_url: str | None, succeeded: bool
    ) -> None:
        """Terminal lifecycle: optional native Devin review of the PR (success
        only), then archive the session (sessions never exit on their own).
        Best-effort: the task outcome is already recorded, so
        failures here are logged, not propagated. Archiving is skipped when
        the CI retry path is on, which needs the session responsive to
        follow-up messages."""
        number = task["issue_number"]
        if succeeded and pr_url and self._cfg.devin_review_enabled:
            try:
                review = await self._devin.create_pr_review(pr_url)
                self._store.record_event(
                    "devin_review_requested", number, pr_url=pr_url,
                    review=review.get("review_id") or review.get("id"),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("devin_review_failed", issue=number, error=str(exc))
        if not self._cfg.ci_checks_enabled:
            await self._archive_session(task)

    async def _archive_session(self, task: dict[str, Any]) -> None:
        try:
            await self._devin.archive_session(task["session_id"])
            self._store.record_event("session_archived", task["issue_number"])
        except Exception as exc:  # noqa: BLE001
            log.error(
                "session_archive_failed",
                issue=task["issue_number"],
                error=str(exc),
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
        # Anything completed-but-not-passing counts as failed (cancelled,
        # timed_out, action_required, ...), not just conclusion=failure.
        failed = [
            c["name"]
            for c in checks
            if c.get("status") == "completed"
            and c.get("conclusion") not in ("success", "neutral", "skipped")
        ]
        pending = [c for c in checks if c.get("status") != "completed"]
        if pending and not failed:
            return
        if not failed:
            self._store.update_task(number, ci_status="green")
            self._store.record_event("ci_green", number)
            await self._archive_session(task)
            return
        if task["retries"] >= 1:
            self._store.update_task(number, ci_status="red", status=TaskStatus.FAILED)
            self._store.record_event("ci_red_final", number, failed=failed)
            await self._archive_session(task)
            return
        # If the API refuses to deliver to the idle session, fail the task
        # rather than loop.
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
            await self._archive_session(task)
            return
        self._store.update_task(
            number, ci_status="red", status=TaskStatus.RETRYING, retries=1
        )
        self._store.record_event("ci_retry_sent", number, failed=failed)
