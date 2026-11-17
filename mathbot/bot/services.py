"""Async access to the database for command handlers.

Handlers never touch SQLite on the event loop: each call runs in a worker
thread with its own short-lived connection, so a slow query cannot stall the
gateway heartbeat and connections are never shared across threads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from mathbot.db import Repository, connect

T = TypeVar("T")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _run(self, fn: Callable[[Repository], T]) -> T:
        conn = connect(self.path)
        try:
            return fn(Repository(conn))
        finally:
            conn.close()

    async def call(self, fn: Callable[[Repository], T]) -> T:
        return await asyncio.to_thread(self._run, fn)

    def ping(self) -> bool:
        """Synchronous liveness probe used by the health endpoint."""
        try:
            return self._run(lambda repo: repo.conn.execute("SELECT 1").fetchone()[0] == 1)
        except Exception:  # health checks report failure rather than raise
            return False
