"""Event sources feeding remediation candidates (GitHub issues) to the dispatcher.

Polling is the default trigger: it's one of the brief's sanctioned event types,
needs no public endpoint, and cannot miss events because listing is idempotent.
WebhookSource marks the production seam for real-time triggering.
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
        return await self._github.list_open_issues(self._label)


class WebhookSource(EventSource):
    """Production path: an HTTP endpoint receiving GitHub `issues` webhooks
    (HMAC-SHA256 signature-verified) and enqueueing their payloads, so
    dispatch latency is push-driven instead of poll-interval-bound.

    Deliberately unimplemented in this demo — it requires a public endpoint
    (tunnel or deployment), which adds a failure mode without changing the
    architecture: the dispatcher consumes either source identically.
    """

    async def fetch_candidates(self) -> list[dict[str, Any]]:
        raise NotImplementedError("See class docstring — use PollingSource")
