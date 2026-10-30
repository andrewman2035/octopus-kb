"""Markdown files under ~/Octopus/library — the source of truth."""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import sqlite3
import uuid
import copy
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import yaml

from octopus import i18n
from octopus.media import IMAGE_SUFFIXES

logger = logging.getLogger(__name__)

REQUIRED_KEYS = ("id", "saved_at")
FRONTMATTER_KEYS = ["id", "title", "source", "url", "media_url", "media_id", "author",
                    "saved_at", "channel", "language", "tags", "status", "url_aliases", "title_set", "fetched",
                    "fetched_title"]
# A note's headings come in two sets, Chinese and English (P25, the catalog's note.* keys). Both are read, forever: a
# note keeps the set it was written with; a new note gets the configured language's.
NOTE_LANGS = ("zh", "en")
ROLES = ("summary", "points", "body", "comments", "attachments", "attach_line", "attach_start", "attach_end")
HEADINGS = {lang: {role: i18n.CATALOGS[lang][f"note.{role}"] for role in ROLES} for lang in NOTE_LANGS}
SUMMARY_H, POINTS_H, BODY_H = (HEADINGS["zh"][r] for r in ("summary", "points", "body"))  # the Chinese set's names
NOTES_H = HEADINGS["zh"]["comments"]
ATTACH_H = HEADINGS["zh"]["attachments"]  # Obsidian links to the files the attachment lines name; rebuilt on save
# a body line naming an attached file, in either language
ATTACH_LINE_RE = re.compile(r"(?m)^(?:" + "|".join(re.escape(HEADINGS[lang]["attach_line"]) for lang in NOTE_LANGS)
                            + r")attachments/(.+)$")
# the links between these two Obsidian comments are generated: parse drops them whatever Obsidian made of them
ATTACH_START, ATTACH_END = HEADINGS["zh"]["attach_start"], HEADINGS["zh"]["attach_end"]
ATTACH_BLOCK = f"\n{ATTACH_H}\n\n{ATTACH_START}\n"
WIKILINK_UNSAFE = set("[]|#^")  # a name with these cannot be an Obsidian [[link]]: a Markdown link instead
# files Obsidian shows inline: ![[…]]; others get a plain [[link]]
EMBED_SUFFIXES = (*IMAGE_SUFFIXES, ".pdf", ".mp3", ".m4a", ".wav", ".ogg", ".flac", ".mp4", ".mov", ".webm")
MARKER = ".octopus-library"  # in the library folder: its identity; without it the folder is not ours (moved?)
FM_ID_RE = re.compile(r"^id:[ \t]*['\"]?(\d{8}-[0-9a-f]{6})['\"]?[ \t]*$", re.M)
# what parsing a hand-edited (broken) file can raise
READ_ERRORS = (ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError)


@dataclass
class Entry:
    id: str
    title: str
    source: str
    saved_at: datetime
    channel: str
    status: str = "ok"
    url: str | None = None
    media_url: str | None = None
    media_id: str | None = None
    author: str | None = None
    language: str | None = None
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    points: list[str] = field(default_factory=list)
    body: str = ""
    notes: str = ""
    # kept as the user wrote them in Obsidian: frontmatter keys we do not know (aliases, cssclasses…), text before
    # Summary (a "# title" there names the entry), text after the Key points list (their own words or ## sections)
    extra: dict = field(default_factory=dict)
    preamble: str = ""
    more: str = ""
    # the heading set of the file ("zh" / "en", P25), kept when it is written again; "": the configured language's
    heading_lang: str = field(default="", compare=False)
    # P36 fix round 1: the page's other links (redirects, canonical, the browser tab's), found by them too; the title
    # was set by the owner (/title): a fill keeps it; the sha256 of the body as fetched (a partial entry's): a fill
    # replaces only a body nobody has edited since; fetched_title: the sha256 of the title it was saved with (fix
    # round 2): a title changed since (in Obsidian too) is the owner's and a fill keeps it
    url_aliases: list[str] = field(default_factory=list)
    title_set: bool = False
    fetched: str | None = None
    fetched_title: str | None = None


def new_entry_id(now: datetime) -> str:
    return f"{now:%Y%m%d}-{secrets.token_hex(3)}"


