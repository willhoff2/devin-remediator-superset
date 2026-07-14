"""Pipeline tests with fake API clients: dedupe, concurrency gate, finalize."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.config import Config
from src.dispatcher import Dispatcher
from src.http_util import APIError
from src.monitor import Monitor
from src.store import Store, TaskStatus


def make_config(**overrides: Any) -> Config:
    defaults: dict[str, Any] = {
        "devin_api_key": "cog_test",
        "devin_org_id": "org-test",
        "github_token": "ghp_test",
        "github_repo": "willhoff2/superset",
        "devin_api_base": "https://api.devin.example",
        "devin_playbook_id": "pb-test",
        "issue_label": "devin-remediate",
        "done_label": "remediated-pending-merge",
        "poll_interval_issues": 1,
        "poll_interval_sessions": 1,
        "max_concurrent_sessions": 1,
        "max_acu_default": 5,
        "max_acu_medium": 8,
        "ci_checks_enabled": False,
        "devin_review_enabled": False,
        "pr_verify_enabled": True,
        "db_path": "unused",
        "events_path": "unused",
        "dashboard_port": 0,
        "backlog_total": 208,
    }
    return Config(**{**defaults, **overrides})


def make_issue(number: int, effort: str = "default") -> dict[str, Any]:
    labels = [{"name": "devin-remediate"}, {"name": "category:describe-migration"}]
    if effort != "default":
        labels.append({"name": f"effort:{effort}"})
    return {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/willhoff2/superset/issues/{number}",
        "labels": labels,
    }


class FakeSource:
    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues

    async def fetch_candidates(self) -> list[dict[str, Any]]:
        return self.issues


class FakeDevin:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.session_states: dict[str, dict[str, Any]] = {}
        self.messages: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.reviews: list[str] = []

    async def create_session(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        session_id = f"dv-{len(self.created)}"
        self.session_states[session_id] = {"status": "new"}
        return {"session_id": session_id, "url": f"https://app.devin.ai/{session_id}"}

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return self.session_states[session_id]

    async def send_message(self, session_id: str, message: str) -> None:
        self.messages.append((session_id, message))

    async def archive_session(self, session_id: str) -> None:
        self.archived.append(session_id)

    async def create_pr_review(self, pr_url: str) -> dict[str, Any]:
        self.reviews.append(pr_url)
        return {"review_id": f"rev-{len(self.reviews)}"}


class FakeGitHub:
    def __init__(self) -> None:
        self.repo = "willhoff2/superset"
        self.comments: list[tuple[int, str]] = []
        self.labels_added: list[tuple[int, list[str]]] = []
        self.labels_removed: list[tuple[int, str]] = []
        # pr_number -> response dict; missing number raises like a 404
        self.prs: dict[int, dict[str, Any]] = {}

    async def get_pr(self, pr_number: int) -> dict[str, Any]:
        if pr_number not in self.prs:
            raise RuntimeError(f"404: no PR {pr_number}")
        return self.prs[pr_number]

    async def comment(self, issue_number: int, body: str) -> None:
        self.comments.append((issue_number, body))

    async def add_labels(self, issue_number: int, labels: list[str]) -> None:
        self.labels_added.append((issue_number, labels))

    async def remove_label(self, issue_number: int, label: str) -> None:
        self.labels_removed.append((issue_number, label))


@pytest.fixture
def store(tmp_path: Any) -> Store:
    return Store(str(tmp_path / "state.db"), str(tmp_path / "events.jsonl"))


def test_store_dedupes_issues(store: Store) -> None:
    assert store.create_task(1, "u", "t", "c", 5) is True
    assert store.create_task(1, "u", "t", "c", 5) is False


def test_dispatcher_dedupe_and_concurrency_gate(store: Store) -> None:
    cfg = make_config(max_concurrent_sessions=1)
    devin, github = FakeDevin(), FakeGitHub()
    source = FakeSource([make_issue(1), make_issue(2)])
    dispatcher = Dispatcher(cfg, store, source, github, devin)  # type: ignore[arg-type]

    asyncio.run(dispatcher.tick())
    # gate: only issue 1 dispatched
    assert len(devin.created) == 1
    assert store.get_task(1)["status"] == TaskStatus.DISPATCHED
    assert store.get_task(2) is None

    asyncio.run(dispatcher.tick())
    # still gated, and issue 1 not re-dispatched
    assert len(devin.created) == 1

    # issue 1 completes -> gate opens -> issue 2 dispatched, 1 stays done
    store.update_task(1, status=TaskStatus.SUCCEEDED)
    asyncio.run(dispatcher.tick())
    assert len(devin.created) == 2
    assert store.get_task(2)["status"] == TaskStatus.DISPATCHED
    # session creation params carried the guardrails
    assert devin.created[0]["max_acu_limit"] == 5
    assert "gh-issue-1" in devin.created[0]["tags"]
    # the dispatch log line carries the clickable session URL
    dispatched = [e for e in store.recent_events() if e["event"] == "session_dispatched"]
    assert dispatched[0]["session_url"].startswith("https://app.devin.ai/")
    # resumable must NOT be passed: real API 400s on it (see dispatcher)
    assert "resumable" not in devin.created[0]
    assert github.comments[0][0] == 1


def test_dispatcher_effort_label_raises_acu_cap(store: Store) -> None:
    cfg = make_config(max_concurrent_sessions=5)
    devin, github = FakeDevin(), FakeGitHub()
    dispatcher = Dispatcher(
        cfg, store, FakeSource([make_issue(7, effort="medium")]), github, devin  # type: ignore[arg-type]
    )
    asyncio.run(dispatcher.tick())
    assert devin.created[0]["max_acu_limit"] == 8


class ErringDevin(FakeDevin):
    """Raises the given exception for the first N create_session calls."""

    def __init__(self, exc: Exception, fail_first_n: int = 10**9) -> None:
        super().__init__()
        self._exc = exc
        self._fail_first_n = fail_first_n
        self.attempts = 0

    async def create_session(self, **kwargs: Any) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= self._fail_first_n:
            raise self._exc
        return await super().create_session(**kwargs)


def _err(status: int) -> APIError:
    return APIError("POST", "/sessions", status, "boom")


def test_dispatch_rejection_halts_tick(store: Store) -> None:
    """A 4xx rejection means the payload is systemically wrong: the tick
    stops after the first failed call."""
    cfg = make_config(max_concurrent_sessions=5)
    devin = ErringDevin(_err(400))
    dispatcher = Dispatcher(
        cfg, store, FakeSource([make_issue(1), make_issue(2)]), FakeGitHub(), devin  # type: ignore[arg-type]
    )
    asyncio.run(dispatcher.tick())
    assert devin.attempts == 1
    assert store.get_task(1)["status"] == TaskStatus.FAILED
    assert store.get_task(2) is None  # never attempted this tick


def test_dispatch_transient_frees_row_and_continues(store: Store) -> None:
    """5xx/429: no session exists — row freed for retry, others continue."""
    cfg = make_config(max_concurrent_sessions=5)
    devin = ErringDevin(_err(503), fail_first_n=1)
    dispatcher = Dispatcher(
        cfg, store, FakeSource([make_issue(1), make_issue(2)]), FakeGitHub(), devin  # type: ignore[arg-type]
    )
    asyncio.run(dispatcher.tick())
    # issue 1 failed transiently: row freed; issue 2 dispatched same tick
    assert store.get_task(1) is None
    assert store.get_task(2)["status"] == TaskStatus.DISPATCHED

    asyncio.run(dispatcher.tick())
    # next tick retries issue 1 successfully
    assert store.get_task(1)["status"] == TaskStatus.DISPATCHED


def test_dispatch_ambiguous_fails_task_but_continues(store: Store) -> None:
    """Timeout: a session MIGHT exist — never auto-retry, others continue."""
    cfg = make_config(max_concurrent_sessions=5)
    devin = ErringDevin(RuntimeError("connect timeout"), fail_first_n=1)
    dispatcher = Dispatcher(
        cfg, store, FakeSource([make_issue(1), make_issue(2)]), FakeGitHub(), devin  # type: ignore[arg-type]
    )
    asyncio.run(dispatcher.tick())
    assert store.get_task(1)["status"] == TaskStatus.FAILED
    assert store.get_task(2)["status"] == TaskStatus.DISPATCHED

    asyncio.run(dispatcher.tick())
    # failed task is terminal: no re-dispatch, no further attempts for it
    assert store.get_task(1)["status"] == TaskStatus.FAILED
    assert devin.attempts == 2  # one failure + one success, nothing more


def test_monitor_success_path(store: Store) -> None:
    cfg = make_config()
    devin, github = FakeDevin(), FakeGitHub()
    github.prs[9] = {"state": "open"}
    monitor = Monitor(cfg, store, github, devin)  # type: ignore[arg-type]
    store.create_task(1, "u", "t", "describe-migration", 5)
    store.update_task(1, status=TaskStatus.DISPATCHED, session_id="dv-1", session_url="s")
    devin.session_states["dv-1"] = {"status": "running", "status_detail": "working"}

    asyncio.run(monitor.tick())
    assert store.get_task(1)["status"] == TaskStatus.SESSION_RUNNING

    # Devin blocked on a question (idle, incomplete output) is NOT an outcome
    devin.session_states["dv-1"] = {
        "status": "running",
        "status_detail": "waiting_for_user",
        "structured_output": None,
    }
    asyncio.run(monitor.tick())
    assert store.get_task(1)["status"] == TaskStatus.SESSION_RUNNING

    # verified real-API terminal shape: finished sessions never reach
    # status=exit — they idle at running/waiting_for_user with output complete
    devin.session_states["dv-1"] = {
        "status": "running",
        "status_detail": "waiting_for_user",
        "acus_consumed": 2.5,
        "structured_output": {
            "success": True,
            "pr_url": "https://github.com/willhoff2/superset/pull/9",
            "checks_run": ["jest", "pre-commit"],
            "summary": "migrated",
        },
        "pull_requests": [{"pr_url": "https://github.com/willhoff2/superset/pull/9"}],
    }
    asyncio.run(monitor.tick())
    task = store.get_task(1)
    assert task["status"] == TaskStatus.SUCCEEDED
    assert task["pr_url"].endswith("/pull/9")
    assert task["acus_consumed"] == 2.5
    assert github.labels_added == [(1, ["remediated-pending-merge"])]
    assert github.labels_removed == [(1, "devin-remediate")]
    assert "pull/9" in github.comments[-1][1]
    # lifecycle close-out: finished sessions idle forever unless archived
    assert devin.archived == ["dv-1"]
    assert devin.reviews == []  # DEVIN_REVIEW_ENABLED off by default


def test_monitor_success_with_devin_review(store: Store) -> None:
    cfg = make_config(devin_review_enabled=True)
    devin, github = FakeDevin(), FakeGitHub()
    github.prs[9] = {"state": "open"}
    monitor = Monitor(cfg, store, github, devin)  # type: ignore[arg-type]
    store.create_task(1, "u", "t", "describe-migration", 5)
    store.update_task(1, status=TaskStatus.DISPATCHED, session_id="dv-1", session_url="s")
    devin.session_states["dv-1"] = {
        "status": "running",
        "status_detail": "finished",
        "structured_output": {
            "success": True,
            "pr_url": "https://github.com/willhoff2/superset/pull/9",
            "summary": "migrated",
        },
    }
    asyncio.run(monitor.tick())
    assert store.get_task(1)["status"] == TaskStatus.SUCCEEDED
    assert devin.reviews == ["https://github.com/willhoff2/superset/pull/9"]
    assert devin.archived == ["dv-1"]


def test_monitor_rejects_fabricated_pr(store: Store) -> None:
    """success:true with a PR that doesn't exist on GitHub fails the task."""
    cfg = make_config()
    devin, github = FakeDevin(), FakeGitHub()  # github.prs is empty: every PR 404s
    monitor = Monitor(cfg, store, github, devin)  # type: ignore[arg-type]
    store.create_task(1, "u", "t", "c", 5)
    store.update_task(1, status=TaskStatus.DISPATCHED, session_id="dv-1", session_url="s")
    devin.session_states["dv-1"] = {
        "status": "running",
        "status_detail": "finished",
        "structured_output": {
            "success": True,
            "pr_url": "https://github.com/willhoff2/superset/pull/9999",
            "summary": "claims success",
        },
    }
    asyncio.run(monitor.tick())
    task = store.get_task(1)
    assert task["status"] == TaskStatus.FAILED
    assert "failed verification" in github.comments[0][1]


