"""Short, bounded, per-operation WAL readers protected against file replacement."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TypeVar
from zoneinfo import ZoneInfo

from core.instance import AlreadyRunning, database_replacement_lock
from core.package_selection import (
    PackageEstimate,
    PackageFilters,
    estimate_package,
    list_counties,
)
from core.storage import SCHEMA_VERSION

T = TypeVar("T")


class EstimateUnavailable(RuntimeError):
    pass


class Estimator:
    def __init__(self, database_path: Path | None, *, query_seconds: float = 0.5):
        self.database_path = database_path
        self.query_seconds = query_seconds
        self._slots = threading.BoundedSemaphore(4)

    def _read(self, action: Callable[[sqlite3.Connection], T]) -> T:
        if self.database_path is None or not self._slots.acquire(blocking=False):
            raise EstimateUnavailable
        try:
            path = self.database_path.resolve(strict=True)
            if not path.is_file():
                raise EstimateUnavailable
            with database_replacement_lock(path):
                # mode=ro cannot create a missing file or migrate it. No engine lock is required.
                conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05, isolation_level=None)
                try:
                    deadline = time.monotonic() + self.query_seconds
                    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN")
                    if (conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                            or conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"):
                        raise EstimateUnavailable
                    return action(conn)
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, AlreadyRunning) as exc:
            raise EstimateUnavailable from exc
        finally:
            self._slots.release()

    def estimate(self, filters: PackageFilters) -> PackageEstimate:
        today = datetime.now(ZoneInfo("Europe/Warsaw")).date()
        return self._read(lambda conn: estimate_package(conn, filters, today=today))

    def counties(self) -> tuple[tuple[str, str], ...]:
        return self._read(list_counties)
