"""The entry the last reply was about, per channel user: what "delete the one above" refers to."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

FOCUS_TTL_SECONDS = 24 * 3600


class Focus:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def set(self, channel: str, user_id: str, entry_id: str) -> None:
        self.conn.execute(
            "INSERT INTO focus(channel, user_id, entry_id, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel, user_id) DO UPDATE SET entry_id = excluded.entry_id, "
            "updated_at = excluded.updated_at",
            (channel, user_id, entry_id, self.clock()),
        )

    def get(self, channel: str, user_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT entry_id FROM focus WHERE channel = ? AND user_id = ? AND updated_at >= ?",
            (channel, user_id, self.clock() - FOCUS_TTL_SECONDS),
        ).fetchone()
        return row[0] if row else None

    def clear(self, channel: str, user_id: str, entry_id: str | None = None) -> None:
        """Forget the focus; with entry_id only when it is still that entry."""
        if entry_id is None:
            self.conn.execute("DELETE FROM focus WHERE channel = ? AND user_id = ?", (channel, user_id))
        else:
            self.conn.execute("DELETE FROM focus WHERE channel = ? AND user_id = ? AND entry_id = ?",
                              (channel, user_id, entry_id))
