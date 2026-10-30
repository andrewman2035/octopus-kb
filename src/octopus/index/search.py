"""Hybrid retrieval: FTS5 (bm25) + sqlite-vec, merged with reciprocal rank fusion."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

from octopus.db import transaction
from octopus.embed import Embedder, EmbedError
from octopus.index.chunker import entry_chunks
from octopus.index.tokenizer import to_fts_doc, to_fts_query
from octopus.library import Entry, Library
from octopus.urls import lookup_keys, same_page

log = logging.getLogger(__name__)
CANDIDATES = 20
RRF_K = 60


# the links of each platform same_page keys, by the key's start (reddit.com and redd.it, threads.net and .com,
# weibo.com and .cn)
_KEY_SITES = {"https://www.xiaohongshu.com/": "%xiaohongshu.com%", "https://www.reddit.com/": "%redd%",
              "https://www.threads.com/": "%threads.%", "weibo:": "%weibo.%"}


def _alias_rows(entry: Entry) -> set[str]:
    """What the url_aliases table holds for the entry: its other links and their platform keys. Its own link's key is
    not stored (find_by_url works it out), so an entry saved before P36 needs no new row."""
    return (set(entry.url_aliases) | {k for u in entry.url_aliases if (k := same_page(u))}) - {entry.url}


@dataclass
class Hit:
    chunk_id: int
    entry_id: str
    title: str
    url: str | None
    text: str
    score: float


class Index:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder):
        self.conn = conn
        self.embedder = embedder

    async def upsert(self, entry: Entry, path: Path) -> None:
        chunks = entry_chunks(entry)
        try:
            vectors = await self.embedder.embed([f"{entry.title}\n{c}" for c in chunks])
        except EmbedError as e:
            log.warning("indexing %s without vectors: %s", entry.id, e)
            vectors = None
        with transaction(self.conn):
            self._delete_rows(entry.id)
            self.conn.execute(
                "INSERT INTO entries(id, path, title, url, source, saved_at, status, tags, has_vectors) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry.id, str(path), entry.title, entry.url, entry.source,
                 entry.saved_at.isoformat(timespec="seconds"), entry.status,
                 json.dumps(entry.tags, ensure_ascii=False), 0 if vectors is None else 1),
            )
            for alias in _alias_rows(entry):
                self.conn.execute("INSERT OR IGNORE INTO url_aliases(url, entry_id) VALUES (?, ?)", (alias, entry.id))
            for ord_, text in enumerate(chunks):
                cid = self.conn.execute("INSERT INTO chunks(entry_id, ord, text) VALUES (?, ?, ?)",
                                        (entry.id, ord_, text)).lastrowid
                self.conn.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                                  (cid, to_fts_doc(f"{entry.title}\n{text}")))
                if vectors is not None:
                    self.conn.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                                      (cid, sqlite_vec.serialize_float32(vectors[ord_])))

    def _delete_rows(self, entry_id: str) -> None:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM chunks WHERE entry_id = ?", (entry_id,))]
        for cid in ids:
            self.conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (cid,))
            self.conn.execute("DELETE FROM chunks_vec WHERE rowid = ?", (cid,))
        self.conn.execute("DELETE FROM chunks WHERE entry_id = ?", (entry_id,))
        self.conn.execute("DELETE FROM url_aliases WHERE entry_id = ?", (entry_id,))
        self.conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

    def delete(self, entry_id: str) -> None:
        with transaction(self.conn):
            self._delete_rows(entry_id)

    def differs(self, entry: Entry, path: Path) -> str | None:
        """How the indexed entry differs from this version of its file: None (not at all), "path" (only moved),
        "content" (or not indexed yet)."""
        row = self.conn.execute("SELECT path, title, url, source, saved_at, status, tags FROM entries WHERE id = ?",
                                (entry.id,)).fetchone()
        if row is None:
            return "content"
        chunks = [r[0] for r in self.conn.execute("SELECT text FROM chunks WHERE entry_id = ? ORDER BY ord",
                                                  (entry.id,))]
        indexed = (row["title"], row["url"], row["source"], row["saved_at"], row["status"], json.loads(row["tags"]))
        now = (entry.title, entry.url, entry.source, entry.saved_at.isoformat(timespec="seconds"), entry.status,
               entry.tags)
        aliases = {r[0] for r in self.conn.execute("SELECT url FROM url_aliases WHERE entry_id = ?", (entry.id,))}
        if indexed != now or chunks != entry_chunks(entry) or aliases != _alias_rows(entry):
            return "content"
        return None if row["path"] == str(path) else "path"

    def set_path(self, entry_id: str, path: Path) -> None:
        self.conn.execute("UPDATE entries SET path = ? WHERE id = ?", (str(path), entry_id))

    def bury(self, entry_id: str) -> None:
        """Remember that the entry was deleted through Octopus (see db.deleted_ids)."""
        self.conn.execute("INSERT OR REPLACE INTO deleted_ids(id, deleted_at) VALUES (?, ?)", (entry_id, time.time()))

    def unbury(self, entry_id: str) -> None:
        self.conn.execute("DELETE FROM deleted_ids WHERE id = ?", (entry_id,))

    def is_buried(self, entry_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM deleted_ids WHERE id = ?", (entry_id,)).fetchone() is not None

    def buried(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT id FROM deleted_ids")}

    def path_of(self, entry_id: str) -> str | None:
        row = self.conn.execute("SELECT path FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return row[0] if row else None

    def paths(self) -> dict[str, str]:
        """id -> the file each indexed entry was read from."""
        return {r[0]: r[1] for r in self.conn.execute("SELECT id, path FROM entries")}

    def find_link(self, url: str) -> str | None:
        """The link save whose own link is exactly this one (no aliases, no platform key)."""
        row = self.conn.execute("SELECT id FROM entries WHERE url = ? AND source IS NOT 'manual'", (url,)).fetchone()
        return row[0] if row else None

    def find_by_url(self, url: str) -> str | None:
        """The link save of this link: its url, else one of its other links (P36 fix round 1: a redirect, the
        browser tab's; or the platform's key for the post, urls.same_page). A note or image from that
        page (the Chrome extension) keeps the link but is not the page."""
        if found := self.find_link(url):
            return found
        keys = lookup_keys(url)
        row = self.conn.execute(
            f"SELECT e.id FROM url_aliases a JOIN entries e ON e.id = a.entry_id WHERE a.url IN "
            f"({', '.join('?' * len(keys))}) AND e.source IS NOT 'manual' ORDER BY e.saved_at LIMIT 1", keys).fetchone()
        if row:
            return row[0]
        if (key := same_page(url)) is None:
            return None
        site = next(like for start, like in _KEY_SITES.items() if key.startswith(start))  # any form of its links
        for entry_id, saved in self.conn.execute(
                "SELECT id, url FROM entries WHERE url LIKE ? AND source IS NOT 'manual' ORDER BY saved_at", (site,)):
            if same_page(saved) == key:
                return entry_id
        return None

    def recent(self, n: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, title, saved_at, status FROM entries ORDER BY julianday(saved_at) DESC, id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    def titles(self) -> list[tuple[str, str]]:
        """(id, title) of every entry: what a question may name by title."""
        return [(r[0], r[1]) for r in self.conn.execute("SELECT id, title FROM entries")]

    def texts(self) -> list[dict]:
        """Every entry (id, title, status) with its indexed chunks in order, newest first: /find scans them."""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, title, saved_at, status FROM entries ORDER BY julianday(saved_at) DESC, id DESC")]
        chunks: dict[str, list[str]] = {}
        for entry_id, text in self.conn.execute("SELECT entry_id, text FROM chunks ORDER BY entry_id, ord"):
            chunks.setdefault(entry_id, []).append(text)
        for row in rows:
            row["chunks"] = chunks.get(row["id"], [])
        return rows

    def missing_vectors(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT id FROM entries WHERE has_vectors = 0 ORDER BY id")]

    def has_vectors(self, entry_id: str) -> bool:
        row = self.conn.execute("SELECT has_vectors FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return bool(row and row[0])

    def count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM entries").fetchone()[0]

    def _fts(self, query: str) -> list[int]:
        q = to_fts_query(query)
        if not q:
            return []
        try:
            rows = self.conn.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                (q, CANDIDATES),
            ).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("fts query failed: %s", e)
            return []
        return [r[0] for r in rows]

    async def _vec(self, query: str) -> list[int]:
        try:
            [qv] = await self.embedder.embed([query])
        except EmbedError as e:
            log.warning("vector search skipped: %s", e)
            return []
        rows = self.conn.execute(
            "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qv), CANDIDATES),
        ).fetchall()
        return [r[0] for r in rows]

    def saved_between(self, since: str | None, until: str | None, limit: int) -> list[dict]:
        """Entries saved on since..until (YYYY-MM-DD, both inclusive, either open), newest first."""
        rows = self.conn.execute(
            "SELECT id, title, saved_at, status FROM entries "
            "WHERE (? IS NULL OR substr(saved_at, 1, 10) >= ?) AND (? IS NULL OR substr(saved_at, 1, 10) <= ?) "
            "ORDER BY julianday(saved_at) DESC, id DESC LIMIT ?",
            (since, since, until, until, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    async def search(self, query: str, k: int = 8, keyword_only: bool = False) -> list[Hit]:
        """keyword_only: full-text matches only (vector search always returns the nearest chunks, even
        unrelated ones, so it cannot tell "exactly one entry" or "none" apart)."""
        scores: dict[int, float] = {}
        for ranked in (self._fts(query), [] if keyword_only else await self._vec(query)):
            for rank, cid in enumerate(ranked):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        hits = []
        for cid, score in sorted(scores.items(), key=lambda kv: -kv[1])[:k]:
            row = self.conn.execute(
                "SELECT c.id, c.entry_id, c.text, e.title, e.url FROM chunks c "
                "JOIN entries e ON e.id = c.entry_id WHERE c.id = ?", (cid,)
            ).fetchone()
            if row:
                hits.append(Hit(row["id"], row["entry_id"], row["title"], row["url"], row["text"], score))
        return hits

    async def rebuild(self, library: Library) -> int:
        """Index every entry file; drop rows of entries with no file. A note deleted through Octopus whose file is
        back (moved back from the trash by hand) is indexed again: an explicit reindex is the way to restore one."""
        buried = self.buried()
        seen: set[str] = set()
        for path, entry in library.iter_entries(revive=True):
            if entry.id in buried:
                self.unbury(entry.id)
                log.warning("%s was deleted through Octopus but its file is back (%s): indexed again", entry.id, path)
            await self.upsert(entry, path)
            seen.add(entry.id)
        # kept: a file that is there but broken or unreadable now, several files for one id, a stored file still there
        keep = (seen | set(library._ids) | library.ambiguous
                | {eid for eid, p in self.paths().items() if Path(p).exists()})
        with transaction(self.conn):
            for (entry_id,) in self.conn.execute("SELECT id FROM entries").fetchall():
                if entry_id not in keep:
                    self._delete_rows(entry_id)
        return len(seen)
