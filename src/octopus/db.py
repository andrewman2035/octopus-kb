"""SQLite connection: runtime state + rebuildable index in one file."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlite_vec

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, user_id TEXT NOT NULL,
  role TEXT NOT NULL, text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS conversations_user ON conversations(channel, user_id, id);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
  channel TEXT NOT NULL, user_id TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_run_at REAL NOT NULL,
  last_error TEXT, entry_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, next_run_at);
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL, url TEXT,
  source TEXT, saved_at TEXT NOT NULL, status TEXT NOT NULL, tags TEXT NOT NULL,
  has_vectors INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS entries_url ON entries(url);
-- an entry's other links (P36 fix round 1): its url_aliases and their platform keys (urls.same_page), so the phone's
-- share link and the browser tab's link find the same entry
CREATE TABLE IF NOT EXISTS url_aliases(url TEXT NOT NULL, entry_id TEXT NOT NULL, PRIMARY KEY(url, entry_id));
CREATE INDEX IF NOT EXISTS url_aliases_entry ON url_aliases(entry_id);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, entry_id TEXT NOT NULL, ord INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_entry ON chunks(entry_id);
CREATE TABLE IF NOT EXISTS pending_manual(
  channel TEXT NOT NULL, user_id TEXT NOT NULL, entry_id TEXT NOT NULL,
  prompted_at REAL NOT NULL, prompt_message_id TEXT, origin TEXT, text_added INTEGER,
  PRIMARY KEY(channel, user_id));
-- every "send screenshots" prompt, so a reply to an older prompt still finds its entry (7-day TTL)
CREATE TABLE IF NOT EXISTS prompts(
  channel TEXT NOT NULL, user_id TEXT NOT NULL, prompt_message_id TEXT NOT NULL, entry_id TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY(channel, user_id, prompt_message_id));
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, user_id TEXT NOT NULL,
  text TEXT NOT NULL, created_at REAL NOT NULL, op_id INTEGER);
-- AUTOINCREMENT: ids are never reused, so a stale button cannot hit a newer action
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, entry_id TEXT, created_at REAL NOT NULL, title TEXT);
CREATE TABLE IF NOT EXISTS focus(
  channel TEXT NOT NULL, user_id TEXT NOT NULL, entry_id TEXT NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(channel, user_id));
-- record changes waiting for the user's confirmation (spec §2.5); AUTOINCREMENT so a stale button never hits a newer op
CREATE TABLE IF NOT EXISTS ops(
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
  entry_ids TEXT NOT NULL, arg TEXT NOT NULL, created_at REAL NOT NULL, text TEXT);
-- saves sent by the Chrome extension (P17), what its popup lists: the job saving it, or the entry it found already
-- saved, or why it failed at once
CREATE TABLE IF NOT EXISTS extension_saves(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, url TEXT, title TEXT NOT NULL,
  telegram INTEGER NOT NULL, job_id INTEGER, entry_id TEXT, error TEXT, created_at REAL NOT NULL, filled TEXT);
-- mails the email channel looked at (P23): the UID high-water mark per UIDVALIDITY, and for a mail taken who to answer,
-- the jobs saving it (done: the parts of it queued, so a save cut off by a stop is finished, not repeated) and the
-- replies waiting for them (one reply per mail)
CREATE TABLE IF NOT EXISTS email_saves(
  id INTEGER PRIMARY KEY AUTOINCREMENT, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL, sender TEXT,
  subject TEXT NOT NULL, message_id TEXT, jobs TEXT NOT NULL DEFAULT '[]', replies TEXT NOT NULL DEFAULT '[]',
  done TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(uidvalidity, uid));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(tokens);
-- ids deleted through Octopus (P18): a file still carrying one (a user's copy) is never taken for the entry again
CREATE TABLE IF NOT EXISTS deleted_ids(id TEXT PRIMARY KEY, deleted_at REAL NOT NULL);
"""


def connect(path: Path | str, embed_dim: int) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    added = "has_vectors" not in cols
    if added:
        conn.execute("ALTER TABLE entries ADD COLUMN has_vectors INTEGER NOT NULL DEFAULT 1")
    # columns added after the table first shipped (nullable: old rows simply lack the value)
    _add_column(conn, "actions", "title", "TEXT")  # a note's title when saved: "Make it a question" only if unchanged
    _add_column(conn, "ops", "text", "TEXT")  # the confirmation as asked, to ask it again
    _add_column(conn, "outbox", "op_id", "INTEGER")  # a held confirmation's op: never delivered once stale
    _add_column(conn, "pending_manual", "origin", "TEXT")  # what opened the window; old rows: a link
    _add_column(conn, "pending_manual", "text_added", "INTEGER")  # an /add or /new window took its one text
    _add_column(conn, "email_saves", "done", "TEXT NOT NULL DEFAULT '[]'")  # P23 fix round 1: parts of a save queued
    _add_column(conn, "extension_saves", "filled", "TEXT")  # P36: the entry the page filled (its id kept)
    reset = _ensure_vec_table(conn, embed_dim)
    if reset:  # every vector was dropped: backfill re-embeds everything
        conn.execute("UPDATE entries SET has_vectors = 0")
    elif added:  # entries saved before the column existed may lack vectors (embedder was down)
        conn.execute("UPDATE entries SET has_vectors = 0 WHERE id NOT IN "
                     "(SELECT DISTINCT c.entry_id FROM chunks c JOIN chunks_vec v ON v.rowid = c.id)")
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _ensure_vec_table(conn: sqlite3.Connection, embed_dim: int) -> bool:
    """Returns True when an existing vector index was dropped because the dimension changed."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone()
    reset = row is not None and int(row[0]) != embed_dim
    if reset:
        conn.execute("DROP TABLE IF EXISTS chunks_vec")
        logger.warning(
            "embedding dimension changed from %s to %d: vector index reset; run `octopus reindex`",
            row[0], embed_dim,
        )
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[{embed_dim}])")
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('embed_dim', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(embed_dim),),
    )
    return reset


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