def _slug(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#\[\]\s：？！，。、]+', "-", title).strip("-")  # zh: content
    return s[:30] or "untitled"


def note_filename(entry: Entry) -> str:
    """The note as a file sent in chat (/read <id> file): <title slug>-<the 6 characters after the "-">.md."""
    return f"{_slug(entry.title)}-{entry.id.rsplit('-', 1)[-1]}.md"


def _set(lang: str | None) -> dict[str, str]:
    lang = lang or i18n.get_language()
    return HEADINGS[lang if lang in HEADINGS else "zh"]


def has_links_block(text: str) -> bool:
    """The text holds the start of a generated links block, in either language."""
    return any(HEADINGS[lang]["attach_start"] in text for lang in NOTE_LANGS)


def attachment_links(body: str, lang: str | None = None) -> str:
    """The generated block of Obsidian links to the files the body's attachment lines name, in lang's words (None:
    the configured language)."""
    h = _set(lang)
    links = []
    for m in ATTACH_LINE_RE.finditer(body):
        rel = m.group(1).strip()
        if not rel or "<" in rel or ">" in rel:
            continue
        bang = "!" if Path(rel).suffix.lower() in EMBED_SUFFIXES else ""
        if WIKILINK_UNSAFE & set(rel):
            name = re.sub(r"[\[\]]", "", Path(rel).name)
            links.append(f"{bang}[{'' if bang else name}](<attachments/{rel}>)")
        else:
            links.append(f"{bang}[[attachments/{rel}]]")
    links = list(dict.fromkeys(links))
    block = f"\n{h['attachments']}\n\n{h['attach_start']}\n"
    return f"{block}" + "\n".join(links) + f"\n{h['attach_end']}\n" if links else ""


def _without_links(body: str) -> str:
    """The body without the generated links block (the last one, in either language), however Obsidian rewrote the
    links in it."""
    found = [(body.rfind(f"\n{h['attachments']}\n\n{h['attach_start']}"), h) for h in HEADINGS.values()]
    i, h = max(found, key=lambda f: f[0])
    if i < 0:
        return body
    j = body.find(h["attach_end"], i)
    if j < 0:  # its closing marker was deleted: not provably ours any more, kept as the user's text
        return body
    rest = body[j + len(h["attach_end"]):].strip("\n")
    return body[:i].rstrip("\n") + (f"\n\n{rest}" if rest else "")


def _title_line(preamble: str) -> str | None:
    """A "# heading" as the first line before the Summary heading is the note's title; one further down is just text."""
    first = preamble.strip().split("\n", 1)[0]
    m = re.fullmatch(r"# (.+)", first)
    return m.group(1).strip() if m else None


def render(entry: Entry) -> str:
    fm: dict = {}
    for key in FRONTMATTER_KEYS:
        value = getattr(entry, key)
        if key == "saved_at":
            value = value.isoformat(timespec="seconds")
        if key == "tags" or value not in (None, "", [], False):
            fm[key] = value
    for key, value in entry.extra.items():
        if key not in FRONTMATTER_KEYS:
            fm[key] = value
    head = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=None, width=10_000)
    preamble = entry.preamble.strip()
    if _title_line(preamble) is not None:
        preamble = f"# {entry.title}" + preamble[len(preamble.split("\n", 1)[0]):]
    preamble = f"{preamble}\n\n" if preamble else ""
    points = "\n".join(f"- {p}" for p in entry.points)
    more = f"{entry.more.strip()}\n\n" if entry.more.strip() else ""
    lang = entry.heading_lang or i18n.get_language()
    h = _set(lang)
    notes = f"{h['comments']}\n\n{entry.notes.strip()}\n\n" if entry.notes.strip() else ""
    return (f"---\n{head}---\n\n{preamble}{h['summary']}\n\n{entry.summary}\n\n{h['points']}\n\n{points}\n\n"
            f"{more}{notes}{h['body']}\n\n{entry.body}\n{attachment_links(entry.body, lang)}")


def _between(text: str, start: str, end: str | None) -> str:
    i = text.find(f"\n{start}\n")
    if i < 0:
        return ""
    i += len(start) + 2
    j = text.find(f"\n{end}\n", i) if end else -1
    return (text[i:j] if j >= 0 else text[i:]).strip("\n")


def _first_heading_lang(rest: str) -> str:
    """The set of the note's first managed heading (summary, points, comments, body): a heading of the
    other set further down, inside the body say, never switches the note. "" when it has none."""
    found = [(i, lang) for lang in NOTE_LANGS for role in ("summary", "points", "comments", "body")
             if (i := rest.find(f"\n{HEADINGS[lang][role]}\n")) >= 0]
    return min(found)[1] if found else ""


