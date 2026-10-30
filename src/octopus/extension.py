"""Saves sent by the Chrome extension (P17): a page, a selection or an image, each run as a normal save job.

Each request is its own "user" of channel extension (ext-<request id>): the job's replies come back to the extension
channel, which hands them to Intake.reply (forwarded to the Telegram owner when the request asked for it). What the
popup lists is read from the request's row and its job's status, so it survives a restart."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

from octopus.app import App
from octopus.channels.base import log_preview, user_tag
from octopus.config import EXTENSION
from octopus.fetchers import http
from octopus.fetchers.base import FetchError
from octopus.fetchers.web import page_link
from octopus.formatting import format_duplicate
from octopus.i18n import aliases, t
from octopus.library import READ_ERRORS
from octopus.models import Attachment, attachment_to_dict
from octopus.urls import host_of, normalize_url, parsable

log = logging.getLogger(__name__)
KINDS = ("page", "selection", "image")
MAX_RECENT = 10  # what the popup lists
KEEP_ROWS = 200
MAX_TITLE = 200
MAX_IMAGE_BYTES = 15 * 1024 * 1024
IMAGE_DOWNLOAD_SECONDS = 15  # the whole download, before the 202: well inside the worker's ~30 s fetch limit
SUMMARY_CHARS = 80
REASON_CHARS = 60
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/heic": ".heic", "image/bmp": ".bmp", "image/tiff": ".tiff"}
# the states of a save as the popup before P25 matched them; kept on the wire for it, beside the code
PENDING, DONE, FAILED = "处理中", "已存", "失败"  # zh: wire
CODES = {PENDING: "processing", DONE: "saved", FAILED: "failed"}  # what the localized popup reads (P25)


def code_of(item: dict) -> str:
    """processing / saved / incomplete / failed: a /recent item's state without words."""
    return "incomplete" if item["state"] == DONE and item.get("incomplete") else CODES[item["state"]]
# the reasons are short and plain: shown in the popup. A Telegram copy's hint: the merge window is the extension
# request's, not the Telegram chat's, so /add is the way
__getattr__ = aliases({
    "BAD_KIND": "extension.bad_kind", "NOT_A_WEB_PAGE": "extension.not_a_web_page",
    "NO_SELECTION": "extension.no_selection", "NO_IMAGE": "extension.no_image", "BAD_IMAGE": "extension.bad_image",
    "NOT_AN_IMAGE": "extension.not_an_image", "IMAGE_FAILED": "extension.image_failed",
    "IMAGE_TOO_SLOW": "extension.image_too_slow", "ADD_HINT": "extension.add_hint", "SAVE_ERROR": "extension.save_error",
    "NOTHING_READ": "extension.nothing_read", "INCOMPLETE": "extension.incomplete", "ENTRY_GONE": "extension.entry_gone",
    "LOST": "extension.lost", "JOB_FAILED": "extension.job_failed",
})
_USER = re.compile(r"ext-(\d{1,18})")
_REPR = re.compile(r"\w+\((['\"])(.*)\1\)", re.S)  # MediaError: the attachment file is gone
_SPACES = re.compile(r"\s+")
_ENTRY_ID = re.compile(r"^id(?:：|: )(\d{8}-[0-9a-f]{6})$", re.M)  # a reply's id line, either language  # zh: input


@dataclass
class SaveRequest:
    kind: str  # page / selection / image
    url: str | None  # the page's link, normalized; None when the page is not a web page (selection, image only)
    title: str
    html: str | None = None
    text: str | None = None
    image: bytes | None = None
    image_mime: str | None = None
    image_url: str | None = None  # the extension could not read the image itself: downloaded here
    telegram: bool = False


def _web_link(value) -> str | None:
    if not isinstance(value, str) or not parsable(value.strip()) \
            or urlsplit(value.strip()).scheme.lower() not in ("http", "https"):
        return None
    return normalize_url(value, trim=False)  # the tab's exact link: a final "." or ")" is part of it


