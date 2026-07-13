"""Turns labeled GitHub issues into Devin sessions, at most once each.

Cost-control guardrails, in dispatch order:
- SQLite primary key on issue_number for idempotency (the task row is created
  before the session, so a crash mid-dispatch can strand a row but never
  double-spend an issue),
- MAX_CONCURRENT_SESSIONS gate,
- per-session max_acu_limit derived from the issue's effort label.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import Config
from .devin_client import DevinClient
from .github_client import GitHubClient
from .http_util import APIError
from .log import get_logger
from .schemas import REMEDIATION_SCHEMA
from .sources import EventSource
from .store import Store, TaskStatus

log = get_logger(__name__)

REMEDIATION_PROMPT = """\
Remediate GitHub issue #{number} in {repo}.

Issue: {url}
Title: {title}

{playbook_line}The issue body is the authoritative scope: it
names the file(s), the required change, and the verification commands.
Report the outcome via structured output.
"""

DISPATCH_COMMENT = """\
🤖 **Devin session started** for this issue: {session_url}

The session is scoped to the file(s) named above, capped at {acu_cap} ACUs, \
and will open a PR referencing this issue when verification passes.
"""


def _issue_meta(issue: dict[str, Any]) -> tuple[str, str]:
    """Extract (category, effort) from issue labels; defaults are safe."""
    category, effort = "uncategorized", "default"
    for label in issue.get("labels", []):
        name = label["name"] if isinstance(label, dict) else str(label)
        if name.startswith("category:"):
            category = name.removeprefix("category:")
        elif name.startswith("effort:"):
            effort = name.removeprefix("effort:")
    return category, effort


class Dispatcher:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        source: EventSource,
        github: GitHubClient,
        devin: DevinClient,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._source = source
        self._github = github
        self._devin = devin

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001, loop must survive transient API errors
                log.error("dispatcher_tick_failed", error=str(exc))
            await asyncio.sleep(self._cfg.poll_interval_issues)

    async def tick(self) -> None:
        issues = await self._source.fetch_candidates()
        for issue in issues:
            number = issue["number"]
            if self._store.get_task(number) is not None:
                continue  # idempotency: already tracked, whatever its state
            if self._store.active_count() >= self._cfg.max_concurrent_sessions:
                log.info(
                    "concurrency_gate_hit",
                    limit=self._cfg.max_concurrent_sessions,
                    waiting_issue=number,
                )
                break
            if not await self._dispatch(issue):
                # Definitive API rejection (4xx): every remaining dispatch
                # this tick shares the payload shape; one failed call, not N.
                break

    async def _dispatch(self, issue: dict[str, Any]) -> bool:
        number = issue["number"]
        title = issue["title"]
        category, effort = _issue_meta(issue)
        acu_cap = (
            self._cfg.max_acu_medium
            if effort == "medium"
            else self._cfg.max_acu_default
        )
        # Create the task row BEFORE the session: a crash between the two
        # leaves a dispatchable-looking row but never a duplicate session.
        if not self._store.create_task(
            number, issue["html_url"], title, category, acu_cap
        ):
            return True  # row left by an earlier crash-restart; keep dispatching
        self._store.record_event("issue_accepted", number, category=category)
        try:
            session = await self._devin.create_session(
                prompt=REMEDIATION_PROMPT.format(
                    number=number,
                    repo=self._cfg.github_repo,
                    url=issue["html_url"],
                    title=title,
                    playbook_line=(
                        "Follow the playbook exactly. "
                        if self._cfg.devin_playbook_id
                        else ""
                    ),
                ),
                repos=[self._cfg.github_repo],
                title=f"remediate #{number}: {title}"[:120],
                playbook_id=self._cfg.devin_playbook_id,
                tags=["auto-remediation", f"gh-issue-{number}", f"category:{category}"],
                max_acu_limit=acu_cap,
                structured_output_schema=REMEDIATION_SCHEMA,
                # resumable=False is documented but rejected by the real API
                # (400 "Invalid additional_args key: skip_snapshot_on_sleep",
                # verified 2026-07-13); do not pass it.
            )
        except APIError as exc:
            if exc.status == 429 or exc.status >= 500:
                # Transient, and a non-2xx status proves no session was
                # created: free the row so the next tick retries; other
                # issues may still dispatch fine.
                self._store.delete_task(number)
                self._store.record_event(
                    "session_create_transient", number, error=str(exc)
                )
                return True
            # Definitive rejection (payload/auth/quota): mark failed and
            # halt the tick (see .tick()); retrying an identical payload
            # only multiplies errors in the provider's logs.
            self._store.update_task(
                number,
                status=TaskStatus.FAILED,
                summary=f"session creation rejected: {exc}",
                completed_at=time.time(),
            )
            self._store.record_event(
                "session_create_rejected", number, error=str(exc)
            )
            return False
        except Exception as exc:  # noqa: BLE001, timeout/connection error
            # Ambiguous: the request may have reached Devin, so a session
            # MIGHT exist. Never auto-retry (double-spend risk); the task
            # stays failed for human triage on the dashboard.
            self._store.update_task(
                number,
                status=TaskStatus.FAILED,
                summary=f"session creation failed: {exc}",
                completed_at=time.time(),
            )
            self._store.record_event("session_create_failed", number, error=str(exc))
            return True
        session_url = session.get("url") or session.get("session_url")
        self._store.update_task(
            number,
            status=TaskStatus.DISPATCHED,
            session_id=session["session_id"],
            session_url=session_url,
            dispatched_at=time.time(),
        )
        self._store.record_event(
            "session_dispatched",
            number,
            session_id=session["session_id"],
            acu_cap=acu_cap,
        )
        await self._github.comment(
            number,
            DISPATCH_COMMENT.format(session_url=session_url, acu_cap=acu_cap),
        )
        return True
