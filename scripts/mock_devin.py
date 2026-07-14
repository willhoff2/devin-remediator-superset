"""Local mock of the Devin v3 Organization API for end-to-end flow rehearsal.

Two jobs:
1. Enforce the verified create-session contract: any payload key outside
   VERIFIED_SESSION_KEYS gets the same 400 shape the real API returns —
   payload drift is caught here, not in Cognition's API logs. (The OpenAPI
   spec over-promises: `resumable` is documented yet 400s in reality, so the
   allowlist is built from keys proven accepted by real successful calls.)
2. Mimic the verified session lifecycle — sessions never reach status=exit;
   they go running/working -> running/finished with structured_output set,
   and archive flips them to suspended.

Usage:
    .venv/bin/uvicorn scripts.mock_devin:app --port 9095
    DEVIN_API_BASE=http://127.0.0.1:9095 python -m src.main
"""

from __future__ import annotations

import itertools
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Keys proven accepted by a real successful POST /sessions. Extend ONLY
# after a real call with the new key has succeeded.
VERIFIED_SESSION_KEYS = frozenset(
    {
        "prompt",
        "repos",
        "title",
        "playbook_id",
        "tags",
        "max_acu_limit",
        "structured_output_schema",
        "structured_output_required",
    }
)

# GET polls per session before it reports finished (~ gives the monitor one
# "working" observation at POLL_INTERVAL_SESSIONS=1).
POLLS_UNTIL_FINISHED = 2

app = FastAPI(title="mock-devin-v3")
_sessions: dict[str, dict[str, Any]] = {}
_pr_counter = itertools.count(9001)


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": detail,
        },
    )


@app.post("/v3/organizations/{org_id}/sessions")
async def create_session(org_id: str, request: Request) -> Any:
    payload = await request.json()
    unknown = set(payload) - VERIFIED_SESSION_KEYS
    if unknown:
        return _bad_request(f"Invalid additional_args key: {sorted(unknown)[0]}")
    if not payload.get("prompt") or not payload.get("repos"):
        return _bad_request("prompt and repos are required")
    session_id = uuid.uuid4().hex
    _sessions[session_id] = {
        "payload": payload,
        "polls": 0,
        "archived": False,
        "pr_number": next(_pr_counter),
    }
    return {
        "session_id": session_id,
        "url": f"http://mock-devin.local/sessions/{session_id}",
        "status": "new",
    }


@app.get("/v3/organizations/{org_id}/sessions/{session_id}")
async def get_session(org_id: str, session_id: str) -> Any:
    s = _sessions[session_id]
    s["polls"] += 1
    repo = (s["payload"].get("repos") or ["owner/repo"])[0]
    pr_url = f"https://github.com/{repo}/pull/{s['pr_number']}"
    base = {
        "session_id": session_id,
        "url": f"http://mock-devin.local/sessions/{session_id}",
        "tags": (s["payload"].get("tags") or []) + ["agent:devin-rs"],
        "is_archived": s["archived"],
        "pull_requests": [],
        "structured_output": None,
    }
    if s["archived"]:
        return {**base, "status": "suspended", "status_detail": "inactivity",
                "acus_consumed": 2.5}
    if s["polls"] < POLLS_UNTIL_FINISHED:
        return {**base, "status": "running", "status_detail": "working",
                "acus_consumed": 0.5}
    return {
        **base,
        "status": "running",
        "status_detail": "finished",
        "acus_consumed": 2.5,
        "structured_output": {
            "success": True,
            "pr_url": pr_url,
            "checks_run": ["jest (mock)", "pre-commit (mock)"],
            "precommit_pass": True,
            "tests_pass": True,
            "summary": "mock remediation complete",
        },
        "pull_requests": [{"pr_url": pr_url}],
    }


@app.post("/v3/organizations/{org_id}/sessions/{session_id}/archive")
async def archive_session(org_id: str, session_id: str) -> Any:
    _sessions[session_id]["archived"] = True
    return {"session_id": session_id, "status": "suspended", "is_archived": True}


@app.post("/v3/organizations/{org_id}/sessions/{session_id}/messages")
async def send_message(org_id: str, session_id: str, request: Request) -> Any:
    return {"session_id": session_id, "status": "running"}


@app.get("/v3/organizations/{org_id}/sessions")
async def list_sessions(org_id: str) -> Any:
    return {"sessions": [], "total": len(_sessions)}


@app.post("/v3/organizations/{org_id}/playbooks")
async def create_playbook(org_id: str, request: Request) -> Any:
    payload = await request.json()
    return {"playbook_id": f"playbook-mock-{uuid.uuid4().hex[:8]}", **payload}


@app.put("/v3/organizations/{org_id}/playbooks/{playbook_id}")
async def update_playbook(org_id: str, playbook_id: str, request: Request) -> Any:
    payload = await request.json()
    return {"playbook_id": playbook_id, **payload}


@app.post("/v3/organizations/{org_id}/pr-reviews")
async def create_pr_review(org_id: str, request: Request) -> Any:
    payload = await request.json()
    return {"review_id": f"rev-mock-{uuid.uuid4().hex[:8]}", **payload}
