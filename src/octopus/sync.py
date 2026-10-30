"""Edits made outside Octopus (in Obsidian): the library folder is scanned and the index follows it (P18).

Every pass walks the folder in a thread and decides each id's canonical file (Library.adopt: a user's copy with the
same id is never it). A canonical file whose (mtime, size) differs from what this scanner last handled and from what
Octopus last wrote is read again and compared with the index; re-indexed only when its text differs. An indexed
entry whose file is gone on two passes at least CONFIRM_DELETE_SECONDS apart is dropped from the index (its files
stay where they are), unless many vanish at once. Nothing happens while the folder lacks its marker file."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from octopus.i18n import aliases, t
from octopus.library import MARKER, READ_ERRORS, Library, Sig, file_sig, read_marker, write_marker
from octopus.pipeline import SavePipeline

log = logging.getLogger(__name__)
SCAN_INTERVAL_SECONDS = 60
SETTLE_SECONDS = 5  # a file saved more recently than this may still be being edited: picked up next pass
CONFIRM_DELETE_SECONDS = 60  # gone on two passes this far apart before its entry leaves the index
MASS_MAX, MASS_SHARE = 5, 0.10  # more gone at once than this (or this share, from 2 on): drop none, tell the owner
YIELD_EVERY = 50
IDENTITY_KEY = "library_id"
MARK_SHARE = 0.9  # an unmarked folder holding this share of the indexed notes where the index says is the library
__getattr__ = aliases({"LIBRARY_GONE": "sync.library_gone", "LIBRARY_UNMARKED": "sync.library_unmarked",
                       "LIBRARY_STRANGE": "sync.library_strange", "LIBRARY_NO_MARKER": "sync.library_no_marker",
                       "SEVERAL_FILES": "sync.several_files", "MASS_VANISH": "sync.mass_vanish"})


def library_identity(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (IDENTITY_KEY,)).fetchone()
    return row[0] if row else None


def remember_identity(conn: sqlite3.Connection, identity: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (IDENTITY_KEY, identity))


def claim_library(root: Path, conn: sqlite3.Connection) -> bool:
    """Mark root as this database's library folder (init, library move, first setup). False when root already
    carries another library's marker (left as it is)."""
    identity, mark = library_identity(conn), read_marker(root)
    if mark is not None and identity is not None and mark != identity:
        return False
    identity = identity or mark or uuid.uuid4().hex
    if mark is None:
        write_marker(root, identity)
    remember_identity(conn, identity)
    return True


def marker_evidence(root: Path, stored: dict[str, str]) -> bool:
    """Blocking (reads files): at least MARK_SHARE of the indexed entries (and at least one) are files under root,
    where the index says, carrying their id. A renamed or recreated empty folder has none of them."""
    base = root.resolve()
    hits = 0
    for entry_id, stored_path in stored.items():
        path = Path(stored_path)
        try:
            if path.resolve().is_relative_to(base) and Library.read_id(path) == entry_id:
                hits += 1
        except (OSError, UnicodeDecodeError):
            continue
    return hits >= 1 and hits >= MARK_SHARE * len(stored)


def library_ok(root: Path, conn: sqlite3.Connection, entries: int, mark: str | None, evidence: bool = False) -> bool:
    """The folder is this library: its marker matches. Without one it is marked now only on first setup (no
    entries) or with evidence (marker_evidence: a library from before P18); otherwise it is not trusted (renamed or
    moved away, an empty one recreated)."""
    identity = library_identity(conn)
    if mark is None:
        if entries and evidence and identity is None and claim_library(root, conn):  # only on the upgrade to P18
            log.warning("library folder %s marked as this library's: its notes are where the index says", root)
            return True
        return entries == 0 and claim_library(root, conn)
    if identity is None:
        remember_identity(conn, mark)
        return True
    return mark == identity


@dataclass
class ScanReport:
    updated: list[str] = field(default_factory=list)  # re-indexed: text, title or tags changed (or new)
    moved: list[str] = field(default_factory=list)  # only the file's place changed
    deleted: list[str] = field(default_factory=list)  # dropped from the index (the file was gone twice)
    skipped: bool = False  # the folder is not trusted (no marker): nothing was looked at


