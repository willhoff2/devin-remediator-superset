"""Send a message to a running Devin session.

Usage:
    python -m scripts.session_msg <session_id> "message text"
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from src.config import Config
from src.devin_client import DevinClient


async def main(session_id: str, message: str) -> int:
    cfg = Config.from_env()
    devin = DevinClient(cfg.devin_api_key, cfg.devin_org_id, cfg.devin_api_base)
    try:
        result = await devin.send_message(session_id, message)
    finally:
        await devin.aclose()
    print(result)
    return 0


if __name__ == "__main__":
    load_dotenv()
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