def parse_request(body: bytes) -> SaveRequest:
    """POST /save's JSON body as a SaveRequest; ValueError with a short Chinese reason when it is not one."""
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(t("extension.not_json")) from None
    if not isinstance(data, dict) or data.get("kind") not in KINDS:
        raise ValueError(t("extension.bad_kind"))
    kind, url = data["kind"], _web_link(data.get("url"))
    title = data.get("title")
    title = _SPACES.sub(" ", title).strip()[:MAX_TITLE] if isinstance(title, str) else ""
    req = SaveRequest(kind, url, title, telegram=data.get("telegram") is True)
    if kind == "page":
        if url is None:
            raise ValueError(t("extension.not_a_web_page"))
        html = data.get("html")
        req.html = html if isinstance(html, str) and html.strip() else None
    elif kind == "selection":
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(t("extension.no_selection"))
        req.text = text.strip()
    else:
        image = data.get("image")
        if isinstance(image, dict):
            mime = str(image.get("mime") or "").split(";")[0].strip().lower()
            if mime not in IMAGE_TYPES or not isinstance(image.get("data"), str):
                raise ValueError(t("extension.bad_image"))
            try:
                req.image = base64.b64decode(image["data"], validate=True)
            except (binascii.Error, ValueError):
                raise ValueError(t("extension.bad_image")) from None
            if not req.image:
                raise ValueError(t("extension.bad_image"))
            req.image_mime = mime
        elif (src := _web_link(data.get("image_url"))) is not None:
            req.image_url = src
        else:
            raise ValueError(t("extension.no_image"))
    return req


def user_of(request_id: int) -> str:
    return f"ext-{request_id}"


def request_of(user_id: str) -> int | None:
    m = _USER.fullmatch(user_id)
    return int(m.group(1)) if m else None


def short_reason(error: str | None) -> str:
    """A failed job's last_error as the popup shows it: the message without the exception's name, cut short."""
    text = (error or "").strip()
    if m := _REPR.fullmatch(text):
        text = m.group(2)
    text = _SPACES.sub(" ", text)
    return text if len(text) <= REASON_CHARS else text[:REASON_CHARS] + "…"


