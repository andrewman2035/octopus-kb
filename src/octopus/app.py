"""Message handling and the background save worker."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import time
import unicodedata
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path

from octopus import manage, qa
from octopus.actions import Actions
from octopus.backup import backup_db, prune
from octopus.channels.base import (Button, Channel, Reply, SendResult, log_preview, out_line, send_status,
                                   user_tag)
from octopus.commands import ALIASES, TAPPABLE, Commands, ambiguous_block, read_parts
from octopus.config import EXTENSION, Config
from octopus.conversations import Conversations
from octopus.fetchers import FetcherRegistry, needs_browser
from octopus.fetchers.base import FetchError
from octopus.fetchers.web import looks_walled
from octopus.focus import Focus
from octopus.i18n import aliases, t
from octopus.formatting import ack, format_answer, format_duplicate, format_job_failed, format_saved
from octopus.index import Index
from octopus.jobs import Job, JobQueue
from octopus.library import ENTRY_ID_RE, READ_ERRORS, Entry, Library, note_filename
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.manage import Intent
from octopus.media import MediaError, MediaTools
from octopus.models import IncomingMessage, attachment_from_dict, attachment_to_dict
from octopus.ops import Ops
from octopus.outbox import Outbox
from octopus.pending import ADD, AUTO, FILES_WINDOW_SECONDS, FRESH, LINK, MERGE_WINDOW_SECONDS, NEW, PendingManual
from octopus.pipeline import SaveOutcome, SavePipeline, attachment_kind, fillable
from octopus.router import (ASK_PREFIX, LONG_PASTE_CHARS, TRAILING_PARTICLES, Route, RouteKind, about_saving_them,
                            classify, words)
from octopus.settings import Settings
from octopus.sync import SCAN_INTERVAL_SECONDS, LibrarySync
from octopus.urls import TRAILING, extract_urls, host_of, normalize_url, without_urls

log = logging.getLogger(__name__)
LOCAL_CHANNELS = {"cli"}
APP_COMMANDS = {"new", "add", "done", "read", "split"}  # handled by App itself, the rest by Commands (cmd_<name>)
BACKFILL_INTERVAL_SECONDS = 600
OUTBOX_SWEEP_SECONDS = 60  # held replies (e.g. over ClawBot's send budget) go out by themselves this often
SWEEP_BACKOFF_SECONDS = 60  # a user the sweep could not reach is skipped this long, doubling each time
SWEEP_BACKOFF_MAX_SECONDS = 480
INCOMING_MAX_AGE_SECONDS = 24 * 3600  # staged files untouched this long and needed by no job are swept at start
PASTE_MIN_CHARS = 80  # a "question" this long (or multi-line) inside the merge window is pasted original text
ADD_ARGS = re.compile(r"\s*/\S+\s+([^\s，,：:]+)[ \t]*(?:[，,：:]\s*)?")  # /add <token>[，：] <text>  # zh: input
# "done" (好了 …) / /done: an /add, /new, auto or failed link's window takes nothing more
DONE_WORDS = {"好了", "好啦", "完成", "完成了", "结束了", "done", "finished"}  # zh: input
DONE_PHRASES = {"alldone", "imdone", "thatsall", "thatsit"}  # "all done", "that's it": its words without spaces
INSTRUCTION_MAX_CHARS = 40
# 图 ("picture") also matches 图片 (picture) and 截图 (screenshot)
_PICTURES = re.compile(r"图|照片|\b(?:images?|pictures?|photos?|screenshots?|pics?)\b", re.I)  # zh: input
# 存 ("save") also matches 保存; 整理一下要点 ("sort out the key points") is a request, not this
_SAVE_VERBS = re.compile(r"存|合成|合并|拼|整理成|\b(?:save|keep|combine|merge)\b", re.I)  # zh: input
_ONE_PIECE = re.compile(r"一篇|一条|\b(?:one|single) (?:note|entry|article|post)\b|\b(?:as|into) one\b", re.I)  # zh: input
_ASKING = re.compile(r"什么|怎么|为什么|哪|谁|多少|意思|吗|[？?]|\b(?:what|how|why|who|which|where|when)\b", re.I)  # zh: input
# something to do with the images: for the answer model
_OTHER_ASK = re.compile(r"总结|翻译|解释|分析|\b(?:summari[sz]e|translate|explain|analy[sz]e|describe)\b", re.I)  # zh: input
_JUST_SAVE = re.compile(r"(?:帮我|请|麻烦你?)?(?:把|将)?(?:这些|上面的?|刚才的?|刚刚的?|前面的?)?(?:都|全部)?"
                        r"(?:(?:合成|合并成?|存成|整理成|拼成)(?:一篇|一条)(?:文章|笔记)?|保存一下|存一下|保存)[吧啊呀哈]?")  # zh: input
MERGE_WAIT_SECONDS = 5  # a merge into an entry whose first files are still being saved tries again this soon
SPLIT_BATCH_SECONDS = 120  # merges this close after each other are one batch: the screenshots of one article
# a bare word answering a confirmation, compared in lower case; 確認/確定 ("confirm" in traditional
# characters): Whisper may hear a one-word clip as Japanese and skip the conversion to simplified. 存 / 问 ("save" /
# "ask") stay corrections, so neither is in a set
CONFIRM_WORDS = {"确认", "确定", "是", "是的", "好", "好的", "对", "可以", "执行", "删", "删吧", "删掉", "删除", "确认删除", "行", "行的", "嗯", "嗯嗯",
                 "好滴", "没问题", "可以的", "确定的", "ok", "okay", "yes", "y", "確認", "確定",
                 "confirm", "confirmed", "sure", "yep", "yeah", "yup", "delete", "remove"}  # zh: input
CANCEL_WORDS = {"取消", "不", "不用", "不要", "算了", "别", "不了", "不用了", "no", "n", "cancel", "nope", "nah", "stop"}  # zh: input
# a yes only to a delete: after "summarize again …?" (确定重新总结…？) a 删 ("delete") asks for something else
DELETE_ONLY_WORDS = {"删", "删吧", "删掉", "删除", "确认删除", "delete", "remove"}  # zh: input
MAX_OP_ID_DIGITS = 18
SHORT_REPLY_CHARS = 4  # a reply this short to a confirmation is an answer we did not understand, not a question
# a "query" that only means "lately"
RECENT_WORDS = {"最近", "最新", "近期", "最近的", "最近存的", "全部", "所有", "都"}  # zh: input
FILLER = re.compile(r"存|的|了|过|什么|东西|哪些|内容|\s")  # "最近存了什么" ("what did I save lately") says no more than "最近" ("lately")  # zh: input
# the English words a "lately" query is made of ("what did I save recently", "my latest notes")
VAGUE_WORDS = {"what", "did", "i", "have", "has", "saved", "save", "saves", "stuff", "things", "thing", "items",
               "item", "the", "a", "an", "my", "me", "all", "everything", "anything", "any", "lately", "recently",
               "recent", "latest", "new", "newest", "entries", "entry", "notes", "note", "records", "record", "show",
               "list", "last", "few", "some", "of", "from", "in", "this", "week", "weeks", "month", "today",
               "yesterday", "day", "days"}
_TRAILING_MARKS = re.compile(r"[\W_]+$")
MAX_LISTED = 10
PART_PAUSE_SECONDS = 1.0  # between /read parts on channels without a send budget (Telegram's flood limit)
# /read <id> <word>: every part at once, or the whole note as a file (P35)
READ_WORDS = {"全部": "all", "all": "all", "文件": "file", "file": "file"}  # zh: input
LONG_READ_PARTS = 10  # an entry with more parts says so under its first batch, next to the file button
MAX_READ_PART_DIGITS = 6
READ_TAP_REPEAT_SECONDS = 5.0  # the same /read button tapped again this soon is a double tap: skipped

__getattr__ = aliases({  # the old constant names, in the configured language
    "ERROR_REPLY": "app.error_reply", "LINKS_UNREAD": "app.links_unread", "FIX_TO_QUESTION": "app.fix_to_question",
    "SAVE_CHAT": "app.save_chat", "VOICE_UNCLEAR": "app.voice_unclear", "NO_CORRECTION": "app.no_correction",
    "ADD_USAGE": "app.add_usage", "ADD_OPEN": "app.add_open", "NEW_DONE": "app.new_done", "FILES_SAVED": "app.files_saved",
    "FILES_SAVING": "app.files_saving", "SPLIT_LABEL": "app.split_label", "SPLIT_NONE": "app.split_none",
    "ANSWER_PLEASE": "app.answer_please", "UNREADABLE": "app.unreadable", "NOTE_CHANGED": "app.note_changed",
    "FIND_NONE": "app.find_none", "FIND_FALLBACK": "app.find_fallback", "MISSING_ID": "app.missing_id",
    "AT_DEFAULT_QUESTION": "app.at_default_question", "AT_TOO_MANY": lambda: t("app.at_too_many", max=qa.MAX_PINNED),
    "IN_PROGRESS": "app.in_progress", "FETCH_RETRYING": "app.fetch_retrying", "ADD_NOT_FOUND": "common.not_found",
    "WINDOW_DONE": "app.window_done", "NO_WINDOW": "app.no_window", "SPLIT_HINT": "app.split_hint",
    "OP_EXPIRED": "app.op_expired", "OP_CANCELLED": "app.op_cancelled", "OP_REASK": "app.op_reask",
    "UNREADABLE_TITLE": "app.unreadable_title", "NOTE_GREW": "app.note_grew", "NOT_FOUND": "app.not_found",
    "AMBIGUOUS": "app.ambiguous", "ASK_TITLE": "app.ask_title", "ASK_NOTE": "app.ask_note",
})


def typed_answer(text: str) -> tuple[bool, bool] | None:
    """A message that only says yes or no to a confirmation, typed or transcribed ("Yes.", "OK!", "yes, delete
    it", or the Chinese words): (confirmed, only a yes to a delete), else None. Punctuation, emoji, a trailing
    Chinese particle and case (OK) are ignored."""
    if text.lstrip().startswith(("/", "\uff0f")):  # a command (/delete with a delete asked) is never a yes or no
        return None
    parts = [p.lower() for p in words(text)]
    if not parts:
        return None
    said = []
    for part in parts:
        if part not in CONFIRM_WORDS | CANCEL_WORDS:
            part = part.rstrip(TRAILING_PARTICLES)
        if part not in CONFIRM_WORDS | CANCEL_WORDS:
            return None
        said.append(part)
    if all(w in CONFIRM_WORDS for w in said):
        return True, any(w in DELETE_ONLY_WORDS for w in said)
    if all(w in CANCEL_WORDS for w in said):
        return False, False
    return None  # "OK, never mind" (yes and no at once): unclear, so not an answer


def done_word(text: str) -> bool:
    """A "done" word (DONE_WORDS, DONE_PHRASES) when that is all the message says, typed or transcribed ("Done.",
    with a trailing particle), also said twice ("done, done"); the same normalisation as a typed confirmation."""
    parts = [p.rstrip(TRAILING_PARTICLES).lower() for p in words(text)]
    if "".join(parts) in DONE_PHRASES:
        return True
    return 0 < len(parts) <= 2 and all(p in DONE_WORDS or any(p == w * 2 for w in DONE_WORDS) for p in parts)


def about_files_sent(text: str) -> bool:
    """A short instruction to save the images just sent ("save the screenshots above as one note", "keep these",
    "combine them into one", or the same in Chinese): an image word with a save verb, or with a "one note" word and
    no question; without an image word only a whole "combine them into one" / "save it"-style sentence (_JUST_SAVE).
    Never a request to do something else with them (summarize, translate …). Deterministic: no model call. Longer
    texts and texts with a link are never one."""
    said = text.strip()
    longest = INSTRUCTION_MAX_CHARS if not said.isascii() else 2 * INSTRUCTION_MAX_CHARS  # English words are longer
    if not said or len(said) > longest or extract_urls(said) or _OTHER_ASK.search(said):
        return False
    if _PICTURES.search(said):
        return bool(_SAVE_VERBS.search(said)) or (bool(_ONE_PIECE.search(said)) and not _ASKING.search(said))
    return bool(_JUST_SAVE.fullmatch("".join(words(said)))) or about_saving_them(said)


class _NotYet(Exception):
    """A merge job whose entry is still being saved by an earlier job: run it again shortly."""


class _IndexWithout:
    """The index as qa.answer sees it, minus one entry (a note being turned into that very question)."""

    def __init__(self, index: Index, entry_id: str):
        self.index = index
        self.entry_id = entry_id

    async def search(self, query: str, k: int = 8):
        hits = await self.index.search(query, k + 4)
        return [h for h in hits if h.entry_id != self.entry_id][:k]


class App:
    def __init__(self, cfg: Config, *, conn: sqlite3.Connection, library: Library, index: Index,
                 llm: LLMRouter, registry: FetcherRegistry, channels: dict[str, Channel],
                 media: MediaTools | None = None):
        self.cfg = cfg
        self.conn = conn
        self.library = library
        self.index = index
        self.llm = llm
        self.media = media
        self.channels = channels
        self.settings = Settings(conn)
        self.convos = Conversations(conn)
        self.jobs = JobQueue(conn)
        self.pipeline = SavePipeline(library, index, llm, registry, media=media)
        self.pipeline.summary_language = cfg.summary_language
        self.pending = PendingManual(conn)
        self.outbox = Outbox(conn)
        self.actions = Actions(conn)
        self.focus = Focus(conn)
        self.ops = Ops(conn)
        # per user: the op whose confirmation was the last thing sent, the only one a typed confirmation may run
        self._awaiting: dict[tuple[str, str], int] = {}
        self._user_locks: dict[tuple[str, str], asyncio.Lock] = {}  # one message at a time per user
        self.commands = Commands(llm, self.settings, library, index, self.jobs, self.pipeline, cfg.data_dir)
        self.wake = asyncio.Event()
        self.backfill_interval: float = BACKFILL_INTERVAL_SECONDS
        self.library_sync = LibrarySync(self.pipeline, notify=self.notify_owner)  # edits made in Obsidian (P18)
        self.library_scan_interval: float = SCAN_INTERVAL_SECONDS
        self.outbox_sweep_interval: float = OUTBOX_SWEEP_SECONDS
        self.tick: float = 1.0  # the worker's longest wait between rounds
        self.monotonic: Callable[[], float] = time.monotonic  # the sweep backoff's clock, injectable for tests
        self._read_taps: dict[tuple[str, str], tuple[str, float]] = {}  # each user's last /read button and when
        self._sweep_backoff: dict[tuple[str, str], tuple[float, float]] = {}  # user -> (skip until, last delay)
        self.today: Callable[[], date] = date.today  # injectable for tests
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # the pause between /read parts
        self.commands.today = lambda: self.today()  # /list without a date: today by the same clock

    def close(self) -> None:
        self.conn.close()

    def allowed(self, msg: IncomingMessage) -> bool:
        if msg.channel in LOCAL_CHANNELS:
            return True
        ch = self.cfg.channels.get(msg.channel)
        if not ch or not ch.enabled:
            return False
        if msg.user_id in ch.allowed_users:
            return True
        obj = self.channels.get(msg.channel)
        is_owner = getattr(obj, "is_owner", None)
        if is_owner is None:
            return False
        try:
            return bool(is_owner(msg.user_id))
        except Exception:
            log.exception("owner check on %s failed", msg.channel)
            return False

    async def _deliver(self, channel: str, user_id: str, text: str, buttons: list[Button] | None = None,
                       *, quiet: bool = False, resend: bool = False, plain: bool = False) -> SendResult:
        """quiet: a retry the sweep repeats every minute, failures only at debug level. resend: from the outbox.
        Each reply gets one out line (channels.base.send_status): ok, held(<reason>) (the channel did not try, e.g.
        ClawBot's send budget) or failed (the send failed, or no such channel); either way App keeps it in the outbox.
        The line can never change what happens."""
        ch = self.channels.get(channel)
        result, status = SendResult(False), "failed"
        if ch is None:
            log.log(logging.DEBUG if quiet else logging.ERROR, "no channel %s for reply to %s", channel,
                    user_tag(user_id))
        else:
            try:
                result = await ch.send(user_id, Reply(text, buttons or [], plain))
                status = send_status(result)
            except Exception:
                if quiet:
                    log.debug("send via %s failed", channel, exc_info=True)
                else:
                    log.exception("send via %s failed", channel)
        try:
            log.log(logging.DEBUG if quiet and not result.ok else logging.INFO,
                    out_line(channel, user_id, text, status, buttons=len(buttons or []), resend=resend))
        except Exception:
            log.debug("logging a reply failed", exc_info=True)
        return result

    async def send(self, channel: str, user_id: str, text: str, buttons: list[Button] | None = None,
                   *, op_id: int | None = None, plain: bool = False) -> SendResult:
        """A reply that cannot be delivered goes to the outbox and is re-sent when the user next writes or by
        the sweep. op_id: the text asks to confirm that op (held, it is never delivered once the op is stale)."""
        # any reply but the confirmation itself (which sets it again right after): a typed "OK" now answers that
        self._awaiting.pop((channel, user_id), None)
        result = await self._deliver(channel, user_id, text, buttons, plain=plain)
        if not result.ok:  # re-sent later without buttons: keep the typed hints (reply "yes" to go ahead …) with it
            self.outbox.add(channel, user_id, "\n".join([text] + [b.fallback for b in buttons or [] if b.fallback]),
                            op_id=op_id)
        return result

    async def _flush_outbox(self, channel: str, user_id: str, *, answering: bool) -> bool:
        """Queued replies one by one, oldest first, never joined (a joined text could exceed ClawBot's send
        budget and lose its tail). Stops at the first failure: it and the newer ones stay queued as they are,
        keeping their order and original age. The caller holds the user's lock. False: something is still held.

        A held confirmation whose op is no longer the user's live one (done, expired, or a newer one asked) is
        dropped, never delivered. answering: flushed while handling an incoming message. Not for the sweep: a
        confirmation it delivers is answered by the user's next typed yes."""
        for entry_id, text, op_id in self.outbox.queued(channel, user_id):
            if op_id is not None:
                live = self.ops.latest(channel, user_id)
                if live is None or live["id"] != op_id:
                    self.outbox.remove(entry_id)
                    continue
            if not (await self._deliver(channel, user_id, text, quiet=not answering, resend=True)).ok:
                return False
            self.outbox.remove(entry_id)
            if answering:  # the user only sees it now: the message being handled cannot answer a held confirmation
                self._awaiting.pop((channel, user_id), None)
        return True

    async def sweep_outbox(self) -> None:
        """One pass: each user's held replies, under that user's lock (never in between a message being
        handled). What still cannot go out stays queued for the next pass or message; never raises."""
        try:
            users = self.outbox.pending_users()
        except Exception:
            log.debug("reading the outbox failed", exc_info=True)
            return
        for channel, user_id in users:
            key = (channel, user_id)
            if channel not in self.channels:  # not running now: kept for when it is
                continue
            skip_until, delay = self._sweep_backoff.get(key, (0.0, 0.0))
            if self.monotonic() < skip_until:  # could not reach them lately: no retry every minute
                continue
            async with self._user_locks.setdefault(key, asyncio.Lock()):
                try:
                    delivered = await self._flush_outbox(channel, user_id, answering=False)
                except Exception:
                    log.debug("outbox sweep for %s:%s failed", channel, user_tag(user_id), exc_info=True)
                    delivered = False
            if delivered:
                self._sweep_backoff.pop(key, None)
            else:
                delay = min(delay * 2, SWEEP_BACKOFF_MAX_SECONDS) if delay else SWEEP_BACKOFF_SECONDS
                self._sweep_backoff[key] = (self.monotonic() + delay, delay)
                log.debug("outbox sweep: %s:%s not reachable, next try in %.0f s", channel, user_tag(user_id),
                          delay)

    async def notify_owner(self, text: str, image: Path | None = None, exclude_channel: str | None = None) -> int:
        """System notices (e.g. ClawBot re-login QR) to the owner on the other enabled channels.

        Recipients are those channels' allow-listed users, which must be only the owner's own accounts;
        exclude_channel is the channel the notice is about (it usually cannot deliver it)."""
        delivered = 0
        for name, ch in self.channels.items():
            conf = self.cfg.channels.get(name)
            if name == exclude_channel or not conf or not conf.enabled:
                continue
            # allowed_users must contain only the owner's own accounts: they all get system notices
            for user_id in conf.allowed_users:
                send_image = getattr(ch, "send_image", None)
                try:
                    if image is not None and send_image is not None:
                        result = await send_image(user_id, image, text)
                    else:
                        result = await ch.send(user_id, Reply(text))
                except Exception:
                    log.exception("notifying %s:%s failed", name, user_tag(user_id))
                    continue
                delivered += 1 if result.ok else 0
        return delivered

    def _discard_staged(self, msg: IncomingMessage) -> None:
        """Delete files a channel already staged for a message nobody allowed (only under incoming_dir)."""
        root = self.cfg.incoming_dir.resolve()
        for a in msg.attachments:
            if a.path is None:
                continue
            try:
                path = a.path.resolve()
                if not path.is_relative_to(root) or path == root:
                    continue
                path.unlink(missing_ok=True)
                if path.parent != root:
                    path.parent.rmdir()  # the per-message staging dir, only if now empty
            except OSError:
                log.info("could not discard staged file %s", a.path, exc_info=True)

    async def handle(self, msg: IncomingMessage) -> None:
        if not self.allowed(msg):
            log.warning("in %s %s ignored: not allowed", msg.channel, user_tag(msg.user_id))
            self._discard_staged(msg)
            return
        key = (msg.channel, msg.user_id)
        self._sweep_backoff.pop(key, None)  # they just wrote: worth trying again (ClawBot can reply now)
        lock = self._user_locks.setdefault(key, asyncio.Lock())
        async with lock:  # a long answer delays that user's next message: replies stay in order
            try:
                await self._flush_outbox(msg.channel, msg.user_id, answering=True)
                awaited = self._awaiting.get(key)
                if awaited is not None and self.outbox.holds_op(msg.channel, msg.user_id, awaited):
                    self._awaiting.pop(key, None)  # its question is still held: this message cannot answer it
                await self._dispatch(msg)
            except Exception:
                log.exception("handling message from %s:%s failed", msg.channel, user_tag(msg.user_id))
                await self.send(msg.channel, msg.user_id, t("app.error_reply"))

    def _log_in(self, msg: IncomingMessage, kind: str, outcome: str = "") -> None:
        """The one line an incoming message gets, once its route is decided and before it is acted on:
        what it was taken as, with the window open when it came. Never the user id or more than a preview, and
        never raises: a log line must not change what happens."""
        try:
            self._write_in(msg, kind, outcome)
        except Exception:
            log.debug("logging an incoming message failed", exc_info=True)

    def _write_in(self, msg: IncomingMessage, kind: str, outcome: str) -> None:
        who = f"{msg.channel} {user_tag(msg.user_id)}"
        if msg.callback:
            log.info("in %s tap/%s", who, log_preview(msg.callback))
            return
        files = msg.attachments
        origin = self.pending.origin(msg.channel, msg.user_id)
        shown = f"+{len(files)}{'图' if self._images_only(msg) else '文件'}" if files else ""  # zh: log
        fields = [kind, f"{len(msg.text)}字", shown, "quote" if msg.reply_to_id else "",  # zh: log
                  f"window={origin}" if origin else "",
                  f"-> {outcome}" if outcome else "", f'"{log_preview(msg.text)}"' if msg.text.strip() else ""]
        log.info("in %s %s", who, " ".join(f for f in fields if f))

    @staticmethod
    def _log_then(msg: IncomingMessage, outcome: str) -> None:
        """What became of a message, when that answers "why didn't it …" (nothing to correct, a window closed)."""
        try:
            log.info("in %s %s -> %s", msg.channel, user_tag(msg.user_id), outcome)
        except Exception:
            log.debug("logging an outcome failed", exc_info=True)

    async def _dispatch(self, msg: IncomingMessage) -> None:
        if msg.callback:
            self._log_in(msg, "tap")
            await self._callback(msg)
            return
        key = (msg.channel, msg.user_id)
        answer = None if msg.attachments else typed_answer(msg.text)
        awaited = self._awaiting.get(key)
        live = self.ops.latest(msg.channel, msg.user_id)
        if answer is not None:
            confirmed, delete_only = answer
            fits = live is not None and (not delete_only or live["kind"] == "delete")
            said = f"answer/{'确认' if confirmed else '取消'}"  # zh: log
            if awaited is not None and (live is None or live["id"] != awaited):
                self._log_in(msg, said, "expired")
                await self._op_reply(msg, confirmed, t("app.op_expired"))  # expired: say so, never let the QA model guess
                return
            if awaited is not None and fits:
                self._log_in(msg, said)
                await self._finish_op(msg, live["id"], confirmed)
                return
            if awaited is None and fits and self._asked_last(msg, live):
                # the confirmation is still the last chat turn, but another reply (a finished save, a held
                # message) came after it: the user may not have read it, so ask again rather than run it. Not
                # for the QA model either: it would see the question as the last turn and might claim it was done
                self._log_in(msg, said, "asked again" if confirmed else "")
                if confirmed:
                    await self._reask(msg, live)
                else:  # cancelling needs no second look
                    await self._finish_op(msg, live["id"], False)
                return
        route = classify(msg)
        if route is None:
            self._log_in(msg, "nothing")
            return
        if route.command == "add" and not msg.text.lstrip().startswith("/"):
            # the add alias (the Chinese word for "add" + id) is free text: unless its token names a record it is
            # just a sentence
            eid, matches = self.library.resolve(route.args[0])
            if eid is None and not matches:
                route = classify(msg, alias=False)
                if route is None:
                    self._log_in(msg, "nothing")
                    return
        if ((route.kind is RouteKind.SAVE_URLS or route.pasted) and not msg.attachments
                and manage.at_ids(msg.text.strip())):
            route = Route(RouteKind.QUESTION, text=msg.text.strip())  # "@b305ad what is https://… in it": asked about
        if (route.kind is RouteKind.SAVE_URLS and not msg.attachments and msg.reply_to_id
                and self.pending.for_reply(msg.channel, msg.user_id, msg.reply_to_id) and words(without_urls(route.text))):
            # a quote-reply to a prompt with more than the link: material for that entry, its link kept as text
            route = Route(RouteKind.SAVE_NOTE, text=route.text, urls=route.urls, pasted=True)
        elif route.kind is RouteKind.SAVE_URLS and not msg.attachments and self._window_excerpt(msg, route):
            route = Route(RouteKind.SAVE_NOTE, text=route.text, urls=route.urls, pasted=True)
        if (route.kind is RouteKind.QUESTION and not msg.attachments and done_word(msg.text)
                and self.pending.origin(*key) is not None):
            route = Route(RouteKind.COMMAND, text=msg.text, command="done")  # a bare "done" is chat with no window open
        # "@b305ad what does it say": always a question about those entries (never material, a confirmation or a
        # management request)
        at = manage.at_ids(route.text) if route.kind is RouteKind.QUESTION else None
        if (route.kind is RouteKind.QUESTION and answer is None and awaited is not None and live is not None
                and live["id"] == awaited and not ASK_PREFIX.match(msg.text.strip()) and at is None
                and len("".join(words(msg.text))) <= SHORT_REPLY_CHARS):
            self._log_in(msg, "answer/unclear")
            await self._ask(msg, live["id"], t("app.answer_please"))  # "uh-huh" / "👍": not for the model to guess at
            return
        if route.kind is not RouteKind.VOICE:  # a spoken yes comes back here as text
            self._awaiting.pop(key, None)  # anything else said since: a later "OK" is not an answer to it
        if (route.kind is RouteKind.QUESTION and not ASK_PREFIX.match(msg.text.strip()) and at is None
                and not (msg.reply_to_id and self.pending.for_reply(msg.channel, msg.user_id, msg.reply_to_id))
                and (reply := self._files_sent_reply(msg, route.text))):  # a quote-reply to a prompt: material
            self._log_in(msg, route.kind.value, "files already saved")
            self.pending.clear(msg.channel, msg.user_id)  # it still ends the window
            await self.send(msg.channel, msg.user_id, reply)
            return
        if answer is None and at is None and self._first_text(msg, route):
            # an /add or /new window's one text: merged like a paste into a failed link's window; right after /new
            # it starts the new entry as the first files would. The window stays open for more files
            self._log_in(msg, route.kind.value, "first_text")
            target = self._merge_target(msg)
            self._enqueue_content(msg, route.text, target, links=bool(route.urls), start=target is None)
            self.pending.mark_text_added(msg.channel, msg.user_id)  # after: starting the entry reopens the window
            await self._ack(msg)
            return
        kind = route.kind.value
        if route.kind is RouteKind.COMMAND:  # an unknown "/…" is often a /s with no space, a link or a path
            name = ALIASES.get(route.command, route.command)
            known = route.command in APP_COMMANDS or hasattr(self.commands, f"cmd_{name}")
            kind = f"command/{route.command if known else 'unknown'}"
        elif route.kind is RouteKind.CORRECTION:
            kind = f"correction/{route.text}"
        elif at is not None:
            kind = "question/@id"
        self._log_in(msg, kind)
        if self._ends_window(msg, route):
            self.pending.clear(msg.channel, msg.user_id)
        if route.kind is RouteKind.CORRECTION:
            await self._correct(msg, route.text)
        elif route.kind is RouteKind.VOICE:
            await self._voice(msg)
        elif route.kind is RouteKind.GREETING:  # the guide: no model call, nothing saved, not a chat turn
            await self._command(msg, "help", [])
        elif route.kind is RouteKind.COMMAND:
            await self._command(msg, route.command, route.args)
        elif route.kind is RouteKind.QUESTION:
            if at is not None:
                await self._at_answer(msg, route.text, *at)
                return
            asked = ASK_PREFIX.match(msg.text.strip()) is not None
            pasted = self._looks_pasted(msg, route.text)
            # a quote-reply to a needs_manual prompt is material for that entry, however short
            quoted = not asked and bool(msg.reply_to_id) and bool(
                self.pending.for_reply(msg.channel, msg.user_id, msg.reply_to_id))
            if (pasted or quoted) and (target := self._merge_target(msg)):
                self._log_then(msg, "material for the window's entry, not a question")
                self._enqueue_content(msg, route.text, target)
                await self._ack(msg)
                return
            # "ask:" is always a plain question; long pastes and questions without a management word skip the lookups
            long_paste = len(route.text.strip()) >= LONG_PASTE_CHARS
            if not asked and not long_paste and manage.wants_manage(route.text, self.library.resolve):
                intent = await manage.detect(self.llm, route.text, self.convos.recent(msg.channel, msg.user_id),
                                             self._focus_pair(msg), self._recent_pairs(), self.today(),
                                             self.library.resolve)
                if intent is not None and intent.kind not in ("answer", "save") and intent.ambiguous:
                    # a token that is not clear yet: nothing is searched or confirmed until every one is
                    await self.send(msg.channel, msg.user_id, "\n".join(
                        ambiguous_block(self.library, token, ids) for token, ids in intent.ambiguous))
                    return
                if intent is not None and intent.kind != "answer":
                    await self._manage(msg, route.text, intent)
                    return
            await self._answer(msg, route.text)
        elif route.kind is RouteKind.SAVE_URLS:
            if not self._is_window_link(msg, route.urls):  # another link starts something new; so do its files
                self.pending.clear(msg.channel, msg.user_id)
            queued = await self._enqueue_urls(msg, route.urls)
            if msg.attachments:  # the files sent along with the link are saved too
                self._enqueue_content(msg, route.text, self._merge_target(msg))
            if queued or msg.attachments:
                await self._ack(msg)
        elif route.kind is RouteKind.SAVE_NOTE:
            # /s, "save:": always a new note, never merged into a window's entry; pasted material with links joins a
            # failed link's window (or a quoted prompt's entry) like pasted text without links does
            target = self._merge_target(msg) if route.pasted else None
            self._enqueue_content(msg, route.text, target, links=bool(route.urls))
            await self._ack(msg)
        elif route.kind is RouteKind.SAVE_ATTACHMENTS:
            self._enqueue_content(msg, route.text, self._merge_target(msg), group=True, links=bool(route.urls))
            await self._ack(msg)

    @staticmethod
    def _images_only(msg: IncomingMessage) -> bool:
        """Images as the pipeline sees them: a screenshot sent as a file (.png …) is one too."""
        return bool(msg.attachments) and all(attachment_kind(a) == "image" for a in msg.attachments)

    def _window_gone(self, msg: IncomingMessage) -> bool:
        """The open window points at an entry that is not there and never will be (deleted, or its first
        files failed for good)."""
        target = self.pending.active(msg.channel, msg.user_id)
        return bool(target) and not self.library.find(target) and not self.jobs.creating(
            msg.channel, msg.user_id, target)

    def _ends_window(self, msg: IncomingMessage, route: Route) -> bool:
        """A /new, /add or auto window collects only the files sent one after another: anything else said ends
        it before it is handled. Not a voice message (its words come back here as text), /add (it switches),
        "save separately" (it moves the window) or a link (cleared with the links: the window's own link keeps
        it). An auto window takes only images: any other file ends it. A failed link's window stays through chat:
        it still waits for screenshots and pasted text."""
        origin = self.pending.origin(msg.channel, msg.user_id)
        if route.kind is RouteKind.SAVE_ATTACHMENTS:
            return origin == AUTO and not self._images_only(msg)
        if route.kind in (RouteKind.VOICE, RouteKind.SAVE_URLS):
            return False
        if route.kind is RouteKind.COMMAND and route.command in ("add", "split", "done"):
            return False
        return origin in (ADD, NEW, AUTO)

    def _first_text(self, msg: IncomingMessage, route: Route) -> bool:
        """The first plain text (typed, pasted, a voice transcript; a long paste with its links kept) in an /add or
        /new window, which takes one. Never an "ask:" / @id question, a quote-reply to a prompt
        (material for that entry, as before), a "save the images above" (about_files_sent) or a window whose entry
        is gone; the caller has ruled out a yes / no word."""
        user = (msg.channel, msg.user_id)
        if msg.attachments or not (route.kind is RouteKind.QUESTION or route.kind is RouteKind.SAVE_NOTE
                                   and route.pasted):
            return False
        if ASK_PREFIX.match(msg.text.strip()) or about_files_sent(route.text):
            return False
        if self.pending.origin(*user) not in (ADD, NEW) or self.pending.text_added(*user):
            return False
        if msg.reply_to_id and self.pending.for_reply(*user, msg.reply_to_id):
            return False
        return self.pending.fresh(*user) or self._merge_target(msg) is not None

    def _files_sent_reply(self, msg: IncomingMessage, text: str) -> str | None:
        """The reply to a short "save the images above" (see about_files_sent) while a /new, /add or auto window
        holds files the user just sent: they are one entry already, so nothing new is saved."""
        user = (msg.channel, msg.user_id)
        target = self.pending.active(*user)
        origin = self.pending.origin(*user)
        if not target or origin not in (ADD, NEW, AUTO) or not about_files_sent(text):
            return None
        # /add points at an entry that existed before: only files sent into this window count. A /new window (or one
        # a "save separately" moved) may hold only its text: its files are the ones being saved with it or already in it
        sent = self.jobs.window_files(*user, target, self.pending.opened_at(*user) - FILES_WINDOW_SECONDS)
        if origin == ADD and not sent:
            return None
        if origin == NEW and not (sent or self.jobs.creating(*user, target, files=True)
                                  or self._has_attachments(target)):
            return None
        if self.library.find(target):
            entry = self._entry(target)
            return t("app.files_saved", title=entry.title, id=target) if entry else None
        return t("app.files_saving") if self.jobs.creating(*user, target) else None

    def _has_attachments(self, entry_id: str) -> bool:
        """The entry has files of its own in its attachments folder."""
        root = self.media.attachments_dir if self.media else self.cfg.attachments_dir
        folder = root / entry_id
        return folder.is_dir() and any(folder.iterdir())

    async def _voice(self, msg: IncomingMessage) -> None:
        """Hold-to-talk voice: transcribe, then handle the words as if typed."""
        parts = []
        try:
            for a in msg.attachments:
                if a.text and a.text.strip():  # e.g. WeChat's own transcript
                    parts.append(a.text.strip())
                elif a.path is not None and self.media is not None:
                    # through the pipeline's heavy lock: one Whisper run at a time, also against save jobs
                    parts.append((await self.pipeline.heavy_call(self.media.transcribe, a.path)).text.strip())
        except Exception:
            log.exception("voice transcription failed")
            parts = []
        finally:
            self._discard_staged(msg)  # the recording itself is not kept
        text = "\n".join(p for p in parts if p)
        if not text:
            await self.send(msg.channel, msg.user_id, t("app.voice_unclear"))
            return
        await self._dispatch(IncomingMessage(channel=msg.channel, user_id=msg.user_id, text=text,
                                             message_id=msg.message_id, reply_to_id=msg.reply_to_id))

    def _fix_button(self, kind: str, action_id: int) -> Button:
        if kind == "note":
            return Button(t("app.fix_to_question"), f"fix:{action_id}", t("app.fix_to_question_hint"))
        return Button(t("app.save_chat"), f"fix:{action_id}", t("app.save_chat_hint"))

    async def _apply_correction(self, msg: IncomingMessage, action: dict | None, why: str = "") -> None:
        """why: the reason action is None, for the log."""
        if action is None:
            self._log_then(msg, f"nothing to correct ({why})" if why else "nothing to correct")
            await self.send(msg.channel, msg.user_id, t("app.no_correction"))
            return
        if action["kind"] == "note":
            if problem := self._note_changed(action):  # notes or a new title would go to the trash with it
                await self.send(msg.channel, msg.user_id, problem)
                return
            # answer first (without citing the note itself): if the models are down, the note stays
            if not await self._answer(msg, action["text"], without=action["entry_id"], fail_suffix=t("app.note_kept")):
                return
            self.actions.consume(action["id"])
            # checked again under the entry lock: a merge may have landed while the answer was written
            if action["entry_id"] and await self.pipeline.delete(
                    action["entry_id"], keep_if=lambda: self._note_changed(action) is not None) is None:
                await self.send(msg.channel, msg.user_id, self._note_changed(action) or t("app.note_grew"))
        else:  # "Save this": always a new note, never merged into an entry waiting for screenshots
            self.actions.consume(action["id"])
            self._enqueue_content(msg, action["text"], None, from_chat=True)
            await self._ack(msg)

    def _note_changed(self, action: dict) -> str | None:
        """Why a saved note may no longer be turned into a question: changed since it was saved."""
        entry_id = action["entry_id"]
        if not entry_id or not self.library.find(entry_id):
            return None  # already deleted: only the answer is left to give
        entry = self._entry(entry_id)
        if entry is None:
            return t("app.unreadable") + entry_id
        if entry.notes.strip() or (action.get("title") is not None and entry.title != action["title"]):
            return t("app.note_changed")
        if entry.body.split() != action["text"].split():  # more added since (files after a /new text); spacing aside
            return t("app.note_grew")
        return None

    async def _command(self, msg: IncomingMessage, name: str, args: list[str]) -> None:
        if name == "new":  # commands that need the user are App's own: Commands never sees them
            self.pending.set(msg.channel, msg.user_id, FRESH, origin=NEW)  # the next files start one new entry
            await self.send(msg.channel, msg.user_id, t("app.new_done"))
            return
        if name == "add":
            await self._add(msg, args)
            return
        if name == "done":  # only stops further merging: what is being saved goes on
            had = self.pending.origin(msg.channel, msg.user_id)
            self._log_then(msg, f"window {had} closed by done" if had else "done: no window open")
            self.pending.clear(msg.channel, msg.user_id)
            await self.send(msg.channel, msg.user_id, t("app.window_done" if had else "app.no_window"))
            return
        if name == "read":
            await self._read(msg, args)
            return
        if name == "split":  # typed "separate": the latest batch of merges, while its window could still be open
            latest = self.actions.latest(msg.channel, msg.user_id, MERGE_WINDOW_SECONDS, kinds=("merge",))
            if latest is None:
                await self.send(msg.channel, msg.user_id, t("app.split_none"))
                return
            await self._split(msg, latest, earlier=True)
            return
        reply = await self.commands.reply(name, args)
        await self.send(msg.channel, msg.user_id, reply.text, reply.buttons)

    async def _read(self, msg: IncomingMessage, args: list[str]) -> None:
        """/read [id] [K | all | file]: the entry's full text from part K on, a batch of read_batch_parts at a time
        (no id: the current entry; K needs an id); "all": every part; "file": the note as a Markdown file, else in
        parts with why. It becomes the current entry. No model call."""
        user = (msg.channel, msg.user_id)
        first, mode = 1, ""
        if len(args) == 1 and unicodedata.normalize("NFKC", args[0]).lower() in READ_WORDS:  # /read file: this one
            mode, args = READ_WORDS[unicodedata.normalize("NFKC", args[0]).lower()], []
        elif len(args) == 2:
            k = unicodedata.normalize("NFKC", args[1]).lower()  # full-width digits
            mode = READ_WORDS.get(k, "")
            first = 1 if mode else int(k) if k.isascii() and k.isdecimal() else 0
        if len(args) > 2 or first <= 0:
            await self.send(*user, t("commands.read_usage"))
            return
        if args:
            entry_id, matches = self.library.resolve(args[0].lower())
            if matches:
                await self.send(*user, ambiguous_block(self.library, args[0], matches))
                return
            if entry_id is None:
                await self.send(*user, t("commands.read_not_found", token=args[0]))
                return
        else:  # as for "this one": a quote-reply means the quoted prompt's entry (none for other messages), else the
            # current entry; one deleted since counts as none
            entry_id = (self.pending.for_reply(*user, msg.reply_to_id) if msg.reply_to_id
                        else self.focus.get(*user))
            if entry_id is None or not self.library.find(entry_id):
                await self.send(*user, t("commands.read_usage"))
                return
        try:
            entry = self.library.get(entry_id)
        except (*READ_ERRORS, OSError) as e:
            log.warning("could not read entry %s: %s", entry_id, e)
            await self.send(*user, t("app.unreadable") + entry_id)
            return
        if entry is None:  # moved away since it was resolved
            await self.send(*user, t("commands.read_gone"))
            return
        self.focus.set(*user, entry.id)
        notice = ""
        if mode == "file":
            notice = await self._send_note_file(msg, entry)
            if not notice:
                return
        parts = read_parts(entry)
        if first > len(parts):
            await self.send(*user, t("commands.read_total", n=len(parts)))
            return
        await self._send_parts(msg, entry.id, parts, first, batch=None if mode == "all" else self.cfg.read_batch_parts,
                               notice=notice)

    async def _send_note_file(self, msg: IncomingMessage, entry: Entry) -> str:
        """The note's Markdown file as stored (frontmatter and all) as <title>-<short id>.md: "" once sent, else
        why not (the parts follow instead). ClawBot cannot send files: iLink has no file upload here."""
        send_file = getattr(self.channels.get(msg.channel), "send_file", None)
        if send_file is None:
            return t("commands.read_no_file_wechat" if msg.channel == "clawbot" else "commands.read_no_file")
        path = self.library.find(entry.id)
        try:
            data = path.read_bytes() if path else b""
        except OSError as e:
            log.warning("could not read entry %s for its file: %s", entry.id, e)
            data = b""
        if not data:
            return t("commands.read_file_failed")
        result = SendResult(False, reason="error")
        try:
            result = await send_file(msg.user_id, data, note_filename(entry))
        except Exception:
            log.exception("sending the file of %s via %s failed", entry.id, msg.channel)
        log.info("out %s %s file %d bytes %s", msg.channel, user_tag(msg.user_id), len(data), send_status(result))
        if result.ok:
            return ""
        return t("commands.read_file_too_big" if result.reason == "too-big" else "commands.read_file_failed")

    @staticmethod
    def _read_buttons(entry_id: str, k: int, n: int, files: bool) -> list[Button]:
        """Under the last part of a batch with part k next: "Continue (k/n)" and, where files can be sent, "Send as
        a file"; their typed fallbacks are /read <id> k and /read <id> file. The state is all in the data
        (read:<id>:<k|file>, at most 27 bytes), so a button on an old batch still works."""
        buttons = [Button(t("commands.read_continue", k=k, n=n), f"read:{entry_id}:{k}",
                          t("commands.read_more", left=n - k + 1, id=entry_id, next=k))]
        if files:
            buttons.append(Button(t("commands.read_file_button"), f"read:{entry_id}:file",
                                  t("commands.read_file_hint", id=entry_id)))
        return buttons

    async def _send_parts(self, msg: IncomingMessage, entry_id: str, parts: list[str], first: int, *,
                          batch: int | None, notice: str = "") -> None:
        """Parts first.. in order, one message each: at most batch of them (None: all), and no more than the channel
        may send now (ClawBot: its budget, via the optional sends_left; others PART_PAUSE_SECONDS apart). notice
        heads the first one. The last one sent carries the continue / file buttons (their typed hints where there
        are no buttons); under the first batch of a long entry it also says so. A part that cannot be delivered is
        held in the outbox and nothing after it is sent; without the hints it is held with them."""
        n = len(parts)
        ch = self.channels.get(msg.channel)
        sends_left = getattr(ch, "sends_left", None)
        files = getattr(ch, "send_file", None) is not None and not notice  # no file offered right after one failed
        stop = n if batch is None else min(n, first + batch - 1)
        for k in range(first, stop + 1):
            if k > first and sends_left is None:
                await self.sleep(PART_PAUSE_SECONDS)
            text = (t("commands.read_part_mark", k=k, n=n) if n > 1 else "") + parts[k - 1]
            if k == first and notice:
                text = f"{notice}\n\n{text}"
            last = k == stop or (sends_left is not None and sends_left(msg.user_id) <= 1)
            buttons = self._read_buttons(entry_id, k + 1, n, files) if k < n else []
            if last and buttons and first == 1 and n > LONG_READ_PARTS and files:
                text = f"{text}\n\n{t('commands.read_long', n=n)}"
            shown = buttons if last else []
            if shown and sends_left is not None:  # ClawBot: no buttons; the typed hint a blank line below, as before
                text, shown = f"{text}\n\n" + "\n".join(b.fallback for b in shown), []
            if not (await self.send(msg.channel, msg.user_id, text, shown, plain=True)).ok:
                if not last:
                    self.outbox.add(msg.channel, msg.user_id, "\n".join(b.fallback for b in buttons))
                return
            if last:
                return

    async def _add(self, msg: IncomingMessage, args: list[str]) -> None:
        """/add <id> [text]: open the merge window on an entry; with text (or files) merge it right away."""
        user = (msg.channel, msg.user_id)
        if not args:
            rows = self.index.recent(10)
            lines = [f"{r['id']}  {r['title']}" for r in rows]
            usage = t("app.add_usage")
            await self.send(*user, "\n".join([usage, *lines]) if lines else f"{usage}\n{t('commands.nothing_saved')}")
            self._discard_staged(msg)
            return
        slash = msg.text.lstrip().startswith("/")
        # the router split on whitespace only: "/add 6e43" plus a full-width comma and text names 6e43 too, and the
        # text is taken as typed
        m = ADD_ARGS.match(msg.text) if slash else None
        token = (m.group(1) if m else args[0]).lower()
        entry_id, matches = self.library.resolve(token)
        entry = self.library.get(entry_id) if entry_id else None
        if entry is None:  # not found, ambiguous, or an unreadable file: nothing is saved
            await self.send(*user, ambiguous_block(self.library, token, matches) if matches
                            else t("common.not_found", token=token))
            self._discard_staged(msg)
            return
        self.pending.set(msg.channel, msg.user_id, entry_id, origin=ADD)
        self.focus.set(msg.channel, msg.user_id, entry_id)
        if slash:
            text = msg.text[m.end():].strip() if m else ""
        else:  # the add alias hands the text over as one argument
            text = " ".join(args[1:]).strip()
        if text or msg.attachments:  # links in the text are kept as text, never read
            if text:  # this window's one text: the next one is chat
                self.pending.mark_text_added(msg.channel, msg.user_id)
            self._enqueue_content(msg, text, entry_id, explicit=True, links=bool(extract_urls(text)))
            await self._ack(msg)
            return
        await self.send(*user, t("app.add_open", title=entry.title, id=entry_id))

    async def _callback(self, msg: IncomingMessage) -> None:
        prefix, _, rest = msg.callback.partition(":")
        if prefix == "cmd":  # cmd:<name>[ <arg>] from the guide: only the few commands meant to be tapped
            name, _, arg = rest.partition(" ")
            if arg in TAPPABLE.get(name, ()):
                await self._command(msg, name, [arg] if arg else [])
            return
        if prefix == "del":  # del:<id> from the /delete picker: asks for a confirmation first, never deletes directly
            if rest.isascii() and ENTRY_ID_RE.fullmatch(rest):
                reply = await self._confirm(msg, "delete", [rest], "")
                self.convos.add(msg.channel, msg.user_id, "user", t("app.said_delete", id=rest))  # a chat turn, as if typed
                self.convos.add(msg.channel, msg.user_id, "assistant", reply)
            return
        if prefix == "read":  # read:<id>:<k|file> under a /read batch: as if /read <id> <k|file> were typed
            entry_id, _, arg = rest.partition(":")
            if (entry_id.isascii() and ENTRY_ID_RE.fullmatch(entry_id) and arg.isascii()
                    and (arg == "file" or (arg.isdecimal() and len(arg) <= MAX_READ_PART_DIGITS))):
                # the buttons stay after a tap (Telegram and Discord have no dedupe): a double tap is one. The window
                # runs from when the last one was done: the second tap waited for its batch (the user's lock)
                key, now = (msg.channel, msg.user_id), self.monotonic()
                last_tap = self._read_taps.get(key)
                self._read_taps[key] = (msg.callback, now)
                if last_tap is not None and last_tap[0] == msg.callback and now - last_tap[1] < READ_TAP_REPEAT_SECONDS:
                    self._log_then(msg, "repeated read tap skipped")
                    return
                try:
                    await self._read(msg, [entry_id, arg])
                finally:
                    self._read_taps[key] = (msg.callback, self.monotonic())
            return
        if prefix == "op":  # op:<id>:yes|no
            op_id, _, answer = rest.partition(":")
            if (op_id.isascii() and op_id.isdecimal() and len(op_id) <= MAX_OP_ID_DIGITS
                    and answer in ("yes", "no")):
                await self._finish_op(msg, int(op_id), answer == "yes")
            return
        if prefix not in ("fix", "split") or not (rest.isascii() and rest.isdecimal()
                                                   and len(rest) <= MAX_OP_ID_DIGITS):
            return
        action = self.actions.get(int(rest), msg.channel, msg.user_id)
        if prefix == "split":  # split:<id> from a merge reply: that merge and the later ones of its batch
            if action is None or action["kind"] != "merge":
                await self.send(msg.channel, msg.user_id, t("app.split_none"))
                return
            await self._split(msg, action, earlier=False)
            return
        fits = action is not None and action["kind"] in ("note", "question")
        await self._apply_correction(msg, action if fits else None, "stale-button")

    def _merge_batch(self, msg: IncomingMessage, action: dict, *, earlier: bool) -> list[dict]:
        """action and the window merges into its entry chained to it, each recorded at most SPLIT_BATCH_SECONDS
        after the one before: the later ones, and with earlier the ones before it too; oldest first. A merge
        aimed at the entry on purpose (/add, a quote-reply) joins no batch, and its own batch is itself."""
        if json.loads(action["text"]).get("via") != "window":
            return [action]
        rows = [r for r in self.actions.of_entry(msg.channel, msg.user_id, "merge", action["entry_id"])
                if r["id"] == action["id"] or json.loads(r["text"]).get("via") == "window"]
        ids = [r["id"] for r in rows]
        if action["id"] not in ids:
            return [action]
        start = end = ids.index(action["id"])

        def close(i: int) -> bool:  # rows[i] came soon after rows[i - 1]
            return rows[i]["created_at"] - rows[i - 1]["created_at"] <= SPLIT_BATCH_SECONDS

        while end + 1 < len(rows) and close(end + 1):
            end += 1
        while earlier and start > 0 and close(start):
            start -= 1
        return rows[start:end + 1]

    async def _split(self, msg: IncomingMessage, anchor: dict, *, earlier: bool) -> None:
        """Save separately: the merged sections of anchor's batch (from it on; earlier: the ones before it too)
        leave their entry and become a new one, which the merge window and the focus move to (the rest of that article's
        screenshots join it). The batch is chosen under the entry lock: a merge that was running when the user
        tapped is recorded by then and goes along."""
        old_id = anchor["entry_id"]
        user = (msg.channel, msg.user_id)
        batch: list[dict] = []

        def sections() -> list[tuple]:
            current = self.actions.get(anchor["id"], msg.channel, msg.user_id)  # not consumed meanwhile
            batch[:] = self._merge_batch(msg, current, earlier=earlier) if current else []
            return [(d["section"], d["files"], d.get("notes", [])) for d in (json.loads(a["text"]) for a in batch)]

        out = await self.pipeline.split(old_id, sections, self.pipeline.reserve_entry_id(), msg.channel)
        window = self.pending.active(msg.channel, msg.user_id)
        # an old button (the article is long finished) must not take over a window open on something else
        recent = bool(batch) and self.actions.clock() - batch[-1]["created_at"] <= MERGE_WINDOW_SECONDS
        move = out is not None and (recent or window in (None, old_id))
        if move and (first_job := json.loads(batch[0]["text"]).get("job")) is not None:
            # no await since split released the lock: a merge job waiting for it already sees the new target
            self.jobs.redirect_merges(msg.channel, msg.user_id, old_id, out.entry.id, since_job=first_job)
        for a in batch or [anchor]:  # done, nothing left to move or the entry gone: the buttons stop either way
            self.actions.consume(a["id"])
        if out is None:
            await self.send(*user, t("app.split_none"))
            return
        new, old = out.entry, self._entry(old_id)
        lines = [t("app.split_done", title=new.title), new.summary, out.note, t("common.label", label="id", value=new.id),
                 t("app.split_restored", title=old.title) if old else ""]
        await self.send(*user, "\n".join(line for line in lines if line))
        if move:  # not a prompt: a quoted question on this reply is a question. The user's own files, not a
            # failed link: like a /new entry, the files sent next join it and anything else ends the window (it
            # takes no text: a quoted "delete this" on the split reply is chat)
            self.pending.set(msg.channel, msg.user_id, new.id, origin=NEW)
            self.pending.mark_text_added(msg.channel, msg.user_id)
        self.focus.set(msg.channel, msg.user_id, new.id)

    async def _correct(self, msg: IncomingMessage, word: str) -> None:
        latest = self.actions.latest(msg.channel, msg.user_id)
        wanted = "note" if word == "问" else "question"  # zh: input (the router's word)
        why = "no-latest" if latest is None else "kind-mismatch"
        if latest and latest["kind"] == "question" and not self._answered_last(msg, latest):
            # a listing or a confirmation came after that answer: "save" would save the wrong thing
            latest, why = None, "not-last-turn"
        await self._apply_correction(msg, latest if latest and latest["kind"] == wanted else None, why)

    def _answered_last(self, msg: IncomingMessage, action: dict) -> bool:
        """The latest chat turn is the answer this "Save this" action would save."""
        turns = self.convos.recent(msg.channel, msg.user_id, 1)
        if len(turns) != 2 or [r for r, _ in turns] != ["user", "assistant"]:
            return False
        (_, question), (_, answer) = turns
        return action["text"].startswith(t("app.chat_note", question=question, answer=answer))

    @staticmethod
    def _looks_pasted(msg: IncomingMessage, text: str) -> bool:
        """Pasted original text often contains question words; an explicit "ask:" is always a question."""
        return not ASK_PREFIX.match(msg.text.strip()) and (len(text) >= PASTE_MIN_CHARS or "\n" in text)

    def _enqueue_content(self, msg: IncomingMessage, text: str, target: str | None, from_chat: bool = False,
                         explicit: bool = False, group: bool = False, links: bool = False,
                         start: bool = False) -> None:
        """Queue the message's text and files: merged into target, else a new entry. from_chat: a "Save this" of an
        answer, never meant as a question, so its note gets no "Make it a question". explicit: target named by the user
        (/add), so a "save separately" of the window's entry never redirects it; neither does a quote-reply's.
        group: images sent with no window open start an auto window (the images sent right after them join their entry).
        links: the text has links that are not read, which the saved reply says (LINKS_UNREAD). start: the first text
        after /new, which starts the new entry as the first files would."""
        files = [attachment_to_dict(a) for a in msg.attachments]
        unread = {"links_unread": True} if links else {}
        if target:
            # fallback_id: where the content goes if the entry is deleted before the job runs (fixed up front
            # so a retry of the job stays idempotent)
            quoted = bool(msg.reply_to_id) and self.pending.for_reply(
                msg.channel, msg.user_id, msg.reply_to_id) == target
            window = not explicit and not quoted and target == self.pending.active(msg.channel, msg.user_id)
            if window and self.pending.origin(msg.channel, msg.user_id) != LINK:
                self.pending.touch(msg.channel, msg.user_id)  # the 5 idle minutes count from the latest file
            self.jobs.enqueue("merge", {"entry_id": target, "files": files, "text": text,
                                        "fallback_id": self.pipeline.reserve_entry_id(),
                                        **({"via": "window"} if window else {}), **unread},
                              msg.channel, msg.user_id)
        elif files or start:
            entry_id = self.pipeline.reserve_entry_id()
            self.jobs.enqueue("attachments", {"files": files, "caption": text, "entry_id": entry_id, **unread},
                              msg.channel, msg.user_id)
            if self.pending.fresh(msg.channel, msg.user_id):  # the first files after /new: the rest join them
                self.pending.set(msg.channel, msg.user_id, entry_id, origin=NEW)
            elif group and self._images_only(msg) and (self.pending.opened_at(msg.channel, msg.user_id) is None
                                                        or self._window_gone(msg)):
                self.pending.set(msg.channel, msg.user_id, entry_id, origin=AUTO)  # like /new, without the /new
        else:
            self.jobs.enqueue("note", {"text": text, "from_chat": True} if from_chat else {"text": text, **unread},
                              msg.channel, msg.user_id)

    def _merge_target(self, msg: IncomingMessage) -> str | None:
        target = None
        if msg.reply_to_id:
            target = self.pending.for_reply(msg.channel, msg.user_id, msg.reply_to_id)
        target = target or self.pending.active(msg.channel, msg.user_id)
        if not target:
            return None
        # after /new the window's entry may still be on its way in (its attachments job not done yet)
        # an indexed entry whose file was renamed a moment ago is still the target: the merge job finds it (P18)
        return target if (self.library.find(target) or self.index.path_of(target)
                          or self.jobs.creating(msg.channel, msg.user_id, target)) else None

    async def _ack(self, msg: IncomingMessage) -> None:
        self.wake.set()
        ch = self.channels.get(msg.channel)
        if ch is not None and ch.acks_saves:
            await self.send(msg.channel, msg.user_id, ack())

    def _window_excerpt(self, msg: IncomingMessage, route: Route) -> bool:
        """A failed (or partly read) link's window, and the message has that entry's own link with at least
        PASTE_MIN_CHARS beside it: an excerpt pasted with its link, merged as text. Another link (a share blurb sent
        meanwhile) is read as a new link; "try again\n<link>" (either language) is a retry."""
        win = self.pending.active(msg.channel, msg.user_id)
        return (bool(win) and self.pending.origin(msg.channel, msg.user_id) == LINK
                and len(without_urls(route.text).strip()) >= PASTE_MIN_CHARS
                and any(self.index.find_by_url(normalize_url(u)) == win for u in route.urls))

    def _is_window_link(self, msg: IncomingMessage, urls: list[str]) -> bool:
        """A re-sent link of the entry the window waits on (it will be refetched): the window stays."""
        win = self.pending.active(msg.channel, msg.user_id)
        for raw in urls:
            existing = self.index.find_by_url(normalize_url(raw))
            if win and existing == win and (entry := self.library.get(win)) and entry.status == "needs_manual":
                return True
        return False

    async def _enqueue_urls(self, msg: IncomingMessage, urls: list[str]) -> int:
        """Queues save/refetch jobs; returns how many were queued (the caller acks)."""
        queued = 0
        for raw in urls:
            what, _, entry = await self.queue_url(normalize_url(raw), msg.channel, msg.user_id)
            if what == "in progress":
                await self.send(msg.channel, msg.user_id, t("app.in_progress"))
            elif what == "duplicate":
                await self.send(msg.channel, msg.user_id, format_duplicate(entry.id, entry.title))
                self.focus.set(msg.channel, msg.user_id, entry.id)
            else:
                queued += 1
        return queued

    async def queue_url(self, url: str, channel: str, user_id: str, extra: dict | None = None,
                        canonical: str | None = None) -> tuple[str, int | None, Entry | None]:
        """A normalized link as a save takes it, in chat or from the Chrome extension: ("queued" | "refetch", the job,
        None) when a job will save it, ("in progress", the job already saving it, None), or ("duplicate", None, the
        entry). extra: more for a new job's payload (the extension's page, read instead of fetching). canonical: the
        page's canonical / og:url (P36 fix round 2): an entry saved under exactly that link is filled while it is
        partial or needs_manual; it never makes the page "already saved" (many pages may name one canonical)."""
        existing = self.index.find_by_url(url)
        if existing is None and url.rstrip(TRAILING) != url:  # saved before P19 without its closing ")"
            existing = self.index.find_by_url(url.rstrip(TRAILING))
        by_canonical = existing is None and canonical is not None
        if by_canonical:
            existing = self.index.find_link(canonical)
        # a note renamed in Obsidian since the last scan is found by one walk (in a thread), never taken for stale
        path = await self.pipeline.locate(existing) if existing else None
        entry = self.library.load(path) if path else None
        # no await below: the pending check and the enqueue stay one step on the loop
        if existing and entry is None:  # Markdown was deleted by hand: the index row is stale
            self.index.delete(existing)
            existing = None  # a new link save, not a refetch of the entry that is gone
        elif existing and not fillable(entry, bool(extra and extra.get("page"))):  # a page fills a partial one (P36)
            if not by_canonical:
                return "duplicate", None, entry
            existing = None  # the canonical's entry is complete: this page is an entry of its own
        if pending := self.jobs.pending_url_job(url):
            return "in progress", pending, None
        if existing:
            how = {"by_canonical": True} if by_canonical else {}
            return "refetch", self.jobs.enqueue("refetch", {"entry_id": existing, "url": url, **(extra or {}), **how},
                                                channel, user_id), None
        return "queued", self.jobs.enqueue("url", {"url": url, **(extra or {})}, channel, user_id), None

    async def _at_answer(self, msg: IncomingMessage, text: str, tokens: list[str], question: str) -> None:
        """"@id question": the entries named are the whole material. A token naming none or several entries, a file that
        cannot be read (each problem on its own) or more than MAX_PINNED entries are said instead, no model call."""
        problems, entries = [], {}
        for token in tokens:
            eid, matches = self.library.resolve(token)
            if matches:
                problems.append(ambiguous_block(self.library, token, matches))
            elif eid is None:
                problems.append(t("app.missing_id", id=token))
            elif (entry := self._entry(eid)) is None:
                problems.append(t("app.unreadable") + eid)
            else:
                entries[eid] = entry  # "@b305ad @b305": one entry
        if problems or len(entries) > qa.MAX_PINNED:
            self._log_then(msg, f"@id unresolved ({len(problems)})" if problems else "@id too many")
            await self.send(msg.channel, msg.user_id, "\n".join(problems) or t("app.at_too_many", max=qa.MAX_PINNED))
            return
        if not question:  # "@b305ad" or "@b305ad?": what it is about
            text = f"{_TRAILING_MARKS.sub('', text)} {t('app.at_default_question')}"
        await self._answer(msg, text, only=list(entries.values()))

    async def _answer(self, msg: IncomingMessage, question: str, *, without: str | None = None,
                      fail_suffix: str = "", only: list[Entry] | None = None) -> bool:
        """Returns False when it was not answered (the user was told): no model could, the full ids in it are not
        there and nothing else is named, or a short id in it names several entries. without: an entry not to cite.
        only: the entries an "@id" question is about, all of the material (the question is not searched)."""
        note, pinned = "", only
        if only is None:
            missing = [i for i in dict.fromkeys(manage.FULL_ID_TOKEN.findall(question)) if not self.library.find(i)]
            note = t("app.missing_id", id=t("common.list_sep").join(missing)) if missing else ""
            pinned = self._pinned(msg, question, without)
            if isinstance(pinned, str) or (note and not pinned):  # not there or not clear: say so, no model call
                self._log_then(msg, "ids unresolved, not answered")
                unclear = pinned if isinstance(pinned, str) else ""
                await self.send(msg.channel, msg.user_id, "\n".join(filter(None, [note, unclear])))
                return False
        history = self.convos.recent(msg.channel, msg.user_id)
        index = _IndexWithout(self.index, without) if without else self.index
        try:
            ans = await qa.answer(question, index, self.llm, history, pinned=pinned, search=only is None)
        except LLMError as e:
            await self.send(msg.channel, msg.user_id, t("app.no_model", error=e, suffix=fail_suffix))
            return False
        # the entry the answer was about is the current one now ("this one" next, or "delete the one above")
        about = pinned[0].id if pinned else next((s.entry_id for s in ans.sources), None)
        if about:
            self.focus.set(msg.channel, msg.user_id, about)
        self.convos.add(msg.channel, msg.user_id, "user", question)
        self.convos.add(msg.channel, msg.user_id, "assistant", ans.text)
        # "Save this" saves the user's words and the answer together as one note
        action_id = self.actions.record(msg.channel, msg.user_id, "question",
                                        t("app.chat_note", question=question, answer=format_answer(ans)))
        reply = f"{note}\n{format_answer(ans)}" if note else format_answer(ans)  # the ids that are there are answered
        await self.send(msg.channel, msg.user_id, reply, [self._fix_button("question", action_id)])
        return True

    def _pinned(self, msg: IncomingMessage, question: str, without: str | None) -> list[Entry] | str:
        """The entries a question names, for the answer to read in full: full ids typed in it that are there, short
        ids naming one entry (see manage.question_id_tokens) and titles; else, for a short "what does this one say", a
        short id beside it or the current entry. The ambiguous reply instead when a marked short id names several."""
        typed = list(dict.fromkeys(manage.FULL_ID_TOKEN.findall(question)))
        ids, unclear = [i for i in typed if self.library.find(i)], []
        for token, marked in manage.question_id_tokens(question):
            eid, matches = self.library.resolve(token)
            if eid:
                ids.append(eid)
            elif matches and marked:  # an unmarked one naming several entries is likely a word: ignored
                unclear.append(ambiguous_block(self.library, token, matches))
        if unclear:
            return "\n".join(unclear)
        ids += qa.title_pins(question, self.index.titles())
        if not ids and not typed and qa.points_back(question):
            # "05ad, what does this one say": beside "this one" a short id with a letter (or marked) is meant as one;
            # naming none or several, nothing is pinned (never the current entry: the user named another one).
            # Plain numbers ("the 100 tips this one gives") are not ids
            near = [tok for tok in manage.short_id_tokens(question) if any(c in "abcdefABCDEF" for c in tok)]
            near += [tok for tok, marked in manage.question_id_tokens(question) if marked]
            if near:
                ids = [eid for eid, _ in map(self.library.resolve, near) if eid]
            # on a quote-reply the quoted message's entry, known only for the prompts that asked for material; any
            # other quoted message pins nothing
            elif current := (self.pending.for_reply(msg.channel, msg.user_id, msg.reply_to_id) if msg.reply_to_id
                             else self.focus.get(msg.channel, msg.user_id)):
                ids = [current]
        entries = [e for i in dict.fromkeys(ids) if i != without and (e := self._entry(i))]
        return entries[:qa.MAX_PINNED]

    # ---- record management in chat ----

    def _entry(self, entry_id: str) -> Entry | None:
        """The entry, None when its file is gone or cannot be parsed (e.g. broken by a hand edit in Obsidian)."""
        try:
            return self.library.get(entry_id)
        except (*READ_ERRORS, OSError) as e:
            log.warning("could not read entry %s: %s", entry_id, e)
            return None

    def _targets(self, ids: list[str]) -> list[tuple[str, Entry | None]]:
        """The ids whose file still exists, each with its entry (None: there but unreadable)."""
        return [(i, self._entry(i)) for i in ids if self.library.find(i)]

    def _focus_entry(self, msg: IncomingMessage) -> Entry | None:
        """The current entry, if it still exists and can be read (the focus may name one deleted since)."""
        entry_id = self.focus.get(msg.channel, msg.user_id)
        return self._entry(entry_id) if entry_id else None

    def _focus_pair(self, msg: IncomingMessage) -> tuple[str, str] | None:
        entry = self._focus_entry(msg)
        return (entry.id, entry.title) if entry else None

    def _recent_pairs(self) -> list[tuple[str, str]]:
        return [(r["id"], r["title"]) for r in self.index.recent(MAX_LISTED)]

    async def _search_entries(self, query: str, k: int = 20) -> list[tuple[str, str]]:
        """Entries matching the words of query, best first, one row per entry."""
        found: dict[str, str] = {}
        if not query:
            return []
        for h in await self.index.search(query, k, keyword_only=True):
            found.setdefault(h.entry_id, h.title)
        return list(found.items())

    @staticmethod
    def _lines(pairs: list[tuple[str, str]]) -> str:
        return "\n".join(f"{i}  {title}" for i, title in pairs[:MAX_LISTED])

    async def _manage(self, msg: IncomingMessage, text: str, intent: Intent) -> None:
        if intent.kind == "save":
            # the user's own words: the model's content only when it is a part of them, never a paraphrase
            content = intent.content if intent.content and intent.content in text else text
            self._enqueue_content(msg, content, None)
            await self._ack(msg)
            return
        if intent.kind == "find":
            reply = await self._find(msg, intent)
            await self.send(msg.channel, msg.user_id, reply)
        else:
            reply = await self._request_change(msg, intent)
        # a chat turn like any other: a follow-up ("and the second one?") sees what was said
        self.convos.add(msg.channel, msg.user_id, "user", text)
        self.convos.add(msg.channel, msg.user_id, "assistant", reply)

    async def _find(self, msg: IncomingMessage, intent: Intent) -> str:
        core = FILLER.sub("", intent.query)
        vague = intent.query.isascii() and all(w in VAGUE_WORDS for w in words(intent.query.lower()))
        # "what did I save lately" with "lately" put in the query: no topic at all
        if not core or core in RECENT_WORDS or vague:
            intent.query = ""
        if intent.ids:
            pairs = [(i, e.title if e else t("app.unreadable_title")) for i, e in self._targets(intent.ids)]
        elif intent.since or intent.until:
            rows = self.index.saved_between(intent.since, intent.until, 1000 if intent.query else MAX_LISTED + 1)
            if intent.query:
                wanted = {i for i, _ in await self._search_entries(intent.query, 100)}
                rows = [r for r in rows if r["id"] in wanted]
            pairs = [(r["id"], r["title"]) for r in rows]
        elif intent.query:
            pairs = await self._search_entries(intent.query)
        else:  # "what did I save" with nothing to narrow it down: the latest ones
            pairs = self._recent_pairs()
        if not pairs:
            # "what did I save lately" read as a query or a too-narrow range: the latest ones rather than a bare
            # "nothing found".
            # Not for ids the user named: those are reported as not there.
            recent = [] if intent.ids else self._recent_pairs()
            return f"{t('app.find_fallback')}\n{self._lines(recent)}" if recent else t("app.find_none")
        if len(pairs) == 1:
            self.focus.set(msg.channel, msg.user_id, pairs[0][0])
        more = t("app.found_more") if len(pairs) > MAX_LISTED else ""
        return t("app.found", n=min(len(pairs), MAX_LISTED), lines=self._lines(pairs), more=more)

    async def _request_change(self, msg: IncomingMessage, intent: Intent) -> str:
        """Works out which entries are meant and asks for a confirmation (or says why not); returns what was sent."""
        reply = await self._change_problem(msg, intent)
        if isinstance(reply, str):
            await self.send(msg.channel, msg.user_id, reply)
            return reply
        return await self._confirm(msg, intent.kind, *reply)

    async def _change_problem(self, msg: IncomingMessage, intent: Intent) -> str | tuple[list[str], str]:
        """The reply when the change cannot be asked for yet, else (entry ids, arg)."""
        if intent.ids:
            ids = intent.ids
        elif intent.target == "focus":
            ids = [e.id] if (e := self._focus_entry(msg)) else []
        elif intent.target == "search" or intent.query:
            found = await self._search_entries(intent.query)
            if len(found) > 1:
                return t("app.ambiguous") + self._lines(found)
            ids = [i for i, _ in found]
        else:
            ids = []
        targets = self._targets(ids)
        if not targets:
            return self._with_missing(t("app.not_found"), ids, targets)
        if intent.kind != "delete" and len(targets) > 1:
            return t("app.ambiguous") + self._lines([(i, e.title if e else t("app.unreadable_title")) for i, e in targets])
        if intent.kind != "delete" and targets[0][1] is None:  # a delete moves the file unparsed: fine
            return t("app.unreadable") + targets[0][0]
        arg = {"retitle": intent.title, "annotate": intent.note}.get(intent.kind, "")
        if intent.kind == "retitle" and not arg:
            return t("app.ask_title")
        if intent.kind == "annotate" and not arg:
            return t("app.ask_note")
        return ids, arg

    @staticmethod
    def _with_missing(text: str, ids: list[str], targets: list[tuple[str, Entry | None]]) -> str:
        found = {i for i, _ in targets}
        return "\n".join([text] + [t("common.not_found", token=i) for i in ids if i not in found])

    async def _confirm(self, msg: IncomingMessage, kind: str, ids: list[str], arg: str) -> str:
        """Asks for a confirmation of a change to existing entries (also for other callers, e.g. a delete button);
        NOT_FOUND when none of them exists any more. Returns the text sent."""
        targets = self._targets(ids)
        reply = None
        if not targets:
            reply = self._with_missing(t("app.not_found"), ids, targets)
        elif kind != "delete" and targets[0][1] is None:
            reply = t("app.unreadable") + targets[0][0]
        if reply is not None:
            await self.send(msg.channel, msg.user_id, reply)
            return reply
        first = targets[0][1]
        if kind == "delete":  # an unreadable one is named by its id: deleting does not need to parse it
            head = t("app.confirm_delete_one") if len(targets) == 1 else t("app.confirm_delete", n=len(targets))
            text = head + "\n" + "\n".join(
                t("app.confirm_item", title=e.title, id=i) if e else i for i, e in targets)
        elif kind == "retitle":
            text = t("app.confirm_retitle", title=first.title, arg=arg)
        elif kind == "annotate":
            text = t("app.confirm_annotate", title=first.title, arg=arg)
        elif kind == "resummarize":
            text = t("app.confirm_resummarize", title=first.title)
        else:
            raise ValueError(f"unknown op kind {kind}")
        text = self._with_missing(text, ids, targets)
        op_id = self.ops.add(msg.channel, msg.user_id, kind, [i for i, _ in targets], arg, text)
        await self._ask(msg, op_id, text)
        return text

    async def _ask(self, msg: IncomingMessage, op_id: int, text: str) -> None:
        """Sends text with the op's Confirm / Cancel buttons; a typed yes now answers that op."""
        await self.send(msg.channel, msg.user_id, text,
                        [Button(t("app.confirm"), f"op:{op_id}:yes", t("app.confirm_hint")),
                         Button(t("app.cancel"), f"op:{op_id}:no", t("app.cancel_hint"))], op_id=op_id)
        self._awaiting[(msg.channel, msg.user_id)] = op_id  # after the send, which ends any earlier window

    def _asked_last(self, msg: IncomingMessage, op: dict) -> bool:
        """The op's confirmation (or its re-ask) is the latest chat turn: nothing was talked about since."""
        said = self.convos.last_reply(msg.channel, msg.user_id)
        return bool(op.get("text")) and said in (op["text"], t("app.op_reask") + op["text"])

    async def _reask(self, msg: IncomingMessage, op: dict) -> None:
        """One send with the same op and buttons: on ClawBot cheaper than restating the request and confirming."""
        text = t("app.op_reask") + op["text"]
        await self._ask(msg, op["id"], text)
        self.convos.add(msg.channel, msg.user_id, "user", msg.text.strip() or t("app.confirm"))
        self.convos.add(msg.channel, msg.user_id, "assistant", text)

    async def _finish_op(self, msg: IncomingMessage, op_id: int, confirmed: bool) -> None:
        op = self.ops.take(op_id, msg.channel, msg.user_id)
        if op is None:
            reply = t("app.op_expired")
        elif not confirmed:
            reply = t("app.op_cancelled")
        else:
            reply = await self._run_op(msg, op)
        await self._op_reply(msg, confirmed, reply)

    async def _op_reply(self, msg: IncomingMessage, confirmed: bool, reply: str) -> None:
        await self.send(msg.channel, msg.user_id, reply)  # also ends the typed-confirmation window
        # a chat turn too, so the next message is read against what actually happened
        said = msg.text.strip() or t("app.confirm" if confirmed else "app.cancel")
        self.convos.add(msg.channel, msg.user_id, "user", said)
        self.convos.add(msg.channel, msg.user_id, "assistant", reply)

    async def _run_op(self, msg: IncomingMessage, op: dict) -> str:
        kind, ids, arg = op["kind"], op["entry_ids"], op["arg"]
        if kind == "delete":
            deleted, missing = [], []
            for entry_id in ids:
                entry = self._entry(entry_id)
                if await self.pipeline.delete(entry_id):  # moves the file unparsed: an unreadable one goes too
                    deleted.append(t("app.deleted_item", title=entry.title) if entry else entry_id)
                else:
                    missing.append(entry_id)
                self.focus.clear(msg.channel, msg.user_id, entry_id)
            lines = [t("app.deleted", items=t("app.deleted_sep").join(deleted))] if deleted else []
            return "\n".join(lines + [t("common.not_found", token=i) for i in missing])
        entry_id = ids[0]
        if self.library.find(entry_id) and self._entry(entry_id) is None:  # broken since the question
            return t("app.unreadable") + entry_id
        if kind == "retitle":
            entry = await self.pipeline.retitle(entry_id, arg)
            reply = entry and t("app.retitled", title=entry.title, id=entry.id)
        elif kind == "annotate":
            entry = await self.pipeline.annotate(entry_id, arg)
            reply = entry and t("app.annotated", title=entry.title, id=entry.id)
        elif kind == "resummarize":
            out = await self.pipeline.resummarize(entry_id)
            if out is not None and out.summary_failed:  # same wording as /resummarize
                reply = t("commands.resummarize_failed", title=out.entry.title, note=out.note, id=out.entry.id)
            else:
                reply = out and format_saved(out)
        else:
            log.error("unknown op kind %s", kind)
            return t("app.not_found")
        if not reply:
            return t("app.not_found")
        self.focus.set(msg.channel, msg.user_id, entry_id)
        return reply

    async def _execute(self, job: Job) -> SaveOutcome | str:
        """Returns a SaveOutcome, or the id of an existing entry when the url was saved meanwhile."""
        if job.kind == "url":
            existing = self.index.find_by_url(job.payload["url"])
            if existing and await self.pipeline.locate(existing) is None:  # stale row: Markdown deleted by hand
                self.index.delete(existing)
            elif existing:
                return existing
            return await self.pipeline.save_url(job.payload["url"], job.channel, **self._page_of(job))
        if job.kind == "note":
            return await self.pipeline.save_note(job.payload["text"], job.channel, url=job.payload.get("url"),
                                                 title=job.payload.get("title"))
        if job.kind == "page":  # an HTML mail (P23), read as an article
            p = job.payload
            return await self.pipeline.save_page(Path(p["page"]), job.channel, url=p.get("url"), title=p.get("title"),
                                                 author=p.get("author"), text=p.get("text", ""))
        if job.kind == "attachments":
            files = [attachment_from_dict(d) for d in job.payload["files"]]
            return await self.pipeline.save_attachments(files, job.payload.get("caption", ""), job.channel,
                                                        entry_id=job.payload.get("entry_id"), url=job.payload.get("url"))
        if job.kind == "merge":
            files = [attachment_from_dict(d) for d in job.payload["files"]]

            def current() -> str:  # "save separately" may have moved the article to a new entry while this job waited
                return self.jobs.get(job.id).payload["entry_id"]

            out = await self.pipeline.merge(job.payload["entry_id"], files, job.payload.get("text", ""), current)
            while isinstance(out, str):  # the window followed it there too: the completion below sees the new id
                job.payload["entry_id"] = out
                out = await self.pipeline.merge(out, files, job.payload.get("text", ""), current)
            if out is None and self.jobs.creating(job.channel, job.user_id, job.payload["entry_id"]):
                raise _NotYet()  # the first files after /new are still being saved as that entry
            if out is None:  # entry deleted meanwhile (or never saved): save as a fresh entry instead
                return await self.pipeline.save_attachments(files, job.payload.get("text", ""), job.channel,
                                                            entry_id=job.payload.get("fallback_id"))
            return out
        if job.kind == "refetch":
            path = await self.pipeline.locate(job.payload["entry_id"])
            current = self.library.load(path) if path else None
            if current is not None and not fillable(current, "page" in job.payload):  # supplemented in the meantime
                if job.payload.get("by_canonical"):  # found by its canonical only: the page is an entry of its own
                    return await self.pipeline.save_url(job.payload["url"], job.channel, **self._page_of(job))
                return current.id
            # found by its canonical only: an entry completed or edited meanwhile makes the page an entry of its own
            or_new = job.channel if job.payload.get("by_canonical") else None
            out = await self.pipeline.refetch(job.payload["entry_id"], job.payload["url"], **self._page_of(job),
                                              or_new=or_new)
            if out is not None:
                return out
            existing = self.index.find_by_url(job.payload["url"])  # entry deleted meanwhile: saved again since?
            if existing and await self.pipeline.locate(existing) is not None:
                return existing
            return await self.pipeline.save_url(job.payload["url"], job.channel, **self._page_of(job))
        raise ValueError(f"unknown job kind {job.kind}")

    @staticmethod
    def _wants_browser(entry: Entry) -> bool:
        """A link only partly read (or not at all) whose page the Chrome extension could send (P36): a platform that
        needs the browser the user is logged in to (the link, or a redirect of it recorded at save: a short host not
        in the table, P37), or a page that read as a login wall."""
        if entry.status not in ("partial", "needs_manual") or entry.url is None or entry.source == "manual":
            return False
        return (any(needs_browser(u) for u in (entry.url, *entry.url_aliases))
                or (entry.status == "partial" and looks_walled(entry.body)))

    @staticmethod
    def _page_of(job: Job) -> dict:
        """A link job's page as the Chrome extension sent it (staged HTML and its title): read instead of fetching."""
        page = job.payload.get("page")
        return {"page": Path(page), "title": job.payload.get("title")} if page else {}

    def _drop_page(self, job: Job) -> None:
        """A finished (done or failed) job's staged page goes; a retry still needs it. Never raises."""
        page = job.payload.get("page")
        try:
            if not page or self.jobs.get(job.id).status in ("queued", "running"):
                return
            path = Path(page).resolve()
            if path.is_relative_to(self.cfg.incoming_dir.resolve()) and path.parent != self.cfg.incoming_dir.resolve():
                path.unlink(missing_ok=True)
                path.parent.rmdir()  # its own staging folder
        except Exception:
            log.debug("removing the page of job %s failed", job.id, exc_info=True)

    async def _run_job(self, job: Job) -> None:
        """Never raises: any failure reschedules the job or tells the user."""
        try:
            await self._run_job_inner(job)
        except Exception as e:
            log.exception("job %s failed unexpectedly", job.id)
            try:
                rescheduled = self.jobs.retry_or_fail(job.id, repr(e))
            except Exception:
                log.exception("could not record failure of job %s", job.id)
                rescheduled = False
            if not rescheduled:
                await self.send(job.channel, job.user_id, format_job_failed(str(e) or repr(e)))
        finally:
            self._drop_page(job)

    async def _run_job_inner(self, job: Job) -> None:
        try:
            outcome = await self._execute(job)
        except _NotYet:
            self.jobs.postpone(job.id, MERGE_WAIT_SECONDS)
            return
        except FetchError as e:
            log.warning("job %s: could not fetch %s: %s", job.id, host_of(job.payload.get("url")), e)
            if e.retryable and self.jobs.retry_or_fail(job.id, str(e)):
                if job.attempts == 0:  # first failure only: the user isn't left waiting in silence
                    await self.send(job.channel, job.user_id, t("app.fetch_retrying", reason=e))
                return
            kept = self.library.get(job.payload["entry_id"]) if job.kind == "refetch" else None
            if kept is not None:  # the entry already exists: keep it, don't write a second one
                outcome = SaveOutcome(kept, False, str(e))
            else:
                outcome = await self.pipeline.save_failed_url(job.payload["url"], job.channel, str(e))
        except Exception as e:
            log.exception("job %s failed", job.id)
            # e.g. the staged files are gone: retrying cannot help, so say so now
            permanent = isinstance(e, MediaError) and job.kind in ("attachments", "merge")
            if self.jobs.retry_or_fail(job.id, repr(e), retry=not permanent):
                return
            await self.send(job.channel, job.user_id, format_job_failed(str(e) or repr(e)))
            return
        if isinstance(outcome, str):
            self.jobs.complete(job.id, outcome)
            entry = self.library.get(outcome)
            await self.send(job.channel, job.user_id,
                            format_duplicate(outcome, entry.title if entry else job.payload["url"]))
            self.focus.set(job.channel, job.user_id, outcome)
            return
        self.jobs.complete(job.id, outcome.entry.id)
        entry = outcome.entry
        # only link entries invite supplements; a partial entry of the user's own files/notes (source "file" /
        # "manual", no url) does not. Keyed on the url because linked PDFs also have source "file"; a selection or
        # image from the Chrome extension keeps its page's url but is "manual"
        incomplete = (entry.status == "partial" and not outcome.summary_failed and entry.url is not None
                      and entry.source != "manual")
        buttons = []
        # a plain note, or the text that started a /new entry (no files), may have been meant as a question; a
        # selection from a page (the Chrome extension: the note keeps the page's link) never was
        said = (job.payload["text"] if job.kind == "note" and not job.payload.get("from_chat")
                and not job.payload.get("url")
                else job.payload.get("caption") if job.kind == "attachments" and not job.payload["files"] else None)
        if said is not None:
            action_id = self.actions.record(job.channel, job.user_id, "note", said, entry.id, entry.title)
            buttons = [self._fix_button("note", action_id)]
        elif outcome.merged and outcome.added_section:  # a real merge (not the fallback): "save separately" can undo it
            action_id = self.actions.record(job.channel, job.user_id, "merge", json.dumps(
                {"section": outcome.added_section, "files": outcome.added_files, "notes": outcome.added_notes,
                 "job": job.id, "via": job.payload.get("via")}, ensure_ascii=False), entry.id)
            buttons = [Button(t("app.split_label"), f"split:{action_id}", t("app.split_hint"))]
        reply = format_saved(outcome)
        if outcome.filled:
            if entry.status == "ok":
                self.pending.close_link_windows(entry.id)  # nothing is waited for any more on that entry
            if job.channel == EXTENSION:  # what the popup says of this request (extension.Intake._item)
                self.conn.execute("UPDATE extension_saves SET filled = ? WHERE job_id = ?", (entry.id, job.id))
        # a link save's reply only: a supplement that stays partial is not told to open the page again
        if (job.kind in ("url", "refetch") and job.channel != EXTENSION and not outcome.summary_failed
                and self._wants_browser(entry)):
            reply += "\n" + t("app.browser_hint" if self.cfg.extension.enabled else "app.browser_hint_install")
        if job.payload.get("links_unread"):
            reply += "\n" + t("app.links_unread")
        sent = await self.send(job.channel, job.user_id, reply, buttons)
        self.focus.set(job.channel, job.user_id, entry.id)
        # after the send: a reply to that prompt message targets this entry even outside the merge window
        if job.kind == "merge":
            # only while the window still waits on this job's target: after /new or another link it stays closed.
            # A target deleted meanwhile means the content became a new entry F: the window moves to F.
            # The reply is a prompt (a quote-reply to it adds more) only while the entry is still incomplete: a
            # quoted "what does this one say?" on a finished merge is a question.
            if self.pending.active(job.channel, job.user_id) == job.payload["entry_id"]:
                prompt = sent.message_id if entry.status == "needs_manual" or incomplete else None
                self.pending.set(job.channel, job.user_id, entry.id, prompt)  # restarts the window, same origin
        elif entry.status == "needs_manual" or incomplete:
            # never over a window the user opened after this link was sent (/add, /new): it finished late
            opened = self.pending.opened_at(job.channel, job.user_id)
            if (opened is None or opened <= job.created_at
                    or self.pending.active(job.channel, job.user_id) == entry.id):
                self.pending.set(job.channel, job.user_id, entry.id, sent.message_id, origin=LINK)
        elif (entry.status == "ok" and job.kind != "attachments"
              and self.pending.active(job.channel, job.user_id) == entry.id):
            self.pending.clear(job.channel, job.user_id)  # e.g. a refetch succeeded (not the first files after /new)

    async def process_due_jobs(self) -> None:
        """Run every currently due job to completion (used by tests and `octopus` one-shots)."""
        while jobs := self.jobs.claim_due(self.cfg.max_concurrent_jobs):
            await asyncio.gather(*(self._run_job(j) for j in jobs))

    async def _backfill(self) -> None:
        try:
            n = await self.pipeline.backfill_vectors()
            if n:
                log.info("backfilled vectors for %d entries", n)
        except Exception:
            log.exception("vector backfill failed")

    async def _scan_library(self) -> None:
        try:
            await self.library_sync.scan()
        except Exception:
            log.exception("looking for edits in the library failed")

    def _sweep_downloads(self) -> None:
        d = self.cfg.downloads_dir
        try:
            for p in (d.iterdir() if d.is_dir() else []):
                if p.is_file() or p.is_symlink():  # finished downloads and *.part leftovers
                    p.unlink(missing_ok=True)
        except OSError:
            log.exception("sweeping %s failed", d)

    def _sweep_incoming(self) -> None:
        """Staged files nobody will read any more (a stop mid-download, a message never handled, a job that failed
        for good): gone once untouched for INCOMING_MAX_AGE_SECONDS, unless a queued job still needs them."""
        root = self.cfg.incoming_dir
        try:
            if not root.is_dir():
                return
            keep = {Path(p).resolve() for p in self.jobs.staged_paths()}
            cutoff = time.time() - INCOMING_MAX_AGE_SECONDS
            removed = 0
            for item in root.iterdir():
                is_dir = item.is_dir() and not item.is_symlink()
                tree = [item, *item.rglob("*")] if is_dir else [item]
                if any(p.lstat().st_mtime > cutoff or p.resolve() in keep for p in tree):
                    continue
                if is_dir:
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
                removed += not item.exists()  # rmtree's errors are ignored: count only what went
            if removed:
                log.info("removed %d staged item(s) older than %d h from %s", removed,
                         INCOMING_MAX_AGE_SECONDS // 3600, root)
        except Exception:
            log.exception("sweeping %s failed", root)

    def _daily_backup(self) -> None:
        try:
            today = self.today()
            path = backup_db(self.conn, self.cfg.backups_dir, today)
            removed = prune(self.cfg.backups_dir)
            if path:
                log.info("database backed up to %s (%d old backups removed)", path, len(removed))
        except Exception:
            log.exception("daily backup failed")

    async def _tell_crash_failed(self, jobs: list[Job]) -> None:
        for job in jobs:
            self._drop_page(job)
            try:  # undeliverable now (e.g. ClawBot not connected yet): the outbox has it for the next message
                await self.send(job.channel, job.user_id, format_job_failed(t("jobs.crash_loop")))
            except Exception:
                log.exception("telling %s:%s about crash-failed job %s failed", job.channel, user_tag(job.user_id),
                              job.id)

    async def run_worker(self, stop: asyncio.Event) -> None:
        running: set[asyncio.Task] = set()
        stop_task = asyncio.create_task(stop.wait())
        backfill: asyncio.Task | None = None
        sweep: asyncio.Task | None = None
        scan: asyncio.Task | None = None
        next_scan = 0.0  # at start too: edits made while Octopus was not running

        def finished(task: asyncio.Task) -> None:
            running.discard(task)
            self.wake.set()

        notices: asyncio.Task | None = None
        try:
            self._sweep_downloads()  # nothing is in flight yet: whatever is there was left by a crash
            self._sweep_incoming()
            try:
                crash_failed = self.jobs.recover_failed()
            except Exception:
                log.exception("recovering interrupted jobs failed")
                crash_failed = []
            if crash_failed:  # in the background: a slow channel must not hold up the queue
                notices = asyncio.create_task(self._tell_crash_failed(crash_failed))
            next_backfill = 0.0
            next_sweep = 0.0  # at start too: replies held before a restart
            last_backup_day: date | None = None
            while not stop.is_set():
                try:
                    today = self.today()
                except Exception:
                    log.exception("reading the date failed")
                    today = last_backup_day
                if today != last_backup_day:
                    self._daily_backup()  # once at start, then whenever the date changes; failures never stop the worker
                    last_backup_day = today
                if (backfill is None or backfill.done()) and time.monotonic() >= next_backfill:
                    next_backfill = time.monotonic() + self.backfill_interval
                    try:
                        if self.index.missing_vectors():
                            backfill = asyncio.create_task(self._backfill())
                    except Exception:
                        log.exception("checking for missing vectors failed")
                # also soon after an entry's file was not where the index said (renamed in Obsidian)
                if (scan is None or scan.done()) and (time.monotonic() >= next_scan or self.library.missed):
                    self.library.missed = False
                    next_scan = time.monotonic() + self.library_scan_interval
                    scan = asyncio.create_task(self._scan_library())
                if (sweep is None or sweep.done()) and time.monotonic() >= next_sweep:
                    if sweep is not None and not sweep.cancelled() and sweep.exception() is not None:
                        log.debug("outbox sweep failed", exc_info=sweep.exception())
                    next_sweep = time.monotonic() + self.outbox_sweep_interval
                    sweep = asyncio.create_task(self.sweep_outbox())  # its own task: it may wait for a user's lock
                try:
                    for job in self.jobs.claim_due(self.cfg.max_concurrent_jobs - len(running)):
                        task = asyncio.create_task(self._run_job(job))
                        running.add(task)
                        task.add_done_callback(finished)
                except Exception:
                    log.exception("claiming jobs failed")
                    await asyncio.sleep(1)
                    continue
                wake_task = asyncio.create_task(self.wake.wait())
                try:
                    await asyncio.wait({wake_task, stop_task}, timeout=self.tick, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake_task.cancel()
                self.wake.clear()
        finally:
            stop_task.cancel()
            if notices is not None:
                await asyncio.gather(notices, return_exceptions=True)
            if backfill is not None:
                backfill.cancel()
                await asyncio.gather(backfill, return_exceptions=True)
            if sweep is not None:
                sweep.cancel()
                await asyncio.gather(sweep, return_exceptions=True)
            if scan is not None:
                scan.cancel()
                await asyncio.gather(scan, return_exceptions=True)
            if running:
                await asyncio.gather(*running, return_exceptions=True)
