"""Persistent save-job queue with retry schedule."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from octopus.db import transaction
from octopus.i18n import aliases, t

RETRY_DELAYS = [60, 300, 1800]
__getattr__ = aliases({"CRASH_LOOP_ERROR": "jobs.crash_loop"})


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    channel: str
    user_id: str
    status: str
    attempts: int
    last_error: str | None
    entry_id: str | None
    created_at: float = 0.0


def _job(row: sqlite3.Row) -> Job:
    return Job(row["id"], row["kind"], json.loads(row["payload"]), row["channel"], row["user_id"],
               row["status"], row["attempts"], row["last_error"], row["entry_id"], row["created_at"])


class JobQueue:
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def enqueue(self, kind: str, payload: dict, channel: str, user_id: str) -> int:
        now = self.clock()
        cur = self.conn.execute(
            "INSERT INTO jobs(kind, payload, channel, user_id, status, next_run_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), channel, user_id, now, now, now),
        )
        return cur.lastrowid

    def staged_paths(self) -> set[str]:
        """The files queued or running jobs will still read (their staged copies must stay): attachments, and the
        page the Chrome extension sent."""
        paths = set()
        for (payload,) in self.conn.execute("SELECT payload FROM jobs WHERE status IN ('queued', 'running')"):
            try:
                data = json.loads(payload)
                files = data.get("files") or []
                page = data.get("page")
            except (ValueError, AttributeError):
                continue
            paths.update(f["path"] for f in files if isinstance(f, dict) and isinstance(f.get("path"), str))
            if isinstance(page, str):
                paths.add(page)
        return paths

    def has_pending_url(self, url: str) -> bool:
        """True if a url or refetch job for this (normalized) url is queued, waiting for retry, or running."""
        return self.pending_url_job(url) is not None

    def pending_url_job(self, url: str) -> int | None:
        """The url or refetch job for this (normalized) url that is queued, waiting for retry, or running."""
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE kind IN ('url', 'refetch') AND status IN ('queued', 'running') "
            "AND json_extract(payload, '$.url') = ? ORDER BY id LIMIT 1", (url,)
        ).fetchone()
        return row[0] if row else None

    def redirect_merges(self, channel: str, user_id: str, old_id: str, new_id: str, since_job: int) -> int:
        """After "save separately" the rest of that article's screenshots join the new entry: the user's merge jobs into
        old_id that are not finished (queued, waiting for a retry, or running), came in through the merge
        window (not /add) and were queued no earlier than job since_job (the split batch's first) now target
        new_id."""
        return self.conn.execute(
            "UPDATE jobs SET payload = json_set(payload, '$.entry_id', ?), updated_at = ? WHERE kind = 'merge' "
            "AND channel = ? AND user_id = ? AND status IN ('queued', 'running') "
            "AND json_extract(payload, '$.entry_id') = ? AND json_extract(payload, '$.via') = 'window' "
            "AND created_at >= (SELECT created_at FROM jobs WHERE id = ?)",
            (new_id, self.clock(), channel, user_id, old_id, since_job)).rowcount

    def creating(self, channel: str, user_id: str, entry_id: str, files: bool = False) -> bool:
        """An attachments job of the user that will save entry_id is queued, waiting for a retry, or running.
        files: only one that has files (not the text that started a /new entry)."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'attachments' AND channel = ? AND user_id = ? "
            "AND status IN ('queued', 'running') AND json_extract(payload, '$.entry_id') = ? "
            + ("AND json_array_length(payload, '$.files') > 0 " if files else "") + "LIMIT 1",
            (channel, user_id, entry_id)).fetchone()
        return row is not None

    def window_files(self, channel: str, user_id: str, entry_id: str, since: float) -> bool:
        """The user sent files for entry_id through the merge window (not /add <id> text) since then."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE kind = 'merge' AND channel = ? AND user_id = ? AND created_at >= ? "
            "AND json_extract(payload, '$.entry_id') = ? AND json_extract(payload, '$.via') = 'window' "
            "AND json_array_length(payload, '$.files') > 0 LIMIT 1",
            (channel, user_id, since, entry_id)).fetchone()
        return row is not None

    def postpone(self, job_id: int, delay: float) -> None:
        """Back to the queue for delay seconds, not counted as a failed attempt."""
        now = self.clock()
        self.conn.execute("UPDATE jobs SET status = 'queued', next_run_at = ?, updated_at = ? "
                          "WHERE id = ? AND status = 'running'", (now + delay, now, job_id))

    def get(self, job_id: int) -> Job:
        return _job(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def _recover(self) -> tuple[int, list[Job]]:
        now = self.clock()
        with transaction(self.conn):
            n = self.conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = attempts + 1, updated_at = ? WHERE status = 'running'",
                (now,)).rowcount
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND attempts > ? ORDER BY id", (len(RETRY_DELAYS),))]
            if ids:
                self.conn.execute(
                    f"UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? "
                    f"WHERE id IN ({','.join('?' * len(ids))})", (t("jobs.crash_loop"), now, *ids))
        return n, [self.get(i) for i in ids]

    def recover(self) -> int:
        """Jobs left 'running' by a crash go back to the queue; repeated crashes fail the job.

        Returns how many were re-queued; recover_failed() does the same and returns the jobs it failed."""
        return self._recover()[0]

    def recover_failed(self) -> list[Job]:
        """recover(), returning the jobs it failed because they crashed the program too often (to tell their users)."""
        return self._recover()[1]

    def claim_due(self, limit: int) -> list[Job]:
        if limit <= 0:
            return []
        claimed: list[int] = []
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND next_run_at <= ? ORDER BY id LIMIT ?",
                (self.clock(), limit),
            ).fetchall()
            for r in rows:
                cur = self.conn.execute(
                    "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                    (self.clock(), r["id"]),
                )
                if cur.rowcount == 1:
                    claimed.append(r["id"])
        return [self.get(job_id) for job_id in claimed]

    def complete(self, job_id: int, entry_id: str) -> None:
        self.conn.execute("UPDATE jobs SET status = 'done', entry_id = ?, updated_at = ? WHERE id = ?",
                          (entry_id, self.clock(), job_id))

    def retry_or_fail(self, job_id: int, error: str, *, retry: bool = True) -> bool:
        """Record a failure. Returns True if the job was rescheduled, False if it is now failed.

        retry=False fails the job at once (the error is permanent)."""
        job = self.get(job_id)
        if job.status != "running":  # e.g. already completed: a late exception must not undo that
            return False
        attempts = job.attempts + 1
        now = self.clock()
        if retry and attempts <= len(RETRY_DELAYS):
            cur = self.conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = ?, last_error = ?, next_run_at = ?, "
                "updated_at = ? WHERE id = ? AND status = 'running'",
                (attempts, error, now + RETRY_DELAYS[attempts - 1], now, job_id),
            )
            return cur.rowcount == 1
        self.conn.execute("UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? "
                          "WHERE id = ? AND status = 'running'", (error, now, job_id))
        return False

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, count(*) FROM jobs GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows if r[0] != "done"}

    def due_count(self) -> int:
        """Jobs running now or queued and ready to run (not waiting out a retry delay)."""
        return self.conn.execute(
            "SELECT count(*) FROM jobs WHERE status = 'running' OR (status = 'queued' AND next_run_at <= ?)",
            (self.clock(),),
        ).fetchone()[0]

    def recent_failures(self, n: int = 5) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = 'failed' ORDER BY updated_at DESC LIMIT ?", (n,)
        ).fetchall()
        return [_job(r) for r in rows]
