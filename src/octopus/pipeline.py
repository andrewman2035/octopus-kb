"""Fetch → summarize → Markdown → index."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import re
import shutil
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from octopus.embed import EmbedError
from octopus.fetchers import FetcherRegistry, manual_source
from octopus.fetchers.base import FETCH_CANCEL, FetchError, FetchResult
from octopus.fetchers.web import MIN_TEXT, looks_walled, parse_html
from octopus.i18n import aliases, t, text_in
from octopus.index import Index
from octopus import i18n
from octopus.library import (ATTACH_LINE_RE, HEADINGS, NOTE_LANGS, READ_ERRORS, Entry, Library, Sig, file_sig,
                             new_entry_id, rebase)
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.media import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES, MediaError, MediaTools, NoSpeechError
from octopus.models import Attachment
from octopus.summarize import summarize
from octopus.titles import post_title
from octopus.urls import host_of, page_aliases

log = logging.getLogger(__name__)
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm")
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_NAME_BYTES = 200
MAX_TITLE = 100
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# the note lines merges and media write, in both languages' words (P25): both are read, the configured one is written
_ADDED = [i18n.CATALOGS[lang]["note.added"].split("{stamp}") for lang in NOTE_LANGS]
SUPPLEMENT_RE = re.compile("(?m)^(?:" + "|".join(re.escape(before) for before, _ in _ADDED) + ")")
SUPPLEMENT_HEADING_RE = re.compile("^(?:" + "|".join(re.escape(before) + r"[^\n]*" + re.escape(after)
                                                    for before, after in _ADDED) + r")\n\n")
KIND_LABEL = {kind: i18n.CATALOGS["zh"][f"note.kind.{kind}"] for kind in ("image", "audio", "video", "file")}
_KIND_WORDS = sorted({i18n.CATALOGS[lang][f"note.kind.{kind}"] for lang in NOTE_LANGS for kind in KIND_LABEL})
MEDIA_HEADING_RE = re.compile(rf"(?m)^### ({'|'.join(map(re.escape, _KIND_WORDS))}) \d+(\uff08| \()")  # a full-width or " (" before the file name
# an attachment line of an entry's body, in either language: (?P<prefix>…)attachments/<id>/<name>
_ATTACH_PREFIX = "(?P<prefix>" + "|".join(re.escape(HEADINGS[lang]["attach_line"]) for lang in NOTE_LANGS) + ")"


def _note_lang(entry: Entry | None) -> str:
    """The language of the lines added to a note: its own headings', a new note's the configured."""
    return (entry.heading_lang if entry is not None else "") or i18n.get_language()


def _attach_line(entry_id: str, name: str, lang: str | None = None) -> str:
    return f"{text_in(lang or i18n.get_language(), 'note.attach_line')}attachments/{entry_id}/{name}"


def _attach_re(entry_id: str, name: str) -> str:
    return rf"^{_ATTACH_PREFIX}attachments/{re.escape(entry_id)}/{re.escape(name)}$"
# links whose page the browser's HTML says best (P17): the generic article reader, WeChat articles and X; a
# platform with no automated path (reddit …) too. Every other fetcher gets media or a file (YouTube, Bilibili,
# podcasts, PDF): by its link. A fetcher with a with_page method (X, P29) has its link fetched as well, the page as
# the fallback (_fetch_link_first)
PAGE_FETCHERS = {"web", "wechat_mp", "x"}
__getattr__ = aliases({"MANUAL_NOTE": "pipeline.manual_note", "RESUMMARIZE_NOTE": "pipeline.resummarize_note"})


Sections = list[tuple]  # (section text, file names[, unreadable notes]) for split()


def _newlines(text: str) -> str:
    """\r\n and \r as \n, the way a Markdown file reads back (read_text translates them)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def now_local() -> datetime:
    return datetime.now().astimezone()


@dataclass
class SaveOutcome:
    entry: Entry
    local_fallback: bool  # the summary came from the local model after the primary failed (the local-model note)
    note: str | None = None
    summary_failed: bool = False
    merged: bool = False
    added_section: str | None = None  # a merge: the exact "## Added (…)" text it appended (for "save separately")
    added_files: list[str] = field(default_factory=list)  # a merge: the files it moved into attachments/<id>/
    added_notes: list[str] = field(default_factory=list)  # a merge: what could not be read of them
    filled: bool = False  # the Chrome extension's page replaced an entry saved earlier (P36): its id kept
    stale: bool = False  # a refetch not applied: the entry changed meanwhile (completed, edited) (P36 fix round 3)


