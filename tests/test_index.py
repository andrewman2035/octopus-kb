from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fakes import FakeEmbedder

from octopus.db import connect
from octopus.index import Index
from octopus.index.chunker import chunk_text, entry_chunks
from octopus.index.tokenizer import to_fts_query
from octopus.library import Entry, Library

CST = timezone(timedelta(hours=8))


def entry(id, title, body, url=None, day=13):
    return Entry(id=id, title=title, source="web", saved_at=datetime(2026, 10, day, 9, tzinfo=CST),
                 channel="cli", url=url, summary="", body=body)


E1 = entry("20261013-aaaaaa", "复利的力量", "长期主义的投资者相信复利。坚持定投十年。", url="https://a.com/1")
E2 = entry("20261013-bbbbbb", "播客转写", "这期播客讲录音设备和转写工具。", url="https://b.com/2", day=14)


def make_index(tmp_path):
    emb = FakeEmbedder()
    return Index(connect(tmp_path / "i.db", embed_dim=emb.dim), emb), emb


async def test_upsert_and_search(tmp_path):
    idx, _ = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    await idx.upsert(E2, Path("b.md"))
    hits = await idx.search("长期投资的复利", k=3)
    assert hits[0].entry_id == E1.id
    assert hits[0].title == "复利的力量"
    assert hits[0].url == "https://a.com/1"
    assert idx.count() == 2
    assert [r["id"] for r in idx.recent(10)] == [E2.id, E1.id]


async def test_upsert_replaces_chunks(tmp_path):
    idx, _ = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    await idx.upsert(E1, Path("a.md"))
    n = idx.conn.execute("select count(*) from chunks where entry_id = ?", (E1.id,)).fetchone()[0]
    assert n == len(entry_chunks(E1))
    assert idx.conn.execute("select count(*) from chunks_fts").fetchone()[0] == n
    assert idx.conn.execute("select count(*) from chunks_vec").fetchone()[0] == n


async def test_find_by_url_and_delete(tmp_path):
    idx, _ = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    assert idx.find_by_url("https://a.com/1") == E1.id
    idx.delete(E1.id)
    assert idx.find_by_url("https://a.com/1") is None
    assert await idx.search("复利") == []
    for table in ("chunks", "chunks_fts", "chunks_vec"):
        assert idx.conn.execute(f"select count(*) from {table}").fetchone()[0] == 0


async def test_titles_lists_every_entry(tmp_path):
    idx, _ = make_index(tmp_path)
    assert idx.titles() == []
    await idx.upsert(E1, Path("a.md"))
    await idx.upsert(E2, Path("b.md"))
    assert sorted(idx.titles()) == [(E1.id, "复利的力量"), (E2.id, "播客转写")]


async def test_search_uses_fts_when_embedder_down(tmp_path):
    idx, emb = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    emb.fail = True
    hits = await idx.search("复利")
    assert [h.entry_id for h in hits] == [E1.id]


async def test_upsert_without_vectors_when_embedder_down(tmp_path):
    idx, emb = make_index(tmp_path)
    emb.fail = True
    await idx.upsert(E1, Path("a.md"))
    assert idx.conn.execute("select count(*) from chunks_vec").fetchone()[0] == 0
    assert [h.entry_id for h in await idx.search("复利")] == [E1.id]