class LibrarySync:
    def __init__(self, pipeline: SavePipeline, clock: Callable[[], float] = time.time,
                 notify: Callable[[str], Awaitable[object]] | None = None):
        self.pipeline = pipeline
        self.library = pipeline.library
        self.index = pipeline.index
        self.clock = clock
        self.notify = notify  # one notice to the owner per problem (the folder is gone, many notes vanished)
        self._seen: dict[Path, Sig] = {}  # each entry file as this scanner last handled it
        self._warned: set[tuple[Path, Sig]] = set()
        self._missing: dict[str, float] = {}  # id -> when its file was first found gone
        self._told: set[str] = set()
        self._buried_seen: set[tuple[Path, Sig]] = set()

    async def _tell(self, key: str, text: str) -> None:
        if key in self._told:
            return
        self._told.add(key)
        log.warning("%s", text)
        if self.notify is not None:
            try:
                await self.notify(text)
            except Exception:
                log.exception("telling the owner about the library failed")

    async def _trusted(self) -> str | None:
        """None when the folder is this library's; else the notice saying why not."""
        root = self.library.root
        mark = await asyncio.to_thread(read_marker, root)
        entries = self.index.count()
        evidence = (mark is None and entries > 0 and library_identity(self.index.conn) is None
                    and await asyncio.to_thread(marker_evidence, root, self.index.paths()))
        if library_ok(root, self.index.conn, entries, mark, evidence):
            return None
        if not await asyncio.to_thread(root.is_dir):
            return t("sync.library_gone", path=root)
        if await asyncio.to_thread((root / MARKER).exists):
            return t("sync.library_unmarked", path=root)
        if entries and (evidence or await asyncio.to_thread(marker_evidence, root, self.index.paths())):
            return t("sync.library_no_marker", path=root)  # the notes are here: only the marker was deleted
        return t("sync.library_strange", path=root)

    async def _walk(self) -> tuple[dict[str, Path], dict[str, str], set[Path], dict]:
        files, unreadable = await asyncio.to_thread(self.library.snapshot, dict(self.library._files))
        stored, buried = self.index.paths(), self.index.buried()
        for path, (sig, fid) in files.items():
            if fid in buried and (path, sig) not in self._buried_seen:
                self._buried_seen.add((path, sig))
                log.warning("%s carries the id %s of an entry deleted through Octopus: ignored (octopus reindex "
                            "brings it back once the deleted note is no longer in the trash)", path, fid)
        ids = self.library.adopt(files, unreadable, stored, buried)
        for entry_id in sorted(self.library.ambiguous):
            names = t("common.list_sep").join(p.name for p, (_, fid) in sorted(files.items()) if fid == entry_id)
            await self._tell(f"several:{entry_id}", t("sync.several_files", id=entry_id, names=names))
        return ids, stored, unreadable, files

    async def scan(self, settle: float = SETTLE_SECONDS) -> ScanReport:
        report = ScanReport()
        if (why := await self._trusted()) is not None:
            report.skipped = True
            await self._tell("gone", why)
            return report
        self._told.discard("gone")
        ids, stored, unreadable, files = await self._walk()
        now = self.clock()
        seen: dict[Path, Sig] = {}
        for n, (entry_id, path) in enumerate(sorted(ids.items())):
            if n % YIELD_EVERY == YIELD_EVERY - 1:
                await asyncio.sleep(0)
            if path not in files:  # unreadable now, or written since the walk: next pass
                if path in self._seen:
                    seen[path] = self._seen[path]
                continue
            sig = files[path][0]
            if self._seen.get(path) == sig or self.library.wrote(path, sig):
                seen[path] = sig
            elif now - sig[0] / 1e9 >= settle and await self._update(entry_id, path, sig, report):
                seen[path] = sig
        self._seen = seen
        await self._drop_deleted(ids, stored, unreadable, now, report)
        return report

    async def _update(self, entry_id: str, path: Path, sig: Sig, report: ScanReport) -> bool:
        """True when this version of the file is handled (indexed, or unreadable and warned about)."""
        async with self.pipeline.entry_lock(entry_id):  # never between a merge's read and its write
            if file_sig(path) != sig:
                return False  # changed again just now: next pass
            try:
                entry = await asyncio.to_thread(self.library.load, path)
            except READ_ERRORS as e:
                if (path, sig) not in self._warned:
                    self._warned.add((path, sig))
                    log.warning("cannot read %s (edited outside Octopus?): keeping what was indexed before: %s",
                                path, e)
                return True
            except (OSError, UnicodeDecodeError):
                return False  # try again next pass
            if entry.id != entry_id:
                return True
            try:
                kind = self.index.differs(entry, path)
                if kind == "path":
                    self.index.set_path(entry_id, path)
                    report.moved.append(entry_id)
                elif kind == "content":
                    await self.index.upsert(entry, path)
                    report.updated.append(entry_id)
                    log.info("picked up an edit made outside Octopus: %s (%s)", entry_id, path)
            except Exception:
                log.exception("indexing %s after an outside edit failed", entry_id)
                return False
            return True

    async def held_deletes(self) -> list[str] | None:
        """Indexed entries whose file is gone (no file carries the id), oldest first, regardless of the two-pass
        wait and the mass guard: for `octopus library sync`. None when the folder is not trusted. A file that cannot be
        read now never holds up the others (see _gone)."""
        if await self._trusted() is not None:
            return None
        ids, stored, _, _ = await self._walk()
        return sorted(eid for eid in stored if self._gone(eid, stored[eid], ids))

    def _gone(self, entry_id: str, stored_path: str, ids: dict[str, Path]) -> bool:
        """No canonical file (one that cannot be read now but is known, or named by the id, counts as present), not
        several, and nothing at the stored path (nor its iCloud placeholder)."""
        path = Path(stored_path)
        return (entry_id not in ids and entry_id not in self.library.ambiguous and not path.exists()
                and not path.with_name(f".{path.name}.icloud").exists())

    async def _drop_deleted(self, ids: dict[str, Path], stored: dict[str, str], unreadable: set[Path], now: float,
                            report: ScanReport) -> None:
        gone = []
        for entry_id, path in stored.items():
            if not self._gone(entry_id, path, ids):
                self._missing.pop(entry_id, None)
                continue
            if now - self._missing.setdefault(entry_id, now) >= CONFIRM_DELETE_SECONDS:
                gone.append(entry_id)
        self._missing = {k: v for k, v in self._missing.items() if k in stored}
        if not gone:
            self._told.discard("mass")
            return
        if len(gone) > MASS_MAX or (len(gone) > 1 and len(gone) > MASS_SHARE * len(stored)):
            await self._tell("mass", t("sync.mass_vanish", n=len(gone)))
            return
        for entry_id in gone:
            if await self.pipeline.forget(entry_id):
                self._missing.pop(entry_id, None)
                gone_path = Path(stored[entry_id])  # put back later (from Obsidian's trash): indexed again
                self._seen.pop(gone_path, None)
                self.library._forget(gone_path)
                log.info("entry %s was deleted outside Octopus (%s): dropped from the index", entry_id,
                         stored[entry_id])
                report.deleted.append(entry_id)
