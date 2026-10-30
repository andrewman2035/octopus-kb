"""Which entry is waiting for screenshots / recordings / pasted text from a user."""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

MERGE_WINDOW_SECONDS = 1800  # a link's window (and a row from before origins)
FILES_WINDOW_SECONDS = 300  # /new, /add and auto windows: this long after the last file sent
PROMPT_TTL_SECONDS = 7 * 24 * 3600  # a reply to a prompt finds its entry this long
FRESH = "fresh"  # after /new: no entry yet; the first files sent become one and the window moves to it
# what opened the window: a link that could not be read (or only partly) waits for screenshots, pasted text and
# quote-replies; /new and /add collect the files sent one after another (the app ends them on anything else);
# auto: an image sent with no window open, the images sent right after it join it (only images).
# /add and /new windows also take the first plain text sent in them; text_added marks it taken
LINK, ADD, NEW, AUTO = "link", "add", "new", "auto"


class PendingManual:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def set(self, channel: str, user_id: str, entry_id: str, prompt_message_id: str | None = None,
            origin: str | None = None) -> None:
        """origin: LINK, ADD, NEW or AUTO; None keeps the open window's (a merge restarting it, which also keeps
        whether it took its text), LINK if none is open. Any origin given opens a new window: no text taken yet."""
        opened = origin is not None or self.origin(channel, user_id) is None
        origin = origin or self.origin(channel, user_id) or LINK
        self.conn.execute(
            "INSERT INTO pending_manual(channel, user_id, entry_id, prompted_at, prompt_message_id, origin) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(channel, user_id) DO UPDATE SET entry_id = excluded.entry_id, "
            "prompted_at = excluded.prompted_at, prompt_message_id = excluded.prompt_message_id, "
            "origin = excluded.origin, text_added = CASE WHEN ? THEN NULL ELSE text_added END",
            (channel, user_id, entry_id, self.clock(), prompt_message_id, origin, opened),
        )
        if prompt_message_id:
            self._record_prompt(channel, user_id, prompt_message_id, entry_id)

    def _record_prompt(self, channel: str, user_id: str, prompt_message_id: str, entry_id: str) -> None:
        now = self.clock()
        self.conn.execute("DELETE FROM prompts WHERE created_at < ?", (now - PROMPT_TTL_SECONDS,))
        self.conn.execute(
            "INSERT INTO prompts(channel, user_id, prompt_message_id, entry_id, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(channel, user_id, prompt_message_id) DO UPDATE SET entry_id = excluded.entry_id, "
            "created_at = excluded.created_at",
            (channel, user_id, prompt_message_id, entry_id, now),
        )

    def touch(self, channel: str, user_id: str) -> None:
        """Restarts the window's time: a file just came in for it."""
        self.conn.execute("UPDATE pending_manual SET prompted_at = ? WHERE channel = ? AND user_id = ?",
                          (self.clock(), channel, user_id))

    def mark_text_added(self, channel: str, user_id: str) -> None:
        """The open /add or /new window took its one text: the next text ends it (kept across a restart)."""
        self.conn.execute("UPDATE pending_manual SET text_added = 1 WHERE channel = ? AND user_id = ?",
                          (channel, user_id))

    def text_added(self, channel: str, user_id: str) -> bool:
        """The open window already took its one text (False when it is closed)."""
        if self._window(channel, user_id) is None:
            return False
        row = self.conn.execute("SELECT text_added FROM pending_manual WHERE channel = ? AND user_id = ?",
                                (channel, user_id)).fetchone()
        return bool(row and row[0])

    def _window(self, channel: str, user_id: str) -> tuple[str, float, str] | None:
        row = self.conn.execute(
            "SELECT entry_id, prompted_at, origin FROM pending_manual WHERE channel = ? AND user_id = ?",
            (channel, user_id),
        ).fetchone()
        if not row:
            return None
        origin = row[2] or LINK  # a row from before origins: a link's window
        ttl = MERGE_WINDOW_SECONDS if origin == LINK else FILES_WINDOW_SECONDS
        return (row[0], row[1], origin) if row[1] >= self.clock() - ttl else None

    def active(self, channel: str, user_id: str) -> str | None:
        """The entry the open window adds to; None when closed or still fresh (see FRESH)."""
        win = self._window(channel, user_id)
        return win[0] if win and win[0] != FRESH else None

    def fresh(self, channel: str, user_id: str) -> bool:
        """/new opened a window that has no entry yet."""
        win = self._window(channel, user_id)
        return bool(win) and win[0] == FRESH

    def opened_at(self, channel: str, user_id: str) -> float | None:
        """When the open window (fresh or not) was last set, None when it is closed."""
        win = self._window(channel, user_id)
        return win[1] if win else None

    def origin(self, channel: str, user_id: str) -> str | None:
        """What opened the open window (fresh or not): LINK, ADD, NEW or AUTO; None when it is closed."""
        win = self._window(channel, user_id)
        return win[2] if win else None

    def for_reply(self, channel: str, user_id: str, reply_to_id: str) -> str | None:
        """The entry a prompt message asked material for (any prompt of the last 7 days, not just the latest)."""
        row = self.conn.execute(
            "SELECT entry_id FROM prompts WHERE channel = ? AND user_id = ? AND prompt_message_id = ? "
            "AND created_at >= ?",
            (channel, user_id, reply_to_id, self.clock() - PROMPT_TTL_SECONDS),
        ).fetchone()
        return row[0] if row else None

    def clear(self, channel: str, user_id: str) -> None:
        self.conn.execute("DELETE FROM pending_manual WHERE channel = ? AND user_id = ?", (channel, user_id))

    def close_link_windows(self, entry_id: str) -> None:
        """Every user's window that a link opened on this entry ends: the entry was filled (P36 fix round 1)."""
        self.conn.execute("DELETE FROM pending_manual WHERE entry_id = ? AND (origin IS NULL OR origin = ?)",
                          (entry_id, LINK))
