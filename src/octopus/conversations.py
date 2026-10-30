"""Recent Q&A turns per channel user, fed back into prompts."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable


class Conversations:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def add(self, channel: str, user_id: str, role: str, text: str) -> None:
        self.conn.execute(
            "INSERT INTO conversations(channel, user_id, role, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (channel, user_id, role, text, self.clock()),
        )

    def last_reply(self, channel: str, user_id: str) -> str | None:
        """The latest assistant turn: what the user last read as an answer in this chat."""
        row = self.conn.execute(
            "SELECT text FROM conversations WHERE channel = ? AND user_id = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1", (channel, user_id)).fetchone()
        return row[0] if row else None

    def recent(self, channel: str, user_id: str, turns: int = 3) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT role, text FROM conversations WHERE channel = ? AND user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (channel, user_id, turns * 2),
        ).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]
