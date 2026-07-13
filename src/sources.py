"""Event sources feeding remediation candidates (GitHub issues) to the dispatcher.

Polling is the default trigger: no public endpoint needed, and it can't miss
events because listing is idempotent. WebhookSource is where real-time
triggering slots in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .github_client import GitHubClient


class EventSource(ABC):
    @abstractmethod
    async def fetch_candidates(self) -> list[dict[str, Any]]:
        """Return open issues currently labeled for remediation."""


class PollingSource(EventSource):
    def __init__(self, github: GitHubClient, label: str) -> None:
        self._github = github
        self._label = label

    async def fetch_candidates(self) -> list[dict[str, Any]]:
        return await self._github.list_issues(self._label, state="open")


class WebhookSource(EventSource):
    """Production path: an HTTP endpoint receiving GitHub `issues` webhooks
    (HMAC-SHA256 signature-verified) and enqueueing their payloads, so
    dispatch latency is push-driven instead of poll-interval-bound.

    Not implemented here: it needs a public endpoint. The dispatcher
    consumes either source identically.
    """

    async def fetch_candidates(self) -> list[dict[str, Any]]:
        raise NotImplementedError("See class docstring — use PollingSource")
