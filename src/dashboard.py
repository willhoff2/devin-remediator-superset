"""Status page (HTML) + /api/state (JSON)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .store import ACTIVE_STATUSES, Store, TaskStatus

# Cost display. Actual ACUs are used when the org meters them; otherwise we
# estimate from session wall time. Per devin.ai/pricing: 1 ACU is ~15 min of
# active work, $2.25/ACU pay-as-you-go (Core).
ACU_MINUTES = 15
ACU_RATE_USD = 2.25

# Savings counterfactual: US software developer median wage, deliberately
# unloaded (no benefits/overhead) and minute-for-minute — a defensible
# floor. BLS OEWS May 2024: $133,080/yr ≈ $63.98/hr.
DEV_RATE_USD_HOUR = 64

_STATIC = Path(__file__).parent / "static"


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    end = task["completed_at"] or time.time()
    duration = int(end - task["dispatched_at"]) if task["dispatched_at"] else None
    acus = task["acus_consumed"] or 0
    if acus > 0:
        cost, basis = acus * ACU_RATE_USD, "actual"
    elif duration:
        cost, basis = (duration / 60 / ACU_MINUTES) * ACU_RATE_USD, "estimated"
    else:
        cost, basis = None, None
    return {
        **task,
        "duration_s": duration,
        "cost_usd": round(cost, 2) if cost is not None else None,
        "cost_basis": basis,
        "cost_cap_usd": round(task["acu_cap"] * ACU_RATE_USD, 2),
    }


def build_state(store: Store, cfg: Config) -> dict[str, Any]:
    tasks = [_task_view(t) for t in store.list_tasks()]
    succeeded = [t for t in tasks if t["status"] == TaskStatus.SUCCEEDED]
    total_cost = sum(t["cost_usd"] or 0 for t in tasks)
    # Deliberately minute-for-minute (no agent-vs-human speed multiplier):
    # successful task wall time at a developer rate, minus ALL Devin spend,
    # failures included.
    dev_equiv = (
        sum(t["duration_s"] or 0 for t in succeeded) / 3600 * DEV_RATE_USD_HOUR
    )
    return {
        "generated_at": time.time(),
        "repo": cfg.github_repo,
        "summary": {
            "active": sum(1 for t in tasks if t["status"] in ACTIVE_STATUSES),
            "succeeded": len(succeeded),
            "failed": sum(1 for t in tasks if t["status"] == TaskStatus.FAILED),
            "total_cost_usd": round(total_cost, 2),
            "est_saved_usd": round(dev_equiv - total_cost, 2),
            "saved_time_s": sum(t["duration_s"] or 0 for t in succeeded),
            "cost_estimated": any(t["cost_basis"] == "estimated" for t in tasks),
            # The 208-file backlog is describe-migration only; don't count
            # any-cleanup successes against it.
            "backlog_done": sum(
                1 for t in succeeded if t["category"] == "describe-migration"
            ),
            "backlog_total": cfg.backlog_total,
            "ci_checks_enabled": cfg.ci_checks_enabled,
        },
        "tasks": tasks,
        "events": store.recent_events(25),
    }


def build_app(store: Store, cfg: Config) -> FastAPI:
    app = FastAPI(title="devin-remediator", docs_url=None, redoc_url=None)
    # Fails at startup if the directory is missing. The page JS fills the
    # repo name from /api/state, so no server-side templating is needed.
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return build_state(store, cfg)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC / "dashboard.html")

    return app