def parse(text: str) -> Entry:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("missing frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    for key in REQUIRED_KEYS:
        if fm.get(key) in (None, ""):
            raise ValueError(f"missing required frontmatter key: {key}")
    rest = "\n" + m.group(2)
    saved_at = fm["saved_at"]
    if not isinstance(saved_at, datetime):
        saved_at = datetime.fromisoformat(str(saved_at))
    lang = _first_heading_lang(rest)
    order = (lang, *(x for x in NOTE_LANGS if x != lang)) if lang else NOTE_LANGS

    def heading(role: str, scope: str) -> str:  # the note's own set first; a heading of the other set is read too
        return next((HEADINGS[x][role] for x in order if f"\n{HEADINGS[x][role]}\n" in scope), HEADINGS[order[0]][role])

    body_h = heading("body", rest)
    body_at = rest.find(f"\n{body_h}\n")
    head = rest[:body_at] if body_at >= 0 else rest  # headings inside the body must not count
    summary_h, points_h, notes_h = heading("summary", head), heading("points", head), heading("comments", head)
    known = [i for h in (summary_h, points_h, notes_h) if (i := head.find(f"\n{h}\n")) >= 0]
    preamble = head[:min(known)].strip("\n") if known else ""
    lines = _between(head, points_h, notes_h).split("\n")
    points, n = [], 0
    while n < len(lines):  # the list (blank lines inside it too), then the user's own text
        if lines[n].startswith("- "):
            points.append(lines[n][2:].strip())
            n += 1
            continue
        nxt = next((k for k in range(n, len(lines)) if lines[k].strip()), len(lines))
        if nxt < len(lines) and lines[nxt].startswith("- ") and not lines[n].strip():
            n = nxt
            continue
        break
    h1 = _title_line(preamble)
    tags = fm.get("tags") or []
    return Entry(
        id=str(fm["id"]),
        title=h1 if h1 is not None else str(fm.get("title", "")),
        source=str(fm.get("source", "")),
        saved_at=saved_at,
        channel=str(fm.get("channel", "")),
        status=str(fm.get("status", "ok")),
        url=fm.get("url"),
        media_url=fm.get("media_url"),
        media_id=None if fm.get("media_id") is None else str(fm["media_id"]),
        author=None if fm.get("author") is None else str(fm["author"]),
        language=fm.get("language"),
        tags=[tags] if isinstance(tags, str) else [str(t) for t in tags],  # "tags: AI" typed by hand
        summary=_between(head, summary_h, points_h).strip(),
        points=points,
        body=_without_links(_between(rest, body_h, None)).strip("\n"),
        notes=_between(head, notes_h, None).strip(),
        extra={k: v for k, v in fm.items() if k not in FRONTMATTER_KEYS},
        url_aliases=[str(u) for u in fm["url_aliases"] if u] if isinstance(fm.get("url_aliases"), list) else [],
        title_set=fm.get("title_set") is True,
        fetched=None if fm.get("fetched") in (None, "") else str(fm["fetched"]),
        fetched_title=None if fm.get("fetched_title") in (None, "") else str(fm["fetched_title"]),
        preamble=preamble,
        more="\n".join(lines[n:]).strip("\n"),
        heading_lang=lang,
    )


def file_id(text: str) -> str | None:
    """The Octopus id in a Markdown file's frontmatter; None for the user's own notes (no id, or not ours)."""
    m = re.match(r"---\n(.*?)\n---(?:\n|\Z)", text, re.S)
    found = FM_ID_RE.search(m.group(1)) if m else None
    return found.group(1) if found else None


GENERATED = {"summary", "points", "tags", "language", "status"}  # an op rewrites these: its version wins


def rebase(base: Entry, mine: Entry, theirs: Entry) -> Entry:
    """mine is base plus what an op changed; theirs is the file as it is now, edited meanwhile (in Obsidian).
    The result is theirs plus the op's change: text the op appended is appended to the user's version; for any
    other field both changed, the op wins only for what it generates (summary, points, tags…)."""
    out = copy.deepcopy(theirs)
    for f in fields(Entry):
        b, m, t = getattr(base, f.name), getattr(mine, f.name), getattr(theirs, f.name)
        if m == b or m == t:
            continue
        if t == b or f.name in GENERATED:
            setattr(out, f.name, copy.deepcopy(m))
        elif f.name in ("body", "notes") and isinstance(m, str) and m.startswith(b):
            added = m[len(b):]
            setattr(out, f.name, f"{t.rstrip()}{added}" if t.strip() else added.lstrip("\n"))
    return out


ENTRY_ID_RE = re.compile(r"\d{8}-[0-9a-f]{6}")
SHORT_ID_RE = re.compile(r"[0-9a-fA-F]{3,6}")  # a part of an id's last 6 characters, as typed by hand
PREFIX_MIN = 4  # the first characters after "-" name an entry from 4 on; 3 only at the end ("123" is too common)


Sig = tuple[int, int]  # (mtime_ns, size): has a file changed


def file_sig(path: Path) -> Sig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


Snapshot = dict[Path, tuple[Sig, str | None]]  # every .md file read, with its id (None: the user's own note)


def read_marker(root: Path) -> str | None:
    try:
        return (root / MARKER).read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def write_marker(root: Path, identity: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(f"{identity}\n", encoding="utf-8")


class Library:
    """Entry files anywhere under root: Octopus names new ones YYYY/MM/<id>-<slug>.md, but the user may rename or
    move them (in Obsidian); a file belongs to the id in its frontmatter (or in that name). Hidden folders
    (.obsidian, .trash) and the attachments folder are not looked into; files without an Octopus id are the user's.

    Each id has one canonical file: the one the index has (index_path / index_paths, set by SavePipeline) while it
    exists and carries the id; else the one Octopus last wrote or found; a file is adopted only when it is the one
    candidate. Other files with the id are the user's copies: never read as the entry, written, moved or deleted.
    Walking the folder is the scanner's job (snapshot, in a thread); find() never walks."""

    def __init__(self, root: Path, trash: Path, attachments: Path | None = None):
        self.root = root
        self.trash = trash
        self.attachments = attachments
        self._files: Snapshot = {}  # the last walk (files read successfully only)
        self._ids: dict[str, Path] = {}  # id -> canonical file
        self._written: dict[Path, Sig] = {}  # what Octopus itself last wrote to each file
        self._warned: set[tuple[str, Path]] = set()
        self.index_path: Callable[[str], str | None] | None = None
        self.index_paths: Callable[[], dict[str, str]] | None = None
        self.index_buried: Callable[[str], bool] | None = None  # deleted through Octopus: no file is it any more
        self.index_buried_all: Callable[[], set[str]] | None = None
        self.ambiguous: set[str] = set()  # ids with several files and none of them canonical: left alone
        self.unreadable: set[Path] = set()  # files the last walk could not read (offline iCloud, a lock…)
        self.missed = False  # an indexed entry's file was not where the index says: the scanner should look soon

    def path_for(self, entry: Entry) -> Path:
        d = entry.saved_at
        return self.root / f"{d:%Y}" / f"{d:%m}" / f"{entry.id}-{_slug(entry.title)}.md"

    def _octopus_named(self, path: Path) -> bool:
        """The file is where and how Octopus names it for the title it has now (so a new title may rename it)."""
        try:
            return path == self.path_for(self.load(path))
        except (*READ_ERRORS, OSError):
            return False

    def save(self, entry: Entry) -> Path:
        old = self.find(entry.id)
        path = self.path_for(entry)
        if old is not None and path != old and (path.exists() or not self._octopus_named(old)):
            path = old  # renamed or moved by the user (or the new name is taken): written where it is
        n = 1
        while old is None and path.exists():  # a new entry never writes over a file that is there
            path = path.with_name(f"{path.stem.rsplit(' ', 1)[0]} {n}.md")
            n += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")  # unique; *.md globs never see it
        try:
            tmp.write_text(render(entry), encoding="utf-8")
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if old and old != path:
            old.unlink()
            self._forget(old)
        if has_links_block(entry.body):
            self._warn(("unclosed", path), "%s: its links block lost its closing marker; kept as text, a new block "
                       "added", path)
        if (sig := file_sig(path)) is not None:
            self._files[path] = (sig, entry.id)
            self._written[path] = sig
        self._ids[entry.id] = path
        return path

    def wrote(self, path: Path, sig: Sig) -> bool:
        """The file is exactly as Octopus last wrote it (not edited since)."""
        return self._written.get(path) == sig

    def _forget(self, path: Path) -> None:
        self._files.pop(path, None)
        self._written.pop(path, None)

    def load(self, path: Path) -> Entry:
        return parse(path.read_text(encoding="utf-8"))

    def paths(self, placeholders: set[Path] | None = None) -> list[Path]:
        """Every Markdown file under root, sorted, except in hidden folders and the attachments folder. iCloud
        placeholders of evicted notes (.name.md.icloud) go to placeholders: there, but not readable now."""
        out: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            dirnames[:] = [n for n in dirnames if not n.startswith(".") and here / n != self.attachments]
            for n in filenames:
                if n.endswith(".md") and not n.startswith("."):
                    out.append(here / n)
                elif placeholders is not None and n.startswith(".") and n.endswith(".md.icloud"):
                    placeholders.add(here / n)
        return sorted(out)

    @staticmethod
    def read_id(path: Path) -> str | None:
        """The id in the frontmatter; else, for a file Octopus named (<id>-<slug>.md) whose frontmatter got broken,
        the id in its name: still an entry, just unreadable. Not UTF-8 is no error (the id is ASCII); raises OSError
        (try again later)."""
        with path.open(encoding="utf-8", errors="replace") as f:
            found = file_id(f.read(65536))
        return found or Library.named_id(path)

    @staticmethod
    def named_id(path: Path) -> str | None:
        named = re.match(rf"({ENTRY_ID_RE.pattern})-", path.name)
        return named.group(1) if named else None

    def _id_at(self, path: Path) -> str | None:
        sig = file_sig(path)
        cached = self._files.get(path)
        if sig is not None and cached and cached[0] == sig:
            return cached[1]
        try:
            fid = self.read_id(path)
        except OSError:
            return None
        if sig is not None:
            self._files[path] = (sig, fid)
        return fid

    def snapshot(self, cache: Snapshot) -> tuple[Snapshot, set[Path]]:
        """Walk root (blocking: the scanner runs it in a thread): every .md file with its id, reading only new or
        changed ones (cache: the last walk). A file that cannot be read now keeps what it was, or is unreadable."""
        files: Snapshot = {}
        unreadable: set[Path] = set()
        for path in self.paths(unreadable):
            sig = file_sig(path)
            cached = cache.get(path)
            if sig is not None and cached and cached[0] == sig:
                files[path] = cached
                continue
            try:
                fid = self.read_id(path)
            except OSError as e:
                unreadable.add(path)
                self._warn(("unreadable", path, sig), "cannot read %s now (%s): tried again next pass", path, e)
                if cached:
                    files[path] = (cached[0], cached[1])  # what it was; its old signature: read again next time
                elif named := self.named_id(path):
                    files[path] = ((-1, -1), named)  # an entry's own file: present, read next time
                continue
            if sig is not None:
                files[path] = (sig, fid)
        return files, unreadable

    def adopt(self, files: Snapshot, unreadable: set[Path], stored: dict[str, str],
              buried: set[str] = frozenset()) -> dict[str, Path]:
        """Each id's canonical file from a walk (see the class doc), remembered; returns id -> file. Files carrying
        a buried id (deleted through Octopus) are no one's."""
        candidates: dict[str, list[Path]] = {}
        for path, (_, fid) in files.items():
            if fid is not None and fid not in buried:
                candidates.setdefault(fid, []).append(path)
        ids: dict[str, Path] = {}
        ambiguous: set[str] = set()
        for fid, paths in candidates.items():
            known = Path(stored[fid]) if fid in stored else None
            if known is not None and known in unreadable:
                ids[fid] = known  # there, only not readable now: no copy takes its place
                continue
            pick = next((p for p in (known, self._ids.get(fid)) if p is not None and p in paths), None)
            if pick is None and len(paths) == 1:
                pick = paths[0]
            if pick is None:
                ambiguous.add(fid)
                self._warn(("several", paths[0]), "%s files carry the id %s and none is the one Octopus knows: "
                           "left alone until one is left (%s)", len(paths), fid, ", ".join(map(str, paths)))
                continue
            ids[fid] = pick
            for p in paths:
                if p != pick:
                    self._warn(("copy", p), "%s is a copy of %s (same id %s): left alone", p, pick, fid)
        for fid, known in stored.items():  # its file is there but could not be read this time
            if fid not in ids and Path(known) in unreadable:
                ids[fid] = Path(known)
        for fid, path in self._ids.items():  # written since the walk began
            if fid not in ids and fid not in ambiguous and path not in files and path.is_file():
                ids[fid] = path
        self._files, self._ids, self.ambiguous, self.unreadable = files, ids, ambiguous, unreadable
        return dict(ids)

    def _warn(self, key: tuple[str, Path], msg: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(msg, *args)

    def refresh(self, revive: bool = False) -> dict[str, Path]:
        """Walk now, on this thread (the CLI: reindex, library move): id -> canonical file. revive: files of ids
        deleted through Octopus count too once the deleted file is no longer in the trash (reindex: a note put back
        from the trash; a user's copy of one still there stays out)."""
        files, unreadable = self.snapshot(self._files)
        buried = self.buried_ids()
        if revive:
            buried &= self.trashed_ids()
        return self.adopt(files, unreadable, self._stored_all(), buried)

    def trashed_ids(self) -> set[str]:
        """Ids of the notes in the trash (delete() moves them there by name)."""
        ids = set()
        for path in self.trash.glob("*.md") if self.trash.is_dir() else ():
            try:
                ids.add(self.read_id(path))
            except OSError:
                ids.add(self.named_id(path))
        return ids - {None}

    def find(self, entry_id: str) -> Path | None:
        """The entry's canonical file: the one known, else the index's (while it carries the id), else for an entry
        the index does not have, the one file named <id>-….md in its month's folder. Never walks the folder."""
        if not ENTRY_ID_RE.fullmatch(entry_id):
            return None
        path = self._ids.get(entry_id)
        if path is not None and path.is_file():
            return path
        if self._buried(entry_id):
            return None
        stored = self._stored(entry_id)
        if stored is not None:
            path = Path(stored)
            if path.is_file() and self._id_at(path) == entry_id:
                self._ids[entry_id] = path
                return path
            self.missed = True  # renamed or moved since the last walk: the scanner finds it
            return None
        if entry_id in self.ambiguous:
            return None
        found = [p for p in self._month_dir(entry_id).glob(f"{entry_id}-*.md") if self._id_at(p) == entry_id]
        if len(found) != 1:
            return None
        self._ids[entry_id] = found[0]
        return found[0]

    def _stored(self, entry_id: str) -> str | None:
        try:
            return self.index_path(entry_id) if self.index_path else None
        except sqlite3.ProgrammingError:  # the database was closed (shutting down): by the files alone
            return None

    def _buried(self, entry_id: str) -> bool:
        try:
            return bool(self.index_buried and self.index_buried(entry_id))
        except sqlite3.ProgrammingError:
            return False

    def buried_ids(self) -> set[str]:
        try:
            return self.index_buried_all() if self.index_buried_all else set()
        except sqlite3.ProgrammingError:
            return set()

    def _stored_all(self) -> dict[str, str]:
        try:
            return self.index_paths() if self.index_paths else {}
        except sqlite3.ProgrammingError:
            return {}

    def _month_dir(self, entry_id: str) -> Path:
        return self.root / entry_id[:4] / entry_id[4:6]

    def taken(self, entry_id: str) -> bool:
        """A new id is free (no walk: a fresh random id can only clash with a file named by it)."""
        return (entry_id in self._ids or self._stored(entry_id) is not None or self._buried(entry_id)
                or any(self._month_dir(entry_id).glob(f"{entry_id}-*.md")))

    def get(self, entry_id: str) -> Entry | None:
        path = self.find(entry_id)
        return self.load(path) if path else None

    def match_short(self, token: str) -> list[str]:
        """Ids whose 6 characters after "-" end with the token (3-6 hex characters, any case) or, from
        PREFIX_MIN characters on, start with it; each once, newest first."""
        if not SHORT_ID_RE.fullmatch(token):
            return []
        t = token.lower()
        ids = set(self._ids) | set(self._stored_all())
        return sorted((i for i in ids if (i.endswith(t) or (len(t) >= PREFIX_MIN and i[9:].startswith(t)))
                       and self.find(i)), reverse=True)

    def resolve(self, token: str) -> tuple[str | None, list[str]]:
        """(id, []) for an existing full id or a short id naming one entry (see match_short); (None, matches)
        when it names several; (None, []) when there is none."""
        if ENTRY_ID_RE.fullmatch(token):
            return (token, []) if self.find(token) else (None, [])
        matches = self.match_short(token)
        return (matches[0], []) if len(matches) == 1 else (None, matches)

    def delete(self, entry_id: str) -> bool:
        """Moves the canonical file to the trash; the user's copies stay."""
        path = self.find(entry_id)
        if not path:
            return False
        self.trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), self.trash / path.name)
        self._forget(path)
        self._ids.pop(entry_id, None)
        return True

    def iter_entries(self, revive: bool = False) -> Iterator[tuple[Path, Entry]]:
        """Walks now (see refresh). A file that cannot be read or parsed is skipped with a warning."""
        for path in sorted(self.refresh(revive).values()):
            try:
                entry = self.load(path)
            except (*READ_ERRORS, OSError) as exc:
                logger.warning("skipping unreadable library file %s: %s", path, exc)
                continue
            yield path, entry
