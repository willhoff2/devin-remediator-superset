"""Dump the full JSON of a Devin session — debugging aid for waiting_for_user etc.

Usage:
    python -m scripts.session_dump <session_id>
"""

from __future__ import annotations

import asyncio
import json
import sys

from dotenv import load_dotenv

from src.config import Config
from src.devin_client import DevinClient


async def main(session_id: str) -> int:
    cfg = Config.from_env()
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    try:
        state = await devin.get_session(session_id)
    finally:
        await devin.aclose()
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    load_dotenv()
    sys.exit(asyncio.run(main(sys.argv[1])))