def body_sha(text: str) -> str:
    """What Entry.fetched holds: the body as Octopus wrote it, whatever the whitespace around it."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def as_fetched(entry: Entry, head: str, page_text: str) -> bool:
    """The entry's text before its supplements is still what was fetched (P36 fix round 1, I-1), so a fill may
    replace it: empty, or its sha the one stored when it was written. An entry saved before the sha was kept: only
    a login wall, or text the page has too."""
    text = head.strip()
    if not text:
        return True
    if entry.fetched:
        return entry.fetched == body_sha(text)
    return looks_walled(text) or _squash(text) in _squash(page_text)


def fillable(entry: Entry, page: bool) -> bool:
    """A link's entry its refetch may replace: one nothing could be read for, or (P36) one only partly read when the
    Chrome extension sends the page the user is logged in to."""
    return entry.status == "needs_manual" or (page and entry.status == "partial")


def attachment_kind(a: Attachment) -> str:
    """image / audio / video / file as the pipeline handles it: a document named .png is an image, a video
    file is transcribed like audio. App uses the same test to group screenshots."""
    suffix = (Path(a.filename or (a.path.name if a.path else "")).suffix or "").lower()
    if a.kind in ("image", "audio", "video"):
        return a.kind
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES or suffix in VIDEO_SUFFIXES:
        return "audio"
    return "file"

class SavePipeline:
    def __init__(self, library: Library, index: Index, llm: LLMRouter, registry: FetcherRegistry,
                 clock: Callable[[], datetime] = now_local, media: MediaTools | None = None):
        self.library = library
        self.index = index
        self.llm = llm
        self.registry = registry
        self.clock = clock
        self.media = media
        self.summary_language = ""  # config's summary_language (App sets it): "zh" | "en" | "auto"; "": `language`
        self._heavy = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        # the index knows each entry's canonical file (P18: a user's copy with the same id is never it)
        library.index_path, library.index_paths = index.path_of, index.paths
        library.index_buried, library.index_buried_all = index.is_buried, index.buried

    @asynccontextmanager
    async def _entry_lock(self, entry_id: str):
        lock = self._locks.setdefault(entry_id, asyncio.Lock())
        async with lock:
            yield

    entry_lock = _entry_lock  # for the library scanner (sync.py): an outside edit is indexed between two ops

    async def locate(self, entry_id: str) -> Path | None:
        """The entry's file, also when it was renamed in Obsidian since the last scan: an indexed entry not where the
        index says gets one walk of the folder (in a thread) before it counts as gone."""
        path = self.library.find(entry_id)
        if path is None and self.index.path_of(entry_id) is not None:
            files, unreadable = await asyncio.to_thread(self.library.snapshot, dict(self.library._files))
            self.library.adopt(files, unreadable, self.index.paths(), self.index.buried())
            path = self.library.find(entry_id)
        return path

    async def _read(self, entry_id: str) -> tuple[Entry, Sig | None] | None:
        """The entry as its file is now, and that file's signature (taken first: a later change shows)."""
        path = await self.locate(entry_id)
        if path is None:
            return None
        sig = file_sig(path)
        return self.library.load(path), sig

    def _edited_meanwhile(self, entry_id: str, sig: Sig | None) -> Entry | None:
        """The entry as it is now when its file changed since sig (edited in Obsidian while a model call ran);
        None when it did not (or cannot be read now: then the op's version is written)."""
        path = self.library.find(entry_id)
        if path is None or file_sig(path) == sig:
            return None
        try:
            fresh = self.library.load(path)
        except READ_ERRORS:
            return None
        log.info("%s was edited while a change was being made: applying the change to the edited file", entry_id)
        return fresh

    async def heavy_call(self, fn, *args):
        """Run a heavy local model call (Whisper, OCR) in a thread, one at a time."""
        async with self._heavy:
            return await asyncio.to_thread(fn, *args)

    def reads_page(self, url: str) -> bool:
        """A page the Chrome extension sends for this link is read from its HTML (else the link is fetched)."""
        fetcher = self.registry.pick(url)
        return fetcher is None or fetcher.name in PAGE_FETCHERS

    async def _read_page(self, url: str, page: Path, title: str | None) -> FetchResult | None:
        """The page as the browser showed it, read with the extraction its link would get, without a fetch; the
        source is the platform's (x, reddit …). None when it cannot be read (not a page link, a parser error, the
        file gone): the link is fetched."""
        if not self.reads_page(url):
            return None
        fetcher = self.registry.pick(url)
        parse = getattr(fetcher, "parse", None) or parse_html
        try:
            html = await asyncio.to_thread(page.read_text, encoding="utf-8", errors="replace")
            result = await asyncio.to_thread(parse, url, html)
        except Exception as e:
            log.info("page of %s not read (%s): fetching the link", host_of(url), type(e).__name__)
            return None
        result.source = fetcher.name if fetcher is not None else manual_source(url) or "web"
        result.title = title or result.title
        return result

    async def _fetch_link(self, url: str) -> FetchResult:
        fetcher = self.registry.pick(url)
        if fetcher is None:
            return FetchResult(url=url, source=manual_source(url) or "web", status="needs_manual", note=t("pipeline.manual_note"))
        stop = threading.Event()  # set on cancel: the thread's downloads stop (asyncio cannot stop a thread)
        token = FETCH_CANCEL.set(stop)  # to_thread copies this context into the fetch's thread
        try:
            return await asyncio.to_thread(fetcher.fetch, url)  # FetchError propagates to the worker
        except asyncio.CancelledError:
            stop.set()
            raise
        except FetchError:
            raise
        except Exception as e:  # parser bugs are not transient: send to the needs_manual flow
            raise FetchError(t("pipeline.parse_failed", error=e), retryable=False) from e
        finally:
            FETCH_CANCEL.reset(token)

    async def _fetch_link_first(self, url: str, read: FetchResult, with_page) -> FetchResult:
        """A platform whose fetch gets more than its page (X, P29: the video, the author): the link is fetched even
        for a page the extension sent, and the page is the fallback. It is used when the fetch fails or needs manual
        help; otherwise the fetcher's with_page decides whose text it is (X: a long post's, an Article's or a
        thread's text from the page; the fetch's videos and author kept). A page
        with no text, or only a login wall, is never used: the fetch's own outcome stands."""
        usable = bool(read.text.strip()) and not looks_walled(read.text)
        try:
            result = await self._fetch_link(url)
        except FetchError as e:
            if not usable:
                raise
            log.info("page of %s: the link fetch failed (%s): the page is kept", host_of(url), e)
            return read
        if not usable:
            return result
        if result.status == "needs_manual":
            self._discard_images(result)
            self._discard_videos(result)
            return read
        return with_page(result, read)

    async def _fetch(self, url: str, page: Path | None = None, title: str | None = None) -> FetchResult:
        """page: the HTML the Chrome extension sent for this link (P17), read instead of fetching. An article
        (MIN_TEXT or more, or a picture post) is used as it is. Shorter text is kept unless fetching the link gets
        more: never fetched on a platform with no automated path, and kept when the fetch fails (any FetchError: the
        browser could read the page where this process may not reach the site), needs manual help or gets less (fix
        rounds 1 and 3). A page with no text, or only a login / pay / app wall, is fetched as before (fix round 2)."""
        read = await self._read_page(url, page, title) if page is not None else None
        short = read is not None and read.status != "ok" and not read.image_paths
        keep = short and bool(read.text) and not looks_walled(read.text)
        with_page = getattr(self.registry.pick(url), "with_page", None) if read is not None else None
        if with_page is not None:
            result = await self._fetch_link_first(url, read, with_page)
        elif read is not None and not short:
            result = read
        elif keep and self.registry.pick(url) is None:
            result = read  # reddit, WeChat Channels …: the link would only ask for screenshots
        else:
            try:
                result = await self._fetch_link(url)
            except FetchError as e:
                if not keep:
                    raise
                log.info("page of %s: the link fetch failed too (%s): the page's short text is kept", host_of(url), e)
                result = read
            else:
                if keep and (result.status == "needs_manual" or result.status == "partial"
                             and len(result.text) < len(read.text)):
                    self._discard_images(result)
                    self._discard_videos(result)
                    result = read
        if result.doc_path is not None:
            await self._extract_doc_into(result)
        if result.audio_path is not None:
            await self._transcribe_into(result)
        if result.video_paths:
            await self._transcribe_videos_into(result)
        if result.image_paths:
            try:
                await self._ocr_images_into(result)
            except BaseException:
                self._discard_images(result)
                raise
        return result

    def _remove_download(self, path: Path | None) -> None:
        if path is not None and self.media is not None and self.media.downloads_dir in path.parents:
            path.unlink(missing_ok=True)

    def _in_downloads(self, path: Path) -> bool:
        return self.media is not None and path.resolve().is_relative_to(self.media.downloads_dir.resolve())

    def _discard_images(self, r: FetchResult) -> None:
        for path in r.image_paths:
            if self._in_downloads(path):
                path.unlink(missing_ok=True)
                self._rmdir_if_empty(path.parent)
        r.image_paths = []

    async def _ocr_images_into(self, r: FetchResult) -> None:
        """Body = caption + the OCR text of every image; the files themselves are attached in store()."""
        caption = r.text.strip()
        parts = [caption] if caption else []
        failed, errors = [], []
        for n, path in enumerate(r.image_paths, start=1):
            try:
                if self.media is None:
                    raise MediaError(t("pipeline.no_ocr"))
                text = (await self.heavy_call(self.media.ocr, path)).strip()
            except MediaError as e:
                failed.append(n)
                errors.append(str(e))
                text = t("note.failed", error=e)
            parts.append(f"### {t('note.kind.image')} {n}\n\n{text or t('note.no_text')}")
        r.text = "\n\n".join(parts)
        if failed and len(failed) == len(r.image_paths) and not caption:
            r.status, note = "needs_manual", errors[0]
        elif failed:
            which = t("common.list_sep").join(t("pipeline.image_nth", n=n) for n in failed)
            r.status, note = "partial", t("pipeline.images_failed", which=which)
        else:
            return
        r.note = f"{r.note}{t('common.note_sep')}{note}" if r.note else note
        if self.media is None:
            self._discard_images(r)

    def _move_downloads(self, entry_id: str, paths: list[Path]) -> list[Path]:
        """Move fetched images from downloads/ into attachments/<entry_id>/ (not from the channels' incoming dir)."""
        dest = self.media.attachments_dir / entry_id
        moved: list[Path] = []
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for path in paths:
                if not self._in_downloads(path):
                    raise MediaError(t("pipeline.image_not_downloaded", name=path.name))
                target = dest / path.name
                n = 1
                while target.exists():
                    target = dest / f"{path.stem}-{n}{path.suffix}"
                    n += 1
                shutil.move(str(path), target)
                moved.append(target)
        except BaseException:
            self._unmove_downloads(dest, moved)
            raise
        for folder in {p.parent for p in paths}:
            self._rmdir_if_empty(folder)
        return moved

    def _unmove_downloads(self, dest: Path, moved: list[Path]) -> None:
        for path in moved:
            path.unlink(missing_ok=True)
        self._rmdir_if_empty(dest)

    async def _extract_doc_into(self, r: FetchResult) -> None:
        path = r.doc_path
        try:
            if self.media is None:
                raise MediaError(t("pipeline.no_pdf"))
            text = await self.heavy_call(self.media.pdf, path)
            r.text = text
            if not text.strip():
                r.status, r.note = "needs_manual", t("pipeline.pdf_no_text")
        except MediaError as e:
            r.status, r.note = "needs_manual", str(e)
        finally:
            r.doc_path = None
            self._remove_download(path)

    async def _transcribe_into(self, r: FetchResult) -> None:
        path = r.audio_path
        try:
            if self.media is None:
                raise MediaError(t("pipeline.no_asr"))
            heard = await self.heavy_call(self.media.transcribe, path)
            r.text = f"{r.text.strip()}\n\n{heard.text}".strip() if r.text.strip() else heard.text
            r.language = r.language or heard.language
        except MediaError as e:
            r.status = "partial" if r.text.strip() else "needs_manual"
            r.note = str(e)
        finally:
            r.audio_path = None
            self._remove_download(path)

    def _discard_videos(self, r: FetchResult) -> None:
        for path in r.video_paths:
            if self.media is None:  # no downloads folder to check against: the fetcher downloaded it (P29)
                path.unlink(missing_ok=True)
            else:
                self._remove_download(path)
        r.video_paths, r.video_numbers = [], []

    async def _transcribe_videos_into(self, r: FetchResult) -> None:
        """A post's videos (X), transcribed in order after its text: one as the transcript, several each under its
        own "### Video n" (n: its place in the post, so a video that did not download leaves a gap). One with no
        speech says so and keeps the status; one that cannot be read keeps the text and says so (partial); the files
        are removed."""
        paths, caption = list(r.video_paths), r.text.strip()
        numbers = r.video_numbers if len(r.video_numbers) == len(paths) else list(range(1, len(paths) + 1))
        headed = len(paths) > 1 or numbers != [1]
        parts = [caption] if caption else []
        errors: list[str] = []
        try:
            for n, path in zip(numbers, paths):
                failed = False
                try:
                    if self.media is None:
                        raise MediaError(t("pipeline.no_asr"))
                    heard = await self.heavy_call(self.media.transcribe, path)
                    text = heard.text
                    r.language = r.language or heard.language
                except NoSpeechError:  # music, a screen recording: nothing was missed
                    text = t("pipeline.video_no_speech")
                except MediaError as e:
                    errors.append(str(e))
                    failed = True
                    text = t("note.failed", error=e)
                if headed:
                    parts.append(f"### {t('note.kind.video')} {n}\n\n{text}")
                elif not failed:
                    parts.append(text)
        finally:
            self._discard_videos(r)
        r.text = "\n\n".join(parts)
        if not errors:
            return
        r.status = "needs_manual" if len(errors) == len(paths) and not caption else "partial"
        note = t("pipeline.video_failed", error=errors[0])
        r.note = f"{r.note}{t('common.note_sep')}{note}" if r.note else note

    def _kind_of(self, a: Attachment) -> str:
        return attachment_kind(a)

    async def _extract(self, a: Attachment) -> tuple[str, str | None]:
        """Returns (text, error note)."""
        kind = self._kind_of(a)
        if kind in ("audio", "video") and a.text and a.text.strip():
            return a.text.strip(), None
        if self.media is None or a.path is None:
            return "", t("pipeline.no_file")
        suffix = a.path.suffix.lower()
        try:
            if kind == "image":
                return await self.heavy_call(self.media.ocr, a.path), None
            if kind in ("audio", "video"):
                return (await self.heavy_call(self.media.transcribe, a.path)).text, None
            if suffix == ".pdf":
                return await self.heavy_call(self.media.pdf, a.path), None
            if suffix in TEXT_SUFFIXES:
                with a.path.open("rb") as f:
                    return f.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace"), None
            return "", t("pipeline.unsupported_file")
        except MediaError as e:
            return "", str(e)
        except OSError as e:
            return "", t("pipeline.read_failed", error=e)

    @staticmethod
    def _safe_name(a: Attachment, position: int) -> str:
        """A bare file name: no directories, never '' / '.' / '..'."""
        name = CONTROL_CHARS_RE.sub("_", Path(a.filename or (a.path.name if a.path else "")).name)
        if name in ("", ".", ".."):
            name = f"file{position}{a.path.suffix if a.path else ''}"
        if len(name.encode("utf-8")) > MAX_NAME_BYTES:  # file systems cap names at 255 bytes
            suffix = Path(name).suffix
            if len(suffix.encode("utf-8")) > 20:
                suffix = ""
            stem = name[: len(name) - len(suffix)] if suffix else name
            budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
            name = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + suffix
        return name

    def _move_into(self, entry_id: str, files: list[Attachment]) -> list[tuple[Attachment, Path]]:
        """Move staged files to attachments/<entry_id>/; returns (moved attachment, original path) pairs.

        On any failure the files already moved are put back before the error propagates."""
        dest = self.media.attachments_dir / entry_id
        incoming = self.media.incoming_dir.resolve() if self.media.incoming_dir else None
        moved: list[tuple[Attachment, Path]] = []
        try:
            dest.mkdir(parents=True, exist_ok=True)
            for position, a in enumerate(files, start=1):
                if a.path is None or not a.path.exists():
                    continue
                source = a.path.resolve()
                if incoming is not None and (source == incoming or not source.is_relative_to(incoming)):
                    raise MediaError(t("pipeline.not_staged", name=a.filename or a.path.name))
                name = self._safe_name(a, position)
                target = dest / name
                n = 1
                while target.exists():
                    target = dest / f"{Path(name).stem}-{n}{Path(name).suffix}"
                    n += 1
                if not target.resolve().is_relative_to(dest.resolve()):
                    raise MediaError(t("pipeline.unsafe_name", name=a.filename))
                shutil.move(str(a.path), target)
                moved.append((Attachment(a.kind, target, target.name, a.mime, a.text, a.size), a.path))
                if incoming is not None and source.parent != incoming:
                    self._rmdir_if_empty(a.path.parent)  # the channel's per-message _incoming/<uuid>/ dir
        except BaseException:
            self._restore(moved)
            self._rmdir_if_empty(dest)
            raise
        if not moved:
            self._rmdir_if_empty(dest)  # a retry's already-moved files keep it alive
        return moved

    @staticmethod
    def _rmdir_if_empty(path: Path) -> None:
        try:
            path.rmdir()
        except OSError:
            pass

    @staticmethod
    def _restore(moved: list[tuple[Attachment, Path]]) -> None:
        for a, original in moved:
            if a.path and a.path.exists() and a.path != original:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(a.path), original)
                SavePipeline._rmdir_if_empty(a.path.parent)

    @staticmethod
    def _transcribed(files: list[Attachment]) -> list[Attachment]:
        """Attachments with no file but text the platform already extracted (a WeChat voice transcript)."""
        return [a for a in files if a.path is None and (a.text or "").strip()]

    async def _compose(self, entry_id: str, moved: list[tuple[Attachment, Path]], caption: str,
                       transcribed: list[Attachment] = (), lang: str | None = None) -> tuple[str, list[str], bool]:
        """Body text for attachments, its lines in lang's words (None: the configured language); returns (body, error
        notes, any_text_extracted)."""
        lang = lang or i18n.get_language()
        parts, notes, got_text, counters = [], [], False, {}
        if caption.strip():
            parts.append(caption.strip())
        for a in [a for a, _ in moved] + list(transcribed):
            kind = self._kind_of(a)
            counters[kind] = counters.get(kind, 0) + 1
            text, err = await self._extract(a)
            body = text.strip() or text_in(lang, "note.failed", error=err)
            if err:
                notes.append(t("common.label", label=a.filename, value=err))
            got_text = got_text or bool(text.strip())
            heading = text_in(lang, "note.media", kind=text_in(lang, f"note.kind.{kind}"), n=counters[kind],
                              name=a.filename)
            parts.append(f"{heading}\n\n{body}")
        if moved:
            parts.append("\n".join(_attach_line(entry_id, a.filename, lang) for a, _ in moved))
        return "\n\n".join(parts), notes, got_text

    async def save_url(self, url: str, channel: str, page: Path | None = None, title: str | None = None) -> SaveOutcome:
        return await self.store(await self._fetch(url, page, title), channel)

    async def save_note(self, text: str, channel: str, url: str | None = None, title: str | None = None) -> SaveOutcome:
        """url, title: the page a selection came from (the Chrome extension); its link is the entry's source."""
        return await self.store(FetchResult(url=url, source="manual", title=title or None, text=text.strip()), channel)

    async def save_page(self, page: Path, channel: str, url: str | None = None, title: str | None = None,
                        author: str | None = None, text: str = "") -> SaveOutcome:
        """An HTML mail (P23: a forwarded newsletter) read as an article, as the extension's page is: source "email",
        url its "view in browser" link (or none). text: the mail's plain words, kept instead when the HTML reads as
        less than half of them."""
        html = await asyncio.to_thread(page.read_text, encoding="utf-8", errors="replace")
        r = await asyncio.to_thread(parse_html, url, html)
        if len(r.text) < len(text.strip()) // 2:
            r = FetchResult(url=url, source="email", text=text.strip())
        r.url, r.source = url, "email"
        r.title = title or r.title
        r.author = author or r.author
        return await self.store(r, channel)

    async def save_failed_url(self, url: str, channel: str, reason: str) -> SaveOutcome:
        result = FetchResult(url=url, source=manual_source(url) or "web", status="needs_manual", note=reason)
        return await self.store(result, channel)

    async def store(self, r: FetchResult, channel: str, *, entry_id: str | None = None) -> SaveOutcome:
        now = self.clock()
        if entry_id is None:
            entry_id = new_entry_id(now)
            while self.library.taken(entry_id):
                entry_id = new_entry_id(now)
        r.title = post_title(r.title, [r.url, *r.aliases])  # a Xiaohongshu note's text: its first sentence (P38)
        entry = Entry(
            id=entry_id, title=r.title or r.url or r.text[:20] or t("note.title"), source=r.source,
            saved_at=now, channel=channel, status=r.status, url=r.url, media_url=r.media_url,
            media_id=r.media_id, author=r.author, language=r.language, body=r.text,
            url_aliases=page_aliases(r.url, r.aliases),
        )
        moved: list[Path] = []
        try:
            if r.image_paths:
                moved = self._move_downloads(entry_id, r.image_paths)
                r.image_paths = []
                entry.body = self._with_attachment_lines(entry_id, entry.body, moved)
            fell_back, failed = await self._summarize_into(entry, title_given=bool(r.title))
            note = r.note
            if failed:
                if entry.status == "ok":
                    entry.status = "partial"
                resum = t("pipeline.resummarize_note", id=entry.id)
                note = f"{r.note}{t('common.note_sep')}{resum}" if r.note else resum
            if entry.url and entry.status == "partial" and entry.source != "manual":  # a fill may replace it later
                entry.fetched, entry.fetched_title = body_sha(entry.body), body_sha(entry.title)
            path = self.library.save(entry)
            try:
                await self.index.upsert(entry, path)
            except BaseException:
                path.unlink(missing_ok=True)  # new entry: a retry would otherwise write a second file
                raise
        except BaseException:
            self._discard_images(r)
            if moved:
                self._unmove_downloads(self.media.attachments_dir / entry_id, moved)
            raise
        return SaveOutcome(entry, fell_back, note, summary_failed=failed)

    @staticmethod
    def _with_attachment_lines(entry_id: str, body: str, files: list[Path]) -> str:
        lines = "\n".join(_attach_line(entry_id, p.name) for p in files)
        return f"{body.strip()}\n\n{lines}" if body.strip() else lines

    async def _summarize_into(self, entry: Entry, title_given: bool) -> tuple[bool, bool]:
        """Returns (local_fallback, failed); the entry is only modified on success."""
        if not entry.body.strip():
            return False, False
        try:
            s = await summarize(self.llm, entry.body, entry.title if title_given else None, self.summary_language)
        except LLMError as e:
            log.warning("summary failed for %s: %s", entry.id, e)
            return False, True
        entry.summary, entry.points, entry.tags = s.summary, s.points, s.tags
        entry.language = entry.language or s.language
        if not title_given and s.title:
            if entry.fetched_title is not None and entry.fetched_title == body_sha(entry.title):
                entry.fetched_title = body_sha(s.title)  # still Octopus's title: its record follows (P36)
            entry.title = s.title
        return s.fell_back and s.provider == "local", False

    async def resummarize(self, entry_id: str) -> SaveOutcome | None:
        async with self._entry_lock(entry_id):
            if (got := await self._read(entry_id)) is None:
                return None
            entry, sig = got
            original = copy.deepcopy(entry)
            had_summary = bool(entry.summary)
            fell_back, failed = await self._summarize_into(entry, title_given=True)
            if self.library.find(entry_id) is None:  # deleted while the model was working
                return None
            if failed:
                return SaveOutcome(entry, False, t("pipeline.resummarize_note", id=entry.id), summary_failed=True)
            if (entry.status == "partial" and entry.summary and not had_summary
                    and (entry.source == "manual" or len(entry.body) >= MIN_TEXT)):
                entry.status = "ok"
            if (fresh := self._edited_meanwhile(entry_id, sig)) is not None:
                entry = rebase(original, entry, fresh)
            path = self.library.save(entry)
            await self.index.upsert(entry, path)
            return SaveOutcome(entry, fell_back)

    def reserve_entry_id(self) -> str:
        """A fresh id for a later save_attachments(entry_id=...); free in the library and attachments dir."""
        now = self.clock()
        entry_id = new_entry_id(now)
        while self.library.taken(entry_id) or (self.media and (self.media.attachments_dir / entry_id).exists()):
            entry_id = new_entry_id(now)
        return entry_id

    async def save_attachments(self, files: list[Attachment], caption: str, channel: str,
                               entry_id: str | None = None, url: str | None = None) -> SaveOutcome:
        """With a reserved entry_id this is idempotent: a retry finds the saved entry or the already-moved files.
        url: the page an image came from (the Chrome extension), the entry's source."""
        if entry_id is None:
            entry_id = self.reserve_entry_id()
        async with self._entry_lock(entry_id):
            existing = self.library.get(entry_id)
            if existing is not None:
                return SaveOutcome(existing, False)
            moved = self._move_into(entry_id, files) if self.media else []
            try:
                # files a previous attempt already moved into place (staged copies are gone on retry)
                dest = self.media.attachments_dir / entry_id if self.media else None
                have = {a.path for a, _ in moved}
                if dest is not None and dest.is_dir():
                    moved[:0] = [(Attachment("file", p, p.name), p) for p in sorted(dest.iterdir())
                                 if p.is_file() and p not in have]
                transcribed = self._transcribed(files)
                if not moved and not transcribed and not caption.strip():
                    raise MediaError(t("pipeline.files_gone"))
                body, notes, got_text = await self._compose(entry_id, moved, caption, transcribed)
                only_files = (bool(moved) and not transcribed
                              and all(self._kind_of(a) == "file" for a, _ in moved))
                # the user sent the content itself, so never "needs_manual"; partial when something couldn't be read
                status = "ok" if (got_text or caption.strip()) and not notes else "partial"
                r = FetchResult(url=url, source="file" if only_files else "manual", title=None,
                                text=body, status=status, note=t("common.note_sep").join(notes) or None)
                return await self.store(r, channel, entry_id=entry_id)
            except BaseException:
                self._restore(moved)
                raise

    async def merge(self, entry_id: str, files: list[Attachment], text: str,
                    current: Callable[[], str] | None = None) -> SaveOutcome | str | None:
        """current: where the job wants this now, asked once the lock is held; when "save separately" moved the
        article
        to a new entry meanwhile, nothing is done and that id is returned to merge into instead."""
        async with self._entry_lock(entry_id):
            if current is not None and (target := current()) != entry_id:
                return target
            if (got := await self._read(entry_id)) is None:
                return None
            entry, sig = got
            original = copy.deepcopy(entry)
            moved = self._move_into(entry_id, files) if (self.media and files) else []
            saved = False
            try:
                transcribed = self._transcribed(files)
                with_files = any(a.path is not None for a in files)
                if with_files and not moved and self.media:  # a retry: a killed attempt may have moved them already
                    moved = self._unlinked_files(entry_id, entry.body)
                if not moved and not transcribed and not text.strip():
                    raise MediaError(t("pipeline.files_gone"))
                addition, notes, _ = await self._compose(entry_id, moved, text, transcribed, _note_lang(entry))
                stamp = self.clock().strftime("%Y-%m-%d %H:%M")
                # newlines as the file reads back, so "save separately" finds the section again
                section = _newlines(f"{text_in(_note_lang(entry), 'note.added', stamp=stamp)}\n\n{addition}")
                entry.body = f"{entry.body}\n\n{section}" if entry.body.strip() else section
                title_given = entry.title not in (entry.url, None, "") and entry.status != "needs_manual"
                fell_back, failed = await self._summarize_into(entry, title_given=title_given)
                if self.library.find(entry_id) is None:
                    self._restore(moved)
                    return None
                entry.status = "partial" if failed or notes else "ok"
                note = t("common.note_sep").join(notes) or None
                if failed:
                    resum = t("pipeline.resummarize_note", id=entry.id)
                    note = f"{note}{t('common.note_sep')}{resum}" if note else resum
                if (fresh := self._edited_meanwhile(entry_id, sig)) is not None:
                    entry, original = rebase(original, entry, fresh), fresh
                path = self.library.save(entry)
                saved = True
                await self.index.upsert(entry, path)
                return SaveOutcome(entry, fell_back, note, summary_failed=failed, merged=True, added_section=section,
                                   added_files=[a.filename for a, _ in moved], added_notes=notes)
            except BaseException:
                if saved:  # the Markdown links the files, so put it back before the files go back
                    try:
                        self.library.save(original)
                    except Exception:
                        log.exception("could not roll back %s after a failed merge", entry_id)
                self._restore(moved)
                raise

    def _unlinked_files(self, entry_id: str, body: str) -> list[tuple[Attachment, Path]]:
        """Files in attachments/<entry_id>/ that the entry's Markdown does not link yet."""
        dest = self.media.attachments_dir / entry_id
        if not dest.is_dir():
            return []
        return [(Attachment("file", p, p.name), p) for p in sorted(dest.iterdir())
                if p.is_file() and not re.search(_attach_re(entry_id, p.name), body, re.M)]

    async def split(self, entry_id: str, sections: Sections | Callable[[], Sections], new_id: str,
                    channel: str) -> SaveOutcome | None:
        """Save separately: move merged sections out of an entry into the new entry new_id, then re-summarise the
        old one.
        A section is (the exact text merge() appended, the files it moved in[, what could not be read of them:
        the new entry is partial then]). A callable is asked once the entry lock is held, so a merge that was
        running meanwhile can be part of it. Sections no longer in the body (edited by hand) are skipped with
        their files. Returns the new entry's outcome; None when the entry is gone or no section is left.

        Both model calls come first; then the files are copied, both entries saved, and only then are the
        originals deleted: whenever it stops, every link of both Markdown files points at a file that exists."""
        async with self._entry_lock(entry_id):
            if (got := await self._read(entry_id)) is None:
                return None
            entry, sig = got
            original = copy.deepcopy(entry)
            body, found, notes = entry.body, [], []
            for section, files, *unread in (sections() if callable(sections) else sections):
                section = _newlines(section).strip("\n")
                body, cut = self._cut_section(body, section)
                if cut:
                    found.append((section, files))
                    notes += unread[0] if unread else []
            if not found:
                return None
            src = self.media.attachments_dir / entry_id if self.media else None
            dest = self.media.attachments_dir / new_id if self.media else None
            if self.library.taken(new_id) or (dest is not None and dest.exists()):
                raise MediaError(t("pipeline.id_taken", id=new_id))  # never overwrite another entry's files
            names = [n for _, files in found for n in files
                     if src is not None and Path(n).name == n and n not in ("", ".", "..") and (src / n).is_file()]
            now = self.clock()
            new_body = self._split_body(entry_id, new_id, [text for text, _ in found], names)
            new = Entry(id=new_id, title=new_body[:20] or t("note.title"), source="manual", saved_at=now, channel=channel,
                        status="partial" if notes else "ok", body=new_body, heading_lang=entry.heading_lang)
            fell_back, failed = await self._summarize_into(new, title_given=False)
            note = t("common.note_sep").join(notes) or None
            if failed:
                new.status = "partial"
                resum = t("pipeline.resummarize_note", id=new_id)
                note = f"{note}{t('common.note_sep')}{resum}" if note else resum
            entry.body = body
            old_note = await self._summarize_rest(entry)
            keep_old = self.library.find(entry_id) is not None  # deleted by hand meanwhile: not brought back
            if keep_old and (fresh := self._edited_meanwhile(entry_id, sig)) is not None:
                cut = fresh.body
                for section, _ in found:
                    cut, _ = self._cut_section(cut, section)
                entry, original = rebase(original, entry, fresh), fresh
                entry.body = cut
            copied: list[Path] = []
            new_saved = old_saved = False
            try:
                for name in names:  # copies: the old Markdown still links the originals until it is saved
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / name, dest / name)
                    copied.append(dest / name)
                path = self.library.save(new)
                new_saved = True
                await self.index.upsert(new, path)
                if keep_old:
                    path = self.library.save(entry)
                    old_saved = True
                    await self.index.upsert(entry, path)
            except BaseException:
                if old_saved and not self._put_back(original):
                    raise  # the old Markdown on disk is the cut one: the new entry keeps that content, files and all
                if new_saved:
                    if (new_path := self.library.find(new_id)) is not None:
                        new_path.unlink(missing_ok=True)
                    self.index.delete(new_id)
                for p in copied:
                    p.unlink(missing_ok=True)
                if dest is not None:
                    self._rmdir_if_empty(dest)
                raise
            for name in names:  # both entries saved: the originals are no longer linked (unless by hand elsewhere)
                if keep_old and re.search(_attach_re(entry_id, name), entry.body, re.M):
                    continue
                try:
                    (src / name).unlink(missing_ok=True)
                except OSError:
                    log.warning("could not remove %s after a split", src / name, exc_info=True)
            if src is not None:
                self._rmdir_if_empty(src)
            note = t("common.note_sep").join(n for n in (note, old_note if keep_old else None) if n) or None
            return SaveOutcome(new, fell_back, note, summary_failed=failed)

    async def _summarize_rest(self, entry: Entry) -> str | None:
        """Re-summarise what is left of an entry after a split; returns a note for the user, if any."""
        if not entry.body.strip():  # nothing of its own left: back to how it was before the merge
            entry.summary, entry.points, entry.tags = "", [], []
            if entry.url:
                entry.status, entry.title = "needs_manual", entry.url
            return None
        _, failed = await self._summarize_into(entry, title_given=True)
        if not failed:
            return None
        entry.summary, entry.points, entry.tags = "", [], []  # it described what just moved out: drop it
        if entry.status == "ok":
            entry.status = "partial"
        return t("pipeline.old_entry", note=t("pipeline.resummarize_note", id=entry.id))

    def _put_back(self, original: Entry) -> bool:
        try:
            self.library.save(original)
            return True
        except Exception:
            log.exception("could not roll back %s after a failed split; the new entry keeps its content", original.id)
            return False

    @staticmethod
    def _cut_section(body: str, text: str) -> tuple[str, bool]:
        """body without the last whole-paragraph occurrence of text (and the blank line before it)."""
        if not text:
            return body, False
        hits = list(re.finditer(rf"(?:\n\n|\A){re.escape(text)}(?=\n|\Z)", body))
        if not hits:
            return body, False
        m = hits[-1]
        if m.start() == 0:  # the first thing in the body: the blank line after it goes instead
            return body[m.end():].lstrip("\n"), True
        return body[:m.start()] + body[m.end():], True

    @staticmethod
    def _split_body(entry_id: str, new_id: str, texts: list[str], moved: list[str]) -> str:
        """The new entry's body: the sections without their Added headings, media renumbered from 1,
        the moved files' attachment lines pointing at the new entry."""
        body = "\n\n".join(SUPPLEMENT_HEADING_RE.sub("", text, count=1) for text in texts)
        counters: dict[str, int] = {}

        def renumber(m: re.Match) -> str:
            counters[m.group(1)] = counters.get(m.group(1), 0) + 1
            return f"### {m.group(1)} {counters[m.group(1)]}{m.group(2)}"

        body = MEDIA_HEADING_RE.sub(renumber, body)
        for name in moved:
            body = re.sub(_attach_re(entry_id, name),
                          lambda m, name=name: f"{m.group('prefix')}attachments/{new_id}/{name}", body, flags=re.M)
        return body

    async def refetch(self, entry_id: str, url: str, page: Path | None = None, title: str | None = None,
                      or_new: str | None = None) -> SaveOutcome | None:
        """or_new: the channel to save the link as a new entry on when the entry changed meanwhile (completed or
        edited) and the refetch is not applied: a page matched by its canonical only (P36 fix round 3). The page
        already read is stored as it is, unless its images went with the entry: then it is read again."""
        if (got := await self._read(entry_id)) is None:  # also a note renamed in Obsidian since the last scan
            return None
        entry, _ = got
        if not fillable(entry, page is not None):  # supplemented in the meantime
            if or_new is not None:
                return await self.save_url(url, or_new, page, title)
            return SaveOutcome(entry, False, None, stale=True)
        r = await self._fetch(url, page, title)  # outside the entry lock: a download must not block /delete or a merge
        images = list(r.image_paths)
        try:
            out = await self._refetch_locked(entry_id, r, filling=page is not None, keep=or_new is not None)
        except BaseException:
            self._discard_images(r)  # whatever was not moved yet
            raise
        if out is not None and out.stale and or_new is not None:
            if r.image_paths == images:  # nothing of it was moved into the entry: saved as it is
                return await self.store(r, or_new)
            return await self.save_url(url, or_new, page, title)
        return out

    async def _refetch_locked(self, entry_id: str, r: FetchResult, filling: bool = False,
                              keep: bool = False) -> SaveOutcome | None:
        """filling: the extension's page is the content (P36): a partial entry is filled too, when the page gets
        more than it has (ok, or longer text). What the owner added stays (fix round 1, I-1): a title set with /title;
        text edited since it was fetched (the page then goes under its own heading below it); the supplements.
        keep: r's images stay when the entry changed meanwhile (stale): the caller saves r as a new entry."""
        async with self._entry_lock(entry_id):
            if (got := await self._read(entry_id)) is None:
                return None
            entry, sig = got
            if not fillable(entry, filling):  # supplemented while we were fetching: keep the user's content
                if not keep:
                    self._discard_images(r)
                return SaveOutcome(entry, False, None, stale=True)
            m = SUPPLEMENT_RE.search(entry.body)
            head, kept = (entry.body[:m.start()], entry.body[m.start():]) if m else (entry.body, "")
            if r.status == "needs_manual" or (entry.status == "partial" and r.status != "ok"
                                              and len(r.text.strip()) <= len(head.strip())):
                self._discard_images(r)
                return SaveOutcome(entry, False, r.note)
            original = copy.deepcopy(entry)
            moved: list[Path] = []
            saved = False
            try:
                if r.image_paths:
                    moved = self._move_downloads(entry_id, r.image_paths)
                    r.image_paths = []
                    r.text = self._with_attachment_lines(entry_id, r.text, moved)
                owned = entry.title_set or (entry.fetched_title is not None
                                            and entry.fetched_title != body_sha(entry.title))
                if not owned:  # set with /title, or changed since it was saved (fix round 2): the owner's
                    entry.title = post_title(r.title, [entry.url, r.url, *r.aliases, *entry.url_aliases]) or entry.title
                entry.source, entry.status, entry.author = r.source, r.status, r.author
                entry.media_url, entry.media_id, entry.language = r.media_url, r.media_id, r.language
                replaced = as_fetched(entry, head, r.text)
                if replaced:
                    body = f"{r.text.strip()}\n\n{kept}" if kept and r.text.strip() else (kept or r.text)
                else:  # the owner's words stay; the page goes below them, under its own heading
                    page = f"{text_in(_note_lang(entry), 'note.page_added')}\n\n{r.text.strip()}"
                    body = f"{entry.body.rstrip()}\n\n{page}"
                entry.body, entry.summary, entry.points, entry.tags = body, "", [], []
                found_by = page_aliases(entry.url, [r.url, *r.aliases])  # the tab's link, its canonical
                entry.url_aliases = list(dict.fromkeys([*entry.url_aliases, *found_by]))
                fell_back, failed = await self._summarize_into(entry, title_given=bool(r.title) or owned)
                if self.library.find(entry_id) is None:
                    self._unmove_downloads(self.media.attachments_dir / entry_id, moved)
                    return None
                if (fresh := self._edited_meanwhile(entry_id, sig)) is not None:  # edited while we fetched: theirs
                    if moved:
                        self._unmove_downloads(self.media.attachments_dir / entry_id, moved)
                    return SaveOutcome(fresh, False, None, stale=True)
                note = r.note
                if failed:
                    entry.status = "partial"
                    note = t("pipeline.resummarize_note", id=entry.id)
                head = body[:hit.start()] if (hit := SUPPLEMENT_RE.search(body)) else body
                partial = entry.status == "partial"  # a later fill may replace them again
                entry.fetched = body_sha(head) if replaced and partial else None
                if not partial or not owned:  # the owner's title keeps its record: still theirs next time
                    entry.fetched_title = body_sha(entry.title) if partial else None
                path = self.library.save(entry)
                saved = True
                await self.index.upsert(entry, path)
                if replaced:
                    self._trash_replaced(entry_id, original.body, entry.body)
                return SaveOutcome(entry, fell_back, note, summary_failed=failed, filled=filling)
            except BaseException:
                if saved:  # the Markdown links the images, so put it back before they go
                    try:
                        self.library.save(original)
                    except Exception:
                        log.exception("could not roll back %s after a failed refetch", entry_id)
                if moved:
                    self._unmove_downloads(self.media.attachments_dir / entry_id, moved)
                raise

    async def backfill_vectors(self) -> int:
        """Re-index entries saved while the embedder was down; stops at the first embedding failure."""
        ids = self.index.missing_vectors()
        if not ids:
            return 0
        try:
            await self.index.embedder.embed(["ping"])
        except EmbedError:
            return 0
        done = 0
        for entry_id in ids:
            async with self._entry_lock(entry_id):  # a delete or merge must not interleave with our upsert
                path = self.library.find(entry_id)
                if path is None or self.index.has_vectors(entry_id):
                    continue
                await self.index.upsert(self.library.load(path), path)
                if not self.index.has_vectors(entry_id):
                    return done  # the embedder went down again
                done += 1
        return done

    async def retitle(self, entry_id: str, title: str) -> Entry | None:
        """None when the entry does not exist (or title is empty after stripping)."""
        title = re.sub(r"\s+", " ", CONTROL_CHARS_RE.sub(" ", title)).strip()[:MAX_TITLE].strip()
        if not title:
            return None
        async with self._entry_lock(entry_id):
            entry = self.library.get(entry_id) if await self.locate(entry_id) else None
            if entry is None:
                return None
            entry.title, entry.title_set = title, True  # the owner's: a fill from the page keeps it (P36)
            path = self.library.save(entry)  # renames the file when the slug changes
            await self.index.upsert(entry, path)
            return entry

    async def annotate(self, entry_id: str, text: str) -> Entry | None:
        """Appends one paragraph `(YYYY-MM-DD) text` (full-width parentheses in Chinese notes; local date from
        self.clock()) to entry.notes."""
        text = re.sub(r"(?m)^#", r"\\#", text.strip())  # a heading line would split the entry on reload
        if not text:
            return None
        async with self._entry_lock(entry_id):
            entry = self.library.get(entry_id) if await self.locate(entry_id) else None
            if entry is None:
                return None
            paragraph = text_in(_note_lang(entry), "note.comment_line", date=f"{self.clock():%Y-%m-%d}", text=text)
            entry.notes = f"{entry.notes.strip()}\n\n{paragraph}" if entry.notes.strip() else paragraph
            path = self.library.save(entry)
            await self.index.upsert(entry, path)
            return entry

    async def delete(self, entry_id: str, keep_if: Callable[[], bool] | None = None) -> bool | None:
        """None: kept because keep_if (asked under the entry lock, so no merge lands in between) said so."""
        async with self._entry_lock(entry_id):
            if await self.locate(entry_id) and keep_if is not None and keep_if():
                return None
            if not self.library.delete(entry_id):
                return False
            self.index.delete(entry_id)
            self.index.bury(entry_id)  # a copy the user made never brings it back
            self._trash_attachments(entry_id)
            return True

    async def forget(self, entry_id: str) -> bool:
        """The entry's file was deleted outside Octopus (in Obsidian): drop it from the index only. Its attachments
        stay where they are (a note restored from Obsidian's trash finds them again). False when its file is there
        after all."""
        async with self._entry_lock(entry_id):
            if self.library.find(entry_id) is not None:
                return False
            self.index.delete(entry_id)
            return True

    def _trash_replaced(self, entry_id: str, old: str, body: str) -> None:
        """Files the replaced text linked and the new body does not (a picture post read again from the page): to
        the trash, so a later merge never takes them for new ones (P36 fix round 1, m-3). Never raises."""
        try:
            for m in ATTACH_LINE_RE.finditer(old):
                folder, _, name = m.group(1).partition("/")
                src = self.media.attachments_dir / entry_id / name if self.media is not None else None
                if folder != entry_id or src is None or not src.is_file() or re.search(
                        _attach_re(entry_id, name), body, re.M):
                    continue
                dest = self.library.trash / f"{entry_id}-replaced"
                dest.mkdir(parents=True, exist_ok=True)
                target, n = dest / name, 1
                while target.exists():
                    target, n = dest / f"{n}-{name}", n + 1
                shutil.move(str(src), target)
        except Exception:
            log.exception("could not move the replaced files of %s to the trash", entry_id)

    def _trash_attachments(self, entry_id: str) -> None:
        """Files leave attachments/ with the entry, so a retried job cannot resurrect it from them."""
        if self.media is None:
            return
        src = self.media.attachments_dir / entry_id
        if not src.is_dir():
            return
        self.library.trash.mkdir(parents=True, exist_ok=True)
        dest = self.library.trash / f"{entry_id}-attachments"
        n = 1
        while dest.exists():
            dest = self.library.trash / f"{entry_id}-attachments-{n}"
            n += 1
        shutil.move(str(src), dest)