class ExtensionSaves:
    """One row per request: which job saves it, or the entry it found already saved, or why it failed at once."""

    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time):
        self.conn = conn
        self.clock = clock

    def add(self, kind: str, url: str | None, title: str, telegram: bool) -> int:
        rid = self.conn.execute(
            "INSERT INTO extension_saves(kind, url, title, telegram, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, url, title, int(telegram), self.clock())).lastrowid
        self.conn.execute("DELETE FROM extension_saves WHERE id <= ?", (rid - KEEP_ROWS,))
        # what App keeps per user (ext-<id>) goes with the request's row: nobody reads it after the save
        for table in ("focus", "pending_manual"):
            self.conn.execute(f"DELETE FROM {table} WHERE channel = ? AND user_id NOT IN "
                              "(SELECT 'ext-' || id FROM extension_saves)", (EXTENSION,))
        return rid

    def set(self, rid: int, *, job_id: int | None = None, entry_id: str | None = None,
            error: str | None = None) -> None:
        self.conn.execute("UPDATE extension_saves SET job_id = ?, entry_id = ?, error = ? WHERE id = ?",
                          (job_id, entry_id, error, rid))

    def get(self, rid: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM extension_saves WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None

    def latest(self, n: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.*, j.status AS job_status, j.entry_id AS job_entry_id, j.last_error AS job_error "
            "FROM extension_saves s LEFT JOIN jobs j ON j.id = s.job_id ORDER BY s.id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]


class Intake:
    def __init__(self, app: App):
        self.app = app
        self.saves = ExtensionSaves(app.conn)
        self._told_no_telegram = False

    # ---- POST /save ----

    async def save(self, req: SaveRequest) -> int:
        """Queues the save and returns the request id at once; the job does the rest."""
        rid = self.saves.add(req.kind, req.url, req.title, req.telegram)
        user, outcome = user_of(rid), "error"
        try:
            outcome = await self._save(rid, user, req)
        except Exception:
            log.exception("extension save #%d failed", rid)
            self.saves.set(rid, error=t("extension.save_error"))
        finally:
            self._log_in(rid, user, req, outcome)
        self.app.wake.set()
        return rid

    async def _save(self, rid: int, user: str, req: SaveRequest) -> str:
        jobs = self.app.jobs
        if req.kind == "selection":  # a note with its page as the source: never a question, never chat routing
            job = jobs.enqueue("note", {"text": req.text, "url": req.url, "title": req.title}, EXTENSION, user)
            self.saves.set(rid, job_id=job)
            return "note"
        if req.kind == "image":
            try:
                path = await self._stage_image(req)
            except TimeoutError:
                self.saves.set(rid, error=t("extension.image_failed", reason=t("extension.image_too_slow")))
                return "image failed"
            except (FetchError, OSError, ValueError) as e:
                self.saves.set(rid, error=t("extension.image_failed", reason=short_reason(str(e))))
                return "image failed"
            files = [attachment_to_dict(Attachment("image", path, path.name, req.image_mime))]
            job = jobs.enqueue("attachments", {"files": files, "caption": "", "url": req.url,
                                               "entry_id": self.app.pipeline.reserve_entry_id()}, EXTENSION, user)
            self.saves.set(rid, job_id=job)
            return "image"
        page = None
        if req.html is not None and self.app.pipeline.reads_page(req.url):
            page = await self._stage(req.html, "page.html")
        extra = {"page": str(page), "title": req.title} if page is not None else {}
        # the page's canonical / og:url: a partial entry saved under exactly that link is filled (P36 fix round 2)
        canonical = await asyncio.to_thread(page_link, req.html, req.url) if page is not None else None
        what, job, entry = await self.app.queue_url(req.url, EXTENSION, user, extra, canonical=canonical)
        if page is not None and what not in ("queued", "refetch"):
            self._drop(page)
        if what == "duplicate":
            self.saves.set(rid, entry_id=entry.id)
            await self.app.send(EXTENSION, user, format_duplicate(entry.id, entry.title))
            return what
        self.saves.set(rid, job_id=job)
        if what == "in progress":
            await self.app.send(EXTENSION, user, t("app.in_progress"))
            return what
        if what == "queued":  # page: read from the HTML; url: saved as its link, as if sent in chat
            return "page" if page is not None else "url"
        return "refetch page" if page is not None else "refetch"

    async def _stage(self, data: bytes | str, name: str) -> Path:
        """A file under the incoming dir, as the chat channels stage theirs (its own folder); off the event loop."""
        folder = self.app.cfg.incoming_dir / f"ext-{uuid.uuid4().hex}"

        def write() -> Path:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            (folder / name).write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
            return folder / name

        try:
            return await asyncio.to_thread(write)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    async def _stage_image(self, req: SaveRequest) -> Path:
        if req.image is not None:
            return await self._stage(req.image, "image" + IMAGE_TYPES[req.image_mime])
        # the thread cannot be stopped: past the limit it finishes on its own and its bytes are dropped
        r = await asyncio.wait_for(asyncio.to_thread(http.get, req.image_url, max_bytes=MAX_IMAGE_BYTES),
                                   IMAGE_DOWNLOAD_SECONDS)
        mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in IMAGE_TYPES:
            raise ValueError(t("extension.not_an_image"))
        req.image_mime = mime
        name = PurePosixPath(urlsplit(req.image_url).path).name
        suffix = IMAGE_TYPES[mime]
        return await self._stage(r.content, name if name.lower().endswith(suffix) and len(name) <= 100
                                 and name.isprintable() else "image" + suffix)

    @staticmethod
    def _drop(page: Path) -> None:
        try:
            page.unlink(missing_ok=True)
            page.parent.rmdir()
        except OSError:
            log.debug("could not remove %s", page, exc_info=True)

    def _log_in(self, rid: int, user: str, req: SaveRequest, outcome: str) -> None:
        """The P16 line of a request: `in extension <tag> #<id> <kind> <n><chars>|+1<image> [tg] -> <outcome>
        "<preview>"` (the unit words are fixed in Chinese); the page only by its host, a selection by its
        first 20 characters. Never raises."""
        try:
            size = (f"{len(req.text)}字" if req.kind == "selection" else "+1图" if req.kind == "image"  # zh: log
                    else f"{len(req.html or '')}字")
            shown = (f'"{log_preview(req.text)}" ' if req.kind == "selection" else "") + f'"链接 {host_of(req.url)}"'  # zh: log
            log.info("in %s %s #%d %s %s%s -> %s %s", EXTENSION, user_tag(user), rid, req.kind, size,
                     " tg" if req.telegram else "", outcome, shown)
        except Exception:
            log.debug("logging an extension save failed", exc_info=True)

    # ---- the job's replies ----

    def telegram_ready(self) -> bool:
        conf = self.app.cfg.channels.get("telegram")
        return "telegram" in self.app.channels and conf is not None and conf.enabled and bool(conf.allowed_users)

    async def reply(self, user_id: str, text: str) -> None:
        """What the job (or the save itself) says about a request: the popup reads the outcome from the database,
        so only a request with "Also send to Telegram" on goes anywhere: to the Telegram owner, as a chat save would."""
        rid = request_of(user_id)
        row = self.saves.get(rid) if rid is not None else None
        if row is None or not row["telegram"]:
            return
        if not self.telegram_ready():
            if not self._told_no_telegram:  # once: every save would say it again
                log.info("extension: 同时发到 Telegram is on but Telegram is not running here; not sent")  # zh: log
                self._told_no_telegram = True
            return
        merge_ask, supplement_ask = t("formatting.merge_ask"), t("formatting.supplement_ask")
        if (m := _ENTRY_ID.search(text)) and (merge_ask in text or supplement_ask in text):
            hint = t("extension.add_hint", id=m.group(1))
            text = text.replace(merge_ask, hint).replace(supplement_ask, hint)
        for owner in self.app.cfg.channels["telegram"].allowed_users:  # only the owner's own accounts
            await self.app.send("telegram", owner, text)  # held in the outbox when it cannot go now

    # ---- GET /recent ----

    async def recent(self) -> dict:
        """The database on the loop as everywhere else (finding an entry's file may ask the index; a note renamed
        since the last scan is found by one walk in a thread); only the Markdown files are read in a thread."""
        rows = self.saves.latest(MAX_RECENT)
        paths = {i: await self.app.pipeline.locate(i) for r in rows if (i := self._entry_id(r))}
        entries = await asyncio.to_thread(self._read_entries, paths)
        return {"telegram": self.telegram_ready(), "items": [self._item(r, entries) for r in rows]}

    @staticmethod
    def _entry_id(row: dict) -> str | None:
        """The entry a finished request found or saved (none while it is pending or when it failed)."""
        if row["error"] or row["job_status"] in ("queued", "running", "failed"):
            return None
        return row["entry_id"] or row["job_entry_id"]

    def _read_entries(self, paths: dict) -> dict:
        entries = {}
        for entry_id, path in paths.items():
            try:
                entries[entry_id] = self.app.library.load(path) if path else None
            except (*READ_ERRORS, OSError):
                entries[entry_id] = None
        return entries

    def _item(self, row: dict, entries: dict) -> dict:
        item = {"id": row["id"], "kind": row["kind"], "title": row["title"], "url": row["url"],
                "at": row["created_at"], "state": PENDING}
        entry_id = self._entry_id(row)
        if row["error"]:
            item.update(state=FAILED, reason=row["error"])
        elif row["job_status"] in ("queued", "running"):
            pass
        elif row["job_status"] == "failed":
            item.update(state=FAILED, reason=t("extension.job_failed", reason=short_reason(row["job_error"])))
        elif entry_id:
            item["entry_id"] = entry_id
            entry = entries.get(entry_id)
            if entry is None:
                item.update(state=DONE, entry_title="", summary=t("extension.entry_gone"))
            elif entry.status == "needs_manual":
                item.update(state=FAILED, entry_title=entry.title, reason=t("extension.nothing_read"))
            else:
                summary = (entry.summary.strip().splitlines() or [""])[0]
                item.update(state=DONE, entry_title=entry.title, incomplete=entry.status == "partial",
                            summary=summary if len(summary) <= SUMMARY_CHARS else summary[:SUMMARY_CHARS] + "…")
                if item["incomplete"]:
                    item["reason"] = t("extension.incomplete")
                if row["filled"] == entry_id:  # the page filled an entry saved earlier, its id kept (P36)
                    item["filled"] = t("extension.filled_partial" if item["incomplete"] else "extension.filled")
        elif row["job_id"] is not None:
            item.update(state=FAILED, reason=t("extension.lost"))
        item["code"] = code_of(item)
        return item
