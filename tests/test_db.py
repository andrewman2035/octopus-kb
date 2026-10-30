import logging
import sqlite3

import pytest
import sqlite_vec

from octopus.conversations import Conversations
from octopus.db import connect, transaction
from octopus.settings import Settings


def test_schema_and_vec_extension(tmp_path):
    conn = connect(tmp_path / "x.db", embed_dim=8)
    assert conn.execute("select vec_version()").fetchone()[0].startswith("v")
    tables = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert {"settings", "conversations", "jobs", "entries", "chunks", "chunks_fts", "chunks_vec", "meta"} <= tables
    connect(tmp_path / "x.db", embed_dim=8)  # idempotent


def test_busy_timeout_is_set(tmp_path):
    conn = connect(tmp_path / "x.db", embed_dim=8)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_transaction_rolls_back(tmp_path):
    conn = connect(tmp_path / "x.db", embed_dim=8)
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("insert into settings(key, value) values ('a', '1')")
            raise RuntimeError("boom")
    assert conn.execute("select count(*) from settings").fetchone()[0] == 0
    assert not conn.in_transaction


def test_transaction_commits_on_success(tmp_path):
    conn = connect(tmp_path / "x.db", embed_dim=8)
    with transaction(conn):
        conn.execute("insert into settings(key, value) values ('a', '1')")
    assert not conn.in_transaction
    assert conn.execute("select value from settings where key = 'a'").fetchone()[0] == "1"


def test_embed_dim_change_resets_vec_index(tmp_path, caplog):
    path = tmp_path / "x.db"
    conn = connect(path, embed_dim=8)
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (1, ?)",
                 (sqlite_vec.serialize_float32([0.1] * 8),))
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1

    with caplog.at_level(logging.WARNING, logger="octopus.db"):
        conn = connect(path, embed_dim=16)
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 0
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (1, ?)",
                 (sqlite_vec.serialize_float32([0.1] * 16),))
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone()[0] == "16"
    assert "octopus reindex" in caplog.text


def test_same_embed_dim_keeps_vec_index(tmp_path, caplog):
    path = tmp_path / "x.db"
    conn = connect(path, embed_dim=8)
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (1, ?)",
                 (sqlite_vec.serialize_float32([0.1] * 8),))
    with caplog.at_level(logging.WARNING, logger="octopus.db"):
        conn = connect(path, embed_dim=8)
    assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] == 1
    assert "octopus reindex" not in caplog.text


def test_settings(tmp_path):
    s = Settings(connect(tmp_path / "x.db", embed_dim=8))
    assert s.get("k") is None
    assert s.get("k", "d") == "d"
    s.set("k", "v")
    s.set("k", "w")
    assert s.get("k") == "w"
    s.delete("k")
    assert s.get("k") is None


def test_conversations_recent(tmp_path):
    t = iter(range(100))
    c = Conversations(connect(tmp_path / "x.db", embed_dim=8), clock=lambda: next(t))
    c.add("cli", "u", "user", "q1")
    c.add("cli", "u", "assistant", "a1")
    c.add("cli", "other", "user", "x")
    c.add("cli", "u", "user", "q2")
    c.add("cli", "u", "assistant", "a2")
    assert c.recent("cli", "u", turns=1) == [("user", "q2"), ("assistant", "a2")]
    assert c.recent("cli", "u", turns=3) == [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]


def test_transaction_takes_write_lock_immediately(tmp_path):
    c1 = connect(tmp_path / "x.db", embed_dim=8)
    c2 = connect(tmp_path / "x.db", embed_dim=8)
    c2.execute("PRAGMA busy_timeout=0")
    with transaction(c1):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            c2.execute("insert into settings(key, value) values ('b', '2')")


def test_old_actions_and_ops_tables_get_their_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE actions(id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, user_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL, entry_id TEXT, created_at REAL NOT NULL);"
        "INSERT INTO actions(channel, user_id, kind, text, created_at) VALUES ('tg', 'me', 'note', 'x', 1);"
        "CREATE TABLE ops(id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, user_id TEXT NOT NULL,"
        " kind TEXT NOT NULL, entry_ids TEXT NOT NULL, arg TEXT NOT NULL, created_at REAL NOT NULL);")
    old.close()
    conn = connect(path, embed_dim=8)
    assert conn.execute("SELECT title FROM actions").fetchone()[0] is None  # old rows: title unknown
    assert "text" in {r[1] for r in conn.execute("PRAGMA table_info(ops)")}
    connect(path, embed_dim=8)  # idempotent


def test_old_outbox_table_gets_op_id(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE outbox(id INTEGER PRIMARY KEY, channel TEXT NOT NULL, user_id TEXT NOT NULL,"
        " text TEXT NOT NULL, created_at REAL NOT NULL);"
        "INSERT INTO outbox(channel, user_id, text, created_at) VALUES ('clawbot', 'u', '旧回复', 1);")
    old.close()
    conn = connect(path, embed_dim=8)
    assert tuple(conn.execute("SELECT text, op_id FROM outbox").fetchone()) == ("旧回复", None)  # an ordinary reply
    connect(path, embed_dim=8)  # idempotent


def test_old_pending_manual_table_gets_origin(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE pending_manual(channel TEXT NOT NULL, user_id TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " prompted_at REAL NOT NULL, prompt_message_id TEXT, PRIMARY KEY(channel, user_id));"
        "INSERT INTO pending_manual VALUES ('tg', 'me', '20261013-aaaaaa', 1, 'm1');")
    old.close()
    conn = connect(path, embed_dim=8)
    assert tuple(conn.execute("SELECT entry_id, origin FROM pending_manual").fetchone()) == ("20261013-aaaaaa", None)
    connect(path, embed_dim=8)  # idempotent


def test_old_pending_manual_table_gets_text_added(tmp_path):
    from octopus.pending import PendingManual

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE pending_manual(channel TEXT NOT NULL, user_id TEXT NOT NULL, entry_id TEXT NOT NULL,"
        " prompted_at REAL NOT NULL, prompt_message_id TEXT, origin TEXT, PRIMARY KEY(channel, user_id));"
        "INSERT INTO pending_manual VALUES ('tg', 'me', '20261013-aaaaaa', 1000, NULL, 'add');")
    old.close()
    conn = connect(path, embed_dim=8)
    p = PendingManual(conn, clock=lambda: 1000.0)
    assert p.origin("tg", "me") == "add" and not p.text_added("tg", "me")  # NULL: no text taken yet
    p.mark_text_added("tg", "me")
    conn.close()
    conn = connect(path, embed_dim=8)  # idempotent
    assert PendingManual(conn, clock=lambda: 1000.0).text_added("tg", "me")
