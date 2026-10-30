"""Replies that could not be delivered; re-sent when the user next writes and by App's periodic sweep."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

MAX_AGE_SECONDS = 7 * 86400


class Outbox:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def _prune(self) -> None:
        self.conn.execute("DELETE FROM outbox WHERE created_at < ?", (self.clock() - MAX_AGE_SECONDS,))

    def add(self, channel: str, user_id: str, text: str, created_at: float | None = None,
            op_id: int | None = None) -> None:
        """created_at: when re-queuing a failed flush, the oldest original time, so the 7-day TTL doesn't restart.
        op_id: the text asks to confirm that op; it replaces an earlier held question for the same op."""
        self._prune()
        if op_id is not None:
            self.conn.execute("DELETE FROM outbox WHERE channel = ? AND user_id = ? AND op_id = ?",
                              (channel, user_id, op_id))
        self.conn.execute("INSERT INTO outbox(channel, user_id, text, created_at, op_id) VALUES (?, ?, ?, ?, ?)",
                          (channel, user_id, text, self.clock() if created_at is None else created_at, op_id))

    def take_entries(self, channel: str, user_id: str) -> list[tuple[str, float]]:
        """(text, created_at) oldest first; removes them."""
        self._prune()
        rows = self.conn.execute(
            "SELECT id, text, created_at FROM outbox WHERE channel = ? AND user_id = ? ORDER BY id",
            (channel, user_id)).fetchall()
        if rows:
            self.conn.execute(f"DELETE FROM outbox WHERE id IN ({','.join('?' * len(rows))})", [r[0] for r in rows])
        return [(r[1], r[2]) for r in rows]

    def queued(self, channel: str, user_id: str) -> list[tuple[int, str, int | None]]:
        """(id, text, op_id) oldest first, left in place: each one is removed only once it was delivered."""
        self._prune()
        rows = self.conn.execute("SELECT id, text, op_id FROM outbox WHERE channel = ? AND user_id = ? ORDER BY id",
                                 (channel, user_id)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def holds_op(self, channel: str, user_id: str, op_id: int) -> bool:
        """The confirmation of op_id is still queued: the user has not seen it."""
        return self.conn.execute("SELECT 1 FROM outbox WHERE channel = ? AND user_id = ? AND op_id = ? LIMIT 1",
                                 (channel, user_id, op_id)).fetchone() is not None

    def pending_users(self) -> list[tuple[str, str]]:
        """(channel, user_id) of everyone with queued replies, the longest waiting first."""
        self._prune()
        rows = self.conn.execute(
            "SELECT channel, user_id FROM outbox GROUP BY channel, user_id ORDER BY min(id)").fetchall()
        return [(r[0], r[1]) for r in rows]

    def remove(self, entry_id: int) -> None:
        self.conn.execute("DELETE FROM outbox WHERE id = ?", (entry_id,))

    def take(self, channel: str, user_id: str) -> list[str]:
        return [text for text, _ in self.take_entries(channel, user_id)]

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