def test_monitor_one_bad_session_does_not_starve_others(store: Store) -> None:
    cfg = make_config()
    devin, github = FakeDevin(), FakeGitHub()
    github.prs[9] = {"state": "open"}
    monitor = Monitor(cfg, store, github, devin)  # type: ignore[arg-type]
    # task 1's session lookup blows up; task 2 finishes normally
    store.create_task(1, "u", "t", "c", 5)
    store.update_task(1, status=TaskStatus.DISPATCHED, session_id="dv-gone", session_url="s")
    store.create_task(2, "u", "t", "c", 5)
    store.update_task(2, status=TaskStatus.DISPATCHED, session_id="dv-2", session_url="s")
    devin.session_states["dv-2"] = {
        "status": "running",
        "status_detail": "finished",
        "structured_output": {
            "success": True,
            "pr_url": "https://github.com/willhoff2/superset/pull/9",
            "summary": "done",
        },
    }
    asyncio.run(monitor.tick())
    assert store.get_task(2)["status"] == TaskStatus.SUCCEEDED


def test_monitor_failure_paths(store: Store) -> None:
    cfg = make_config()
    devin, github = FakeDevin(), FakeGitHub()
    monitor = Monitor(cfg, store, github, devin)  # type: ignore[arg-type]

    # exit without success -> failed with blockers surfaced
    store.create_task(1, "u", "t", "c", 5)
    store.update_task(1, status=TaskStatus.DISPATCHED, session_id="dv-1", session_url="s")
    devin.session_states["dv-1"] = {
        "status": "exit",
        "structured_output": {"success": False, "summary": "s", "blockers": "scope creep"},
    }
    # error status -> failed even with a PR attached
    store.create_task(2, "u", "t", "c", 5)
    store.update_task(2, status=TaskStatus.DISPATCHED, session_id="dv-2", session_url="s")
    devin.session_states["dv-2"] = {
        "status": "error",
        "pull_requests": [{"pr_url": "x"}],
    }
    asyncio.run(monitor.tick())
    assert store.get_task(1)["status"] == TaskStatus.FAILED
    assert store.get_task(2)["status"] == TaskStatus.FAILED
    assert "scope creep" in github.comments[0][1]
