"""Recent saves / answers a user can flip ("Make it a question" / "Save this"), and merges "save separately" can
undo."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

CORRECTION_WINDOW_SECONDS = 600  # for a bare "save" / "ask" correction
MAX_AGE_SECONDS = 7 * 86400  # buttons stop working after this


class Actions:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def record(self, channel: str, user_id: str, kind: str, text: str, entry_id: str | None = None,
               title: str | None = None) -> int:
        """kind: note / question ("Make it a question" / "Save this") or merge ("save separately"; text is the merged
        section and files as JSON).
        title: the saved note's title, so "Make it a question" can tell whether it was renamed
        since."""
        self.conn.execute("DELETE FROM actions WHERE created_at < ?", (self.clock() - MAX_AGE_SECONDS,))
        return self.conn.execute(
            "INSERT INTO actions(channel, user_id, kind, text, entry_id, created_at, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel, user_id, kind, text, entry_id, self.clock(), title)).lastrowid

    def get(self, action_id: int, channel: str, user_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM actions WHERE id = ? AND channel = ? AND user_id = ? AND created_at >= ?",
            (action_id, channel, user_id, self.clock() - MAX_AGE_SECONDS)).fetchone()
        return dict(row) if row else None

    def latest(self, channel: str, user_id: str, within: float = CORRECTION_WINDOW_SECONDS,
               kinds: tuple[str, ...] = ("note", "question")) -> dict | None:
        marks = ", ".join("?" * len(kinds))
        row = self.conn.execute(
            f"SELECT * FROM actions WHERE channel = ? AND user_id = ? AND created_at >= ? AND kind IN ({marks}) "
            "ORDER BY id DESC LIMIT 1", (channel, user_id, self.clock() - within, *kinds)).fetchone()
        return dict(row) if row else None

    def of_entry(self, channel: str, user_id: str, kind: str, entry_id: str) -> list[dict]:
        """The user's live actions of that kind about one entry, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM actions WHERE channel = ? AND user_id = ? AND kind = ? AND entry_id = ? AND created_at >= ? "
            "ORDER BY id", (channel, user_id, kind, entry_id, self.clock() - MAX_AGE_SECONDS)).fetchall()
        return [dict(r) for r in rows]

    def consume(self, action_id: int) -> None:
        self.conn.execute("DELETE FROM actions WHERE id = ?", (action_id,))
