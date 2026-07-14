"""Dashboard state assembly + endpoints against a seeded store."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from src.dashboard import DEV_RATE_USD_HOUR, build_app, build_state
from src.store import Store, TaskStatus

from .test_pipeline import make_config


@pytest.fixture
def seeded_store(tmp_path: Any) -> Store:
    store = Store(str(tmp_path / "state.db"), str(tmp_path / "events.jsonl"))
    store.create_task(1, "u1", "migrate test A", "describe-migration", 5)
    store.update_task(
        1,
        status=TaskStatus.SUCCEEDED,
        pr_url="https://github.com/x/pull/1",
        acus_consumed=2.5,
        dispatched_at=time.time() - 300,
        completed_at=time.time() - 60,
    )
    store.create_task(2, "u2", "fix any types", "any-cleanup", 8)
    store.update_task(
        2, status=TaskStatus.SESSION_RUNNING, dispatched_at=time.time() - 30
    )
    store.record_event("task_succeeded", 1, pr_url="https://github.com/x/pull/1")
    return store


def test_build_state_summary(seeded_store: Store) -> None:
    state = build_state(seeded_store, make_config())
    summary = dict(state["summary"])
    total_cost = summary.pop("total_cost_usd")
    saved = summary.pop("est_saved_usd")
    assert summary == {
        "active": 1,
        "succeeded": 1,
        "failed": 0,
        "cost_estimated": True,  # task 2 is running with no ACUs yet
        "backlog_done": 1,
        "backlog_total": 208,
        "ci_checks_enabled": False,
    }
    assert total_cost == round(sum(t["cost_usd"] for t in state["tasks"]), 2)
    by_issue = {t["issue_number"]: t for t in state["tasks"]}
    # Savings: succeeded wall time only (task 1), minus ALL Devin spend.
    assert saved == round(
        by_issue[1]["duration_s"] / 3600 * DEV_RATE_USD_HOUR - total_cost, 2
    )
    assert by_issue[1]["duration_s"] == 240
    assert by_issue[2]["duration_s"] is not None  # running: duration accrues
    # task 1 has metered ACUs: cost is actual; task 2 estimates from wall time
    assert by_issue[1]["cost_basis"] == "actual"
    assert by_issue[1]["cost_usd"] == round(2.5 * 2.25, 2)
    assert by_issue[1]["cost_cap_usd"] == round(5 * 2.25, 2)  # dollar ceiling
    assert by_issue[2]["cost_basis"] == "estimated"
    assert state["events"][0]["event"] == "task_succeeded"


def test_endpoints_serve(seeded_store: Store) -> None:
    app = build_app(seeded_store, make_config())

    async def hit() -> tuple[httpx.Response, ...]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return (
                await client.get("/api/state"),
                await client.get("/"),
                await client.get("/static/dashboard.css"),
                await client.get("/static/dashboard.js"),
            )

    api, page, css, js = asyncio.run(hit())
    assert api.status_code == 200
    assert api.json()["summary"]["succeeded"] == 1
    # The page JS renders the repo name from this field.
    assert api.json()["repo"] == make_config().github_repo
    assert page.status_code == 200
    assert "Devin Remediator" in page.text
    assert css.status_code == 200
    assert js.status_code == 200