async def test_rebuild_from_library(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    lib.save(E1)
    lib.save(E2)
    idx, _ = make_index(tmp_path)
    await idx.upsert(entry("20261001-cccccc", "已删除", "旧内容"), Path("gone.md"))
    assert await idx.rebuild(lib) == 2
    assert idx.count() == 2
    assert (await idx.search("播客"))[0].entry_id == E2.id


async def test_rebuild_failure_keeps_existing_index(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    lib.save(E1)
    lib.save(E2)
    idx, emb = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    await idx.upsert(E2, Path("b.md"))

    class Flaky(FakeEmbedder):
        async def embed(self, texts):
            if self.calls >= 1:
                raise RuntimeError("boom")
            return await super().embed(texts)

    idx.embedder = Flaky()
    with pytest.raises(RuntimeError):
        await idx.rebuild(lib)
    idx.embedder = emb
    assert idx.count() == 2
    assert (await idx.search("复利"))[0].entry_id == E1.id
    assert (await idx.search("播客"))[0].entry_id == E2.id


async def test_search_uses_vectors_when_fts_empty(tmp_path, monkeypatch):
    idx, _ = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    monkeypatch.setattr(idx, "_fts", lambda q: [])
    hits = await idx.search("长期投资的复利")
    assert [h.entry_id for h in hits] == [E1.id]


def test_chunk_text_groups_paragraphs():
    paras = ["段落" * 100] * 4  # 200 chars each
    chunks = chunk_text("\n\n".join(paras))
    assert len(chunks) == 2
    assert all(len(c) <= 500 for c in chunks)


def test_chunk_text_splits_long_unpunctuated():
    chunks = chunk_text("字" * 2000)
    assert len(chunks) >= 3
    assert all(len(c) <= 800 for c in chunks)
    assert "".join(chunks) == "字" * 2000


def test_entry_chunks_includes_summary_first():
    e = entry("20261013-dddddd", "标题", "正文内容")
    e.summary, e.points = "摘要", ["点一"]
    assert entry_chunks(e)[0] == "标题\n摘要\n点一"
    assert entry_chunks(entry("20261013-eeeeee", "只有标题", "")) == ["只有标题"]


def test_entry_chunks_includes_notes():
    e = entry("20261013-ffffff", "标题", "正文内容")
    e.notes = "我的想法"
    assert entry_chunks(e)[-1] == "我的备注：我的想法"
    e.notes = ""
    assert all("我的备注" not in c for c in entry_chunks(e))


def test_fts_query():
    assert to_fts_query("的 吗 ？") is None
    q = to_fts_query("长期投资的复利")
    assert '"复利"' in q and '"的"' not in q


async def test_recent_orders_by_instant_across_utc_offsets(tmp_path):
    idx, _ = make_index(tmp_path)
    us = timezone(timedelta(hours=-5))
    earlier = Entry(id="20261013-aaaaaa", title="a", source="web", channel="cli", body="x",
                    saved_at=datetime(2026, 10, 13, 9, 0, tzinfo=CST))  # 01:00Z
    later = Entry(id="20261013-bbbbbb", title="b", source="web", channel="cli", body="x",
                  saved_at=datetime(2026, 10, 13, 0, 30, tzinfo=us))  # 05:30Z, but "smaller" as a string
    await idx.upsert(earlier, Path("a.md"))
    await idx.upsert(later, Path("b.md"))
    assert [r["id"] for r in idx.recent(10)] == [later.id, earlier.id]


async def test_missing_vectors_and_has_vectors_flag(tmp_path):
    idx, emb = make_index(tmp_path)
    emb.fail = True
    await idx.upsert(E1, Path("a.md"))
    assert idx.missing_vectors() == [E1.id] and idx.has_vectors(E1.id) is False
    emb.fail = False
    await idx.upsert(E1, Path("a.md"))
    assert idx.missing_vectors() == [] and idx.has_vectors(E1.id) is True
    assert idx.has_vectors("20261013-zzzzzz") is False
    assert idx.conn.execute("select count(*) from chunks_vec").fetchone()[0] > 0


def _old_db(path):
    import sqlite3

    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL, url TEXT, "
                "source TEXT, saved_at TEXT NOT NULL, status TEXT NOT NULL, tags TEXT NOT NULL)")
    raw.execute("CREATE TABLE chunks(id INTEGER PRIMARY KEY, entry_id TEXT NOT NULL, ord INTEGER NOT NULL, "
                "text TEXT NOT NULL)")
    for eid in ("20261013-aaaaaa", "20261013-bbbbbb"):
        raw.execute("INSERT INTO entries VALUES (?, 'x.md', 't', NULL, 'web', '2026-10-13T09:00:00', 'ok', '[]')", (eid,))
        raw.execute("INSERT INTO chunks(entry_id, ord, text) VALUES (?, 0, 'hello')", (eid,))
    raw.commit()
    return raw


def test_has_vectors_column_added_to_old_db(tmp_path):
    db = tmp_path / "old.db"
    raw = _old_db(db)
    raw.close()
    conn = connect(db, embed_dim=16)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)")]
    assert "has_vectors" in cols
    # neither entry had a vector: both must be flagged for backfill
    assert [r[0] for r in conn.execute("SELECT id FROM entries WHERE has_vectors = 0 ORDER BY id")] == [
        "20261013-aaaaaa", "20261013-bbbbbb"]


