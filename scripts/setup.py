"""One-time setup and smoke checks against the real APIs.

Usage:
    python -m scripts.setup validate   # env vars, Devin auth, GitHub repo access
    python -m scripts.setup playbook   # create the Devin playbook from playbook.md
    python -m scripts.setup smoke      # fire the environment-cost gate session

`smoke` is the go/no-go gate for the whole design: it measures whether Devin
can run one jest file + pre-commit from the warm machine snapshot in minutes,
not tens of minutes. If it can't, the per-issue verification story changes
(see docs/spec.md) — run it before trusting anything downstream.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from src.config import Config
from src.devin_client import DevinClient, session_reached_outcome
from src.github_client import GitHubClient
from src.log import configure_logging, get_logger
from src.schemas import SMOKE_SCHEMA

log = get_logger(__name__)

SMOKE_PROMPT = """\
This is a READ-ONLY environment probe of the repository {repo}. Do not modify
any files, do not create branches, do not push, do not open PRs.

Working from your machine snapshot for this repository, measure and report:

1. Whether superset-frontend/node_modules already exists and is usable
   (node_modules_present). If it does not, run `npm ci` in superset-frontend/
   and record how long it took in seconds (npm_install_seconds); otherwise
   set npm_install_seconds to null.
2. Time this command from superset-frontend/ and record jest_seconds and
   jest_passed:
       npm run test -- src/utils/parseCookie.test.ts
3. Whether `pre-commit` is installed and usable in the snapshot
   (precommit_available). If it is, time this from the repository root and
   record precommit_seconds and precommit_passed:
       pre-commit run --files superset-frontend/src/utils/parseCookie.test.ts
   If it is not available, set precommit_seconds and precommit_passed to null
   and say what's missing in notes.

Report everything via structured output. Put anything surprising about the
environment (missing tools, slow steps, version issues) in notes.
"""


async def cmd_validate(cfg: Config) -> int:
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    github = GitHubClient(cfg.github_token, cfg.github_repo)
    ok = True
    try:
        await devin.list_sessions(limit=1)
        print(f"[ok] Devin API auth + org access ({cfg.devin_org_id})")
    except Exception as exc:  # noqa: BLE001 — validation reports, doesn't crash
        print(f"[FAIL] Devin API: {exc}")
        ok = False
    try:
        repo = await github.get_repo()
        print(f"[ok] GitHub repo access: {repo['full_name']} (fork={repo['fork']})")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] GitHub API: {exc}")
        ok = False
    if not cfg.devin_playbook_id:
        print("[warn] DEVIN_PLAYBOOK_ID not set — run `python -m scripts.setup playbook`")
    await devin.aclose()
    await github.aclose()
    return 0 if ok else 1


async def cmd_playbook(cfg: Config) -> int:
    instructions = Path("playbook.md").read_text()
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    try:
        result = await devin.create_playbook(
            "superset-issue-remediation", instructions
        )
    finally:
        await devin.aclose()
    print(json.dumps(result, indent=2))
    playbook_id = result.get("playbook_id") or result.get("id")
    print(f"\nSet DEVIN_PLAYBOOK_ID={playbook_id} in .env")
    return 0


async def cmd_smoke(cfg: Config) -> int:
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    try:
        session = await devin.create_session(
            prompt=SMOKE_PROMPT.format(repo=cfg.github_repo),
            repos=[cfg.github_repo],
            title="smoke: environment cost gate",
            tags=["auto-remediation", "smoke"],
            max_acu_limit=cfg.max_acu_default,
            structured_output_schema=SMOKE_SCHEMA,
        )
        session_id = session["session_id"]
        print(f"Session created: {session.get('url', session_id)}")

        started = time.time()
        last_status = None
        while True:
            await asyncio.sleep(cfg.poll_interval_sessions)
            state = await devin.get_session(session_id)
            status = state.get("status")
            detail = state.get("status_detail")
            if (status, detail) != last_status:
                elapsed = int(time.time() - started)
                print(f"[{elapsed:>4}s] status={status} detail={detail}")
                last_status = (status, detail)
            if session_reached_outcome(state, SMOKE_SCHEMA["required"]):
                break

        print("\n--- structured_output ---")
        print(json.dumps(state.get("structured_output"), indent=2))
        print(f"\nacus_consumed: {state.get('acus_consumed')}")
        print(f"wall time: {int(time.time() - started)}s")

        output = state.get("structured_output") or {}
        gate = output.get("jest_passed") is True
        print(f"\nGATE {'PASSED' if gate else 'FAILED'} — see docs/spec.md prereqs")
        return 0 if gate else 1
    finally:
        await devin.aclose()


def main() -> int:
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "playbook", "smoke"])
    args = parser.parse_args()
    cfg = Config.from_env()
    handler = {
        "validate": cmd_validate,
        "playbook": cmd_playbook,
        "smoke": cmd_smoke,
    }[args.command]
    return asyncio.run(handler(cfg))


if __name__ == "__main__":
    sys.exit(main())
