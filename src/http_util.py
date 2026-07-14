"""Shared HTTP request helper with retry/backoff.

Devin publishes no numeric rate limits (429 is documented, numbers are not),
so both API clients funnel through this: exponential backoff, Retry-After
honored when present.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .log import get_logger

log = get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class APIError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")
        self.status = status


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    max_attempts: int = 4,
    retry_statuses: frozenset[int] = RETRY_STATUSES,
) -> Any:
    for attempt in range(1, max_attempts + 1):
        resp = await client.request(method, path, json=json, params=params)
        if resp.status_code in retry_statuses and attempt < max_attempts:
            delay = float(resp.headers.get("Retry-After", 2**attempt))
            log.warning(
                "http_retry",
                method=method,
                path=path,
                status=resp.status_code,
                attempt=attempt,
                delay_s=delay,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code >= 400:
            raise APIError(method, str(resp.url), resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()
    raise AssertionError("unreachable")  # loop always returns or raises
