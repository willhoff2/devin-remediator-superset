"""Runner entrypoint: dispatcher, monitor, and dashboard as asyncio tasks
in a single process."""

from __future__ import annotations

import asyncio
import signal

import uvicorn
from dotenv import load_dotenv

from .config import Config
from .dashboard import build_app
from .devin_client import DevinClient
from .dispatcher import Dispatcher
from .github_client import GitHubClient
from .log import configure_logging, get_logger
from .monitor import Monitor
from .sources import PollingSource
from .store import Store

log = get_logger(__name__)


async def amain() -> None:
    cfg = Config.from_env()
    store = Store(cfg.db_path, cfg.events_path)
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    dispatcher = Dispatcher(
        cfg, store, PollingSource(github, cfg.issue_label), github, devin
    )
    monitor = Monitor(cfg, store, github, devin)

    log.info(
        "runner_started",
        repo=cfg.github_repo,
        issue_label=cfg.issue_label,
        max_concurrent=cfg.max_concurrent_sessions,
        ci_checks=cfg.ci_checks_enabled,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(store, cfg),
            host="0.0.0.0",  # noqa: S104 — container-internal bind
            port=cfg.dashboard_port,
            log_level="warning",
        )
    )
    tasks = [
        asyncio.create_task(dispatcher.run_forever(), name="dispatcher"),
        asyncio.create_task(monitor.run_forever(), name="monitor"),
        asyncio.create_task(server.serve(), name="dashboard"),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("runner_stopping")
    server.should_exit = True
    for task in tasks[:2]:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await devin.aclose()
    await github.aclose()
    store.close()


def main() -> None:
    load_dotenv()
    configure_logging()
    asyncio.run(amain())


if __name__ == "__main__":
    main()
