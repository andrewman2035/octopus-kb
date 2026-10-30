"""Record changes asked for in chat, waiting for the user's confirmation."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Callable

OP_TTL_SECONDS = 600


class Ops:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def add(self, channel: str, user_id: str, kind: str, entry_ids: list[str], arg: str, text: str = "") -> int:
        """text: the confirmation as it was asked, to ask it again word for word when it was interrupted."""
        self.conn.execute("DELETE FROM ops WHERE created_at < ?", (self.clock() - OP_TTL_SECONDS,))
        return self.conn.execute(
            "INSERT INTO ops(channel, user_id, kind, entry_ids, arg, created_at, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel, user_id, kind, json.dumps(entry_ids), arg, self.clock(), text)).lastrowid

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        op = dict(row)
        op["entry_ids"] = json.loads(op["entry_ids"])
        return op

    def take(self, op_id: int, channel: str, user_id: str) -> dict | None:
        """The op, removed so it runs once; None when it is someone else's, expired or already used."""
        op = self._row(self.conn.execute(
            "SELECT * FROM ops WHERE id = ? AND channel = ? AND user_id = ? AND created_at >= ?",
            (op_id, channel, user_id, self.clock() - OP_TTL_SECONDS)).fetchone())
        if op is None:
            return None
        # rowcount, not the SELECT, decides: of two presses at once only one gets the op
        if self.conn.execute("DELETE FROM ops WHERE id = ?", (op_id,)).rowcount != 1:
            return None
        return op

    def latest(self, channel: str, user_id: str) -> dict | None:
        return self._row(self.conn.execute(
            "SELECT * FROM ops WHERE channel = ? AND user_id = ? AND created_at >= ? ORDER BY id DESC LIMIT 1",
            (channel, user_id, self.clock() - OP_TTL_SECONDS)).fetchone())

    def drop(self, op_id: int) -> None:
        self.conn.execute("DELETE FROM ops WHERE id = ?", (op_id,))
