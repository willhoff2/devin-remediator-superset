"""Serve the dashboard with synthetic seeded tasks — zero network, zero state.

Serves only the dashboard app (no dispatcher/monitor, no GitHub/Devin
clients), backed by a throwaway SQLite file with a representative task mix.
Lets UI work and scripts/verify_dashboard_ui.mjs run without credentials or
a real state.db.

Usage:
    .venv/bin/python -m scripts.preview_dashboard [--port 8091]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# Support `python scripts/preview_dashboard.py` as well as `-m scripts.…`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

_PREVIEW_ENV = {
    "DEVIN_API_KEY": "preview",
    "DEVIN_ORG_ID": "preview",
    "GITHUB_TOKEN": "preview",
    "GITHUB_REPO": "preview/superset",
}


def seed(store) -> None:  # noqa: ANN001, imported after env setup
    from src.store import TaskStatus

    now = time.time()
    repo = _PREVIEW_ENV["GITHUB_REPO"]
    tasks = [
        # issue, title, category, cap, status, duration_s
        (8, "test(dashboard): migrate getLeafComponentIdFromPath.test.ts "
            "off describe() block", "describe-migration", 5,
         TaskStatus.SUCCEEDED, 480),
        (9, "test(utils): migrate parseCookie.test.ts off describe() block",
         "describe-migration", 5, TaskStatus.SUCCEEDED, 420),
        (12, "refactor(home): type file-extension config arrays as string[]",
         "any-cleanup", 8, TaskStatus.SUCCEEDED, 500),
        (13, "refactor(explore): remove explicit any usages",
         "any-cleanup", 8, TaskStatus.FAILED, 300),
    ]
    for issue, title, category, cap, status, duration in tasks:
        url = f"https://github.com/{repo}/issues/{issue}"
        store.create_task(issue, url, title, category, cap)
        done = status == TaskStatus.SUCCEEDED
        store.update_task(
            issue,
            status=status,
            session_url=f"https://app.devin.ai/sessions/preview{issue}",
            pr_url=f"https://github.com/{repo}/pull/{issue + 10}" if done else None,
            dispatched_at=now - duration - 30,
            completed_at=now - 30,
        )
        store.record_event(
            "task_succeeded" if done else "task_failed", issue
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()

    tmp = tempfile.mkdtemp(prefix="dashboard-preview-")
    os.environ.update(_PREVIEW_ENV)
    os.environ["DB_PATH"] = f"{tmp}/state.db"
    os.environ["EVENTS_PATH"] = f"{tmp}/events.jsonl"

    from src.config import Config
    from src.dashboard import build_app
    from src.store import Store

    cfg = Config.from_env()
    store = Store(cfg.db_path, cfg.events_path)
    seed(store)
    print(f"preview dashboard (synthetic data): http://localhost:{args.port}")
    uvicorn.run(build_app(store, cfg), port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