def test_migration_keeps_entries_that_already_have_vectors(tmp_path):
    import sqlite_vec

    db = tmp_path / "old.db"
    raw = _old_db(db)
    raw.close()
    conn = connect(db, embed_dim=16)
    conn.execute("UPDATE entries SET has_vectors = 1")
    conn.execute("INSERT INTO chunks_vec(rowid, embedding) SELECT id, ? FROM chunks WHERE entry_id = '20261013-aaaaaa'",
                 (sqlite_vec.serialize_float32([0.1] * 16),))
    conn.close()
    # re-opening an already migrated db must not touch the flags
    conn = connect(db, embed_dim=16)
    assert conn.execute("SELECT count(*) FROM entries WHERE has_vectors = 1").fetchone()[0] == 2


def test_migration_flags_only_vectorless_entries(tmp_path):
    import sqlite3

    import sqlite_vec

    db = tmp_path / "old.db"
    raw = _old_db(db)
    raw.enable_load_extension(True)
    sqlite_vec.load(raw)
    raw.execute("CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[16])")
    raw.execute("INSERT INTO chunks_vec(rowid, embedding) SELECT id, ? FROM chunks WHERE entry_id = '20261013-aaaaaa'",
                (sqlite_vec.serialize_float32([0.1] * 16),))
    raw.commit()
    raw.close()
    conn = connect(db, embed_dim=16)
    flags = {r[0]: r[1] for r in conn.execute("SELECT id, has_vectors FROM entries")}
    assert flags == {"20261013-aaaaaa": 1, "20261013-bbbbbb": 0}


def test_dimension_change_flags_all_entries_for_backfill(tmp_path):

    db = tmp_path / "d.db"
    conn = connect(db, embed_dim=16)
    conn.execute("INSERT INTO entries(id, path, title, url, source, saved_at, status, tags) "
                 "VALUES ('20261013-aaaaaa', 'x.md', 't', NULL, 'web', '2026-10-13T09:00:00', 'ok', '[]')")
    assert conn.execute("SELECT has_vectors FROM entries").fetchone()[0] == 1
    conn.close()
    conn = connect(db, embed_dim=8)
    assert conn.execute("SELECT has_vectors FROM entries").fetchone()[0] == 0


async def test_saved_between_is_inclusive_by_date_newest_first(tmp_path):
    idx, _ = make_index(tmp_path)
    e3 = entry("20261015-cccccc", "第三条", "正文", day=15)
    for e, p in ((E1, "a.md"), (E2, "b.md"), (e3, "c.md")):
        await idx.upsert(e, Path(p))
    ids = lambda rows: [r["id"] for r in rows]
    assert ids(idx.saved_between("2026-10-13", "2026-10-14", 10)) == [E2.id, E1.id]
    assert ids(idx.saved_between("2026-10-14", None, 10)) == [e3.id, E2.id]
    assert ids(idx.saved_between(None, "2026-10-13", 10)) == [E1.id]
    assert ids(idx.saved_between(None, None, 2)) == [e3.id, E2.id]
    assert set(idx.saved_between("2026-10-13", "2026-10-13", 1)[0]) == {"id", "title", "saved_at", "status"}


async def test_keyword_only_search_skips_vectors(tmp_path):
    idx, _ = make_index(tmp_path)
    await idx.upsert(E1, Path("a.md"))
    await idx.upsert(E2, Path("b.md"))
    assert {h.entry_id for h in await idx.search("复利", 8)} == {E1.id, E2.id}  # vectors always find neighbours
    assert [h.entry_id for h in await idx.search("复利", 8, keyword_only=True)] == [E1.id]
    assert await idx.search("天气", 8, keyword_only=True) == []
