"""Thin typed client for the Devin v3 Organization API.

Docs: https://docs.devin.ai/api-reference/overview
All paths are relative to /v3/organizations/{org_id}.
"""

from __future__ import annotations

from typing import Any

import httpx

from .http_util import request_json

# Terminal values of the v3 session `status` field.
TERMINAL_STATUSES = frozenset({"exit", "error", "suspended"})

# status_detail values where Devin has stopped working and is idling.
IDLE_DETAILS = frozenset({"waiting_for_user", "sleeping"})


def session_reached_outcome(
    state: dict[str, Any], required_fields: list[str]
) -> bool:
    """Whether a session has delivered everything it's going to.

    Verified against the real API (2026-07-12): a session that finishes its
    task does NOT transition to `exit` — it idles at status=running,
    status_detail=waiting_for_user, even if asked to end. So "done" means a
    hard-terminal status, or idling with every required structured-output
    field present. Idling WITHOUT complete output means Devin is blocked on
    a question — not an outcome.
    """
    if state.get("status") in TERMINAL_STATUSES:
        return True
    if state.get("status_detail") in IDLE_DETAILS:
        output = state.get("structured_output") or {}
        return all(output.get(field) is not None for field in required_fields)
    return False


class DevinClient:
    def __init__(
        self,
        api_key: str,
        org_id: str,
        base_url: str = "https://api.devin.ai",
        timeout: float = 30.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/v3/organizations/{org_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    async def create_session(
        self,
        *,
        prompt: str,
        repos: list[str],
        title: str | None = None,
        playbook_id: str | None = None,
        tags: list[str] | None = None,
        max_acu_limit: int | None = None,
        structured_output_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt, "repos": repos}
        if title:
            payload["title"] = title
        if playbook_id:
            payload["playbook_id"] = playbook_id
        if tags:
            payload["tags"] = tags
        if max_acu_limit is not None:
            payload["max_acu_limit"] = max_acu_limit
        if structured_output_schema is not None:
            payload["structured_output_schema"] = structured_output_schema
            payload["structured_output_required"] = True
        return await request_json(self._client, "POST", "/sessions", json=payload)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await request_json(self._client, "GET", f"/sessions/{session_id}")

    async def send_message(self, session_id: str, message: str) -> Any:
        return await request_json(
            self._client,
            "POST",
            f"/sessions/{session_id}/messages",
            json={"message": message},
        )

    async def list_sessions(
        self, tags: list[str] | None = None, limit: int = 100
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        if tags:
            params["tags"] = ",".join(tags)
        return await request_json(self._client, "GET", "/sessions", params=params)

    async def create_playbook(self, title: str, body: str) -> dict[str, Any]:
        return await request_json(
            self._client,
            "POST",
            "/playbooks",
            json={"title": title, "body": body},
        )

    async def aclose(self) -> None:
        await self._client.aclose()
