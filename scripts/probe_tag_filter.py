"""Read-only probe: does list_sessions(tags=...) filter server-side?

Reconcile-by-tag (adopting an orphaned session after an ambiguous create
failure) needs the API to filter by tag membership and to return archived
sessions. This checks both against real sessions from past runs.
Costs ~0 ACUs (GETs only).

Usage: .venv/bin/python -m scripts.probe_tag_filter gh-issue-8 [more tags...]
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from src.config import Config
from src.devin_client import DevinClient


async def amain(probe_tags: list[str]) -> int:
    cfg = Config.from_env()
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    try:
        # Verified response shape (2026-07-14): {items, end_cursor,
        # has_next_page, total}; archived sessions are included.
        unfiltered = await devin.list_sessions(limit=100)
        all_sessions = unfiltered["items"]
        print(
            f"unfiltered: {len(all_sessions)} of total={unfiltered['total']}"
        )

        for tag in probe_tags:
            result = await devin.list_sessions(tags=[tag], limit=100)
            sessions = result["items"]
            print(f"\ntags=[{tag}]: {len(sessions)} session(s)")
            for session in sessions:
                print(
                    f"  {session.get('session_id')}  "
                    f"status={session.get('status')}  "
                    f"archived={session.get('is_archived')}  "
                    f"tags={session.get('tags')}"
                )
            expected = [
                s for s in all_sessions if tag in (s.get("tags") or [])
            ]
            filters_serverside = len(sessions) == len(expected) and all(
                tag in (s.get("tags") or []) for s in sessions
            )
            print(
                f"  server-side filter consistent with unfiltered list: "
                f"{filters_serverside} (expected {len(expected)})"
            )
    finally:
        await devin.aclose()
    return 0


if __name__ == "__main__":
    load_dotenv()
    tags = sys.argv[1:] or ["gh-issue-8"]
    sys.exit(asyncio.run(amain(tags)))
