"""Fail-fast configuration from environment variables.

Missing required vars raise at load time — the container should die loudly on
misconfiguration, not limp along with defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_REQUIRED = ("DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_TOKEN", "GITHUB_REPO")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Config:
    devin_api_key: str
    devin_org_id: str
    github_token: str
    github_repo: str  # "owner/repo"
    devin_api_base: str
    devin_playbook_id: str | None
    issue_label: str
    done_label: str
    poll_interval_issues: int
    poll_interval_sessions: int
    max_concurrent_sessions: int
    max_acu_default: int
    max_acu_medium: int
    ci_checks_enabled: bool
    db_path: str
    events_path: str
    dashboard_port: int
    backlog_total: int

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in _REQUIRED if not os.getenv(name)]
        if missing:
            raise RuntimeError(
                f"Missing required environment variables: {', '.join(missing)} "
                "(see .env.example)"
            )
        return cls(
            devin_api_key=os.environ["DEVIN_API_KEY"],
            devin_org_id=os.environ["DEVIN_ORG_ID"],
            github_token=os.environ["GITHUB_TOKEN"],
            github_repo=os.environ["GITHUB_REPO"],
            devin_api_base=os.getenv("DEVIN_API_BASE", "https://api.devin.ai"),
            devin_playbook_id=os.getenv("DEVIN_PLAYBOOK_ID") or None,
            issue_label=os.getenv("ISSUE_LABEL", "devin-remediate"),
            done_label=os.getenv("DONE_LABEL", "remediated-pending-merge"),
            poll_interval_issues=_int("POLL_INTERVAL_ISSUES", 30),
            poll_interval_sessions=_int("POLL_INTERVAL_SESSIONS", 15),
            max_concurrent_sessions=_int("MAX_CONCURRENT_SESSIONS", 3),
            max_acu_default=_int("MAX_ACU_DEFAULT", 5),
            max_acu_medium=_int("MAX_ACU_MEDIUM", 8),
            ci_checks_enabled=_bool("CI_CHECKS_ENABLED", False),
            db_path=os.getenv("DB_PATH", "data/state.db"),
            events_path=os.getenv("EVENTS_PATH", "data/events.jsonl"),
            dashboard_port=_int("DASHBOARD_PORT", 8090),
            # Known size of the describe-migration backlog in the target repo;
            # drives the dashboard's throughput/progress bar.
            backlog_total=_int("BACKLOG_TOTAL", 208),
        )
