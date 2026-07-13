"""Task state (SQLite) + append-only JSONL event log.

The tasks table is the idempotency backbone: issue_number is the primary key,
so an issue can never be dispatched twice no matter how often the poller sees
it. Synchronous sqlite3 called from async loops is deliberate — calls are
sub-millisecond at this scale and keep the code readable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger(__name__)


class TaskStatus(StrEnum):
    ISSUE_FILED = "issue_filed"
    DISPATCHED = "dispatched"
    SESSION_RUNNING = "session_running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Statuses that count against MAX_CONCURRENT_SESSIONS.
ACTIVE_STATUSES = (
    TaskStatus.DISPATCHED,
    TaskStatus.SESSION_RUNNING,
    TaskStatus.RETRYING,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    issue_number  INTEGER PRIMARY KEY,
    issue_url     TEXT NOT NULL,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL,
    status        TEXT NOT NULL,
    session_id    TEXT,
    session_url   TEXT,
    pr_url        TEXT,
    ci_status     TEXT,
    acu_cap       INTEGER,
    acus_consumed REAL,
    retries       INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    created_at    REAL NOT NULL,
    dispatched_at REAL,
    completed_at  REAL
);
CREATE TABLE IF NOT EXISTS events (
    ts           REAL NOT NULL,
    issue_number INTEGER,
    event        TEXT NOT NULL,
    payload      TEXT NOT NULL
);
"""

_TASK_COLUMNS = frozenset(
    {
        "issue_url",
        "title",
        "category",
        "status",
        "session_id",
        "session_url",
        "pr_url",
        "ci_status",
        "acu_cap",
        "acus_consumed",
        "retries",
        "summary",
        "dispatched_at",
        "completed_at",
    }
)


class Store:
    def __init__(self, db_path: str, events_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._events_path = Path(events_path)
        self._events_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create_task(
        self,
        issue_number: int,
        issue_url: str,
        title: str,
        category: str,
        acu_cap: int,
    ) -> bool:
        """Insert a new task; returns False if the issue is already tracked."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(issue_number, issue_url, title, category, status, acu_cap, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_number,
                    issue_url,
                    title,
                    category,
                    TaskStatus.ISSUE_FILED,
                    acu_cap,
                    time.time(),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def update_task(self, issue_number: int, **fields: Any) -> None:
        unknown = set(fields) - _TASK_COLUMNS
        if unknown:
            raise ValueError(f"Unknown task columns: {unknown}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {assignments} WHERE issue_number = ?",  # noqa: S608 — columns whitelisted above
                (*fields.values(), issue_number),
            )
            self._conn.commit()

    def delete_task(self, issue_number: int) -> None:
        """Free a task row so the issue can be re-dispatched. Only safe when
        it is CERTAIN no session exists (a non-2xx status proves it); see the
        dispatcher's error handling for the classification."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM tasks WHERE issue_number = ?", (issue_number,)
            )
            self._conn.commit()

    def get_task(self, issue_number: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE issue_number = ?", (issue_number,)
        ).fetchone()
        return dict(row) if row else None

    def list_tasks(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def tasks_with_status(self, *statuses: TaskStatus) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY created_at",  # noqa: S608
            tuple(statuses),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_count(self) -> int:
        marks = ", ".join("?" for _ in ACTIVE_STATUSES)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({marks})",  # noqa: S608
            tuple(ACTIVE_STATUSES),
        ).fetchone()
        return int(row["n"])

    def record_event(
        self, event: str, issue_number: int | None = None, **payload: Any
    ) -> None:
        entry = {"ts": time.time(), "event": event, "issue_number": issue_number, **payload}
        line = json.dumps(entry, default=str)
        with self._lock:
            with self._events_path.open("a") as fh:
                fh.write(line + "\n")
            self._conn.execute(
                "INSERT INTO events (ts, issue_number, event, payload) VALUES (?, ?, ?, ?)",
                (entry["ts"], issue_number, event, line),
            )
            self._conn.commit()
        # "event" is structlog's reserved message key; log payload under "detail"
        log.info(event, issue_number=issue_number, detail=payload or None)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def close(self) -> None:
        self._conn.close()
