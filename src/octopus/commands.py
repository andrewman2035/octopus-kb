"""Slash commands."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import date
from pathlib import Path

from octopus.channels.base import Button, Reply
from octopus.formatting import format_saved
from octopus.i18n import aliases, t
from octopus.index import Index
from octopus.index.chunker import chunk_text
from octopus.jobs import JobQueue
from octopus.library import ATTACH_LINE_RE, Entry, Library
from octopus.llm.router import MODEL_MODE_KEY, LLMRouter
from octopus.pipeline import SavePipeline
from octopus.settings import Settings

log = logging.getLogger(__name__)
# under the guide; no fallback: ClawBot has no buttons and the guide already says how to type each command
_GUIDE_BUTTONS = [("recent", "cmd:recent"), ("list", "cmd:list"), ("status", "cmd:status"), ("model", "cmd:model"),
                  ("model_local", "cmd:model local"), ("model_default", "cmd:model default"), ("delete", "cmd:delete")]
# what a tapped cmd:<name>[ <arg>] may run (tappable commands); anything else is ignored
TAPPABLE = {"recent": {""}, "list": {""}, "status": {""}, "model": {"", "local", "default"}, "delete": {""}, "help": {""}}
ALIASES = {"del": "delete", "start": "help"}  # Telegram sends /start on first contact
DELETE_LABEL_CHARS = 12
_ID_SEPARATORS = re.compile(r"[\s,，]+")  # zh: input
MAX_AMBIGUOUS_LISTED = 10
RECENT_DEFAULT = 10
RECENT_MAX = 50
_DATE = re.compile(r"(\d{4})([-/.])(\d{1,2})\2(\d{1,2})|(\d{4})(\d{2})(\d{2})")
STATUSES = ("queued", "running", "failed")  # job states the user sees by name (others: as they are)
MARKS = {"partial": "commands.mark.partial", "needs_manual": "commands.mark.needs_manual"}  # status -> catalog key
# one part is one message on every channel (ClawBot's 2000, with the part mark and the "more" line added), so the
# part numbers are the same everywhere
READ_PART_CHARS = 1800
_ATTACHMENT_LINE = re.compile(ATTACH_LINE_RE.pattern + r"\n?", re.M)  # each file is its own section too
_IMAGE_EMBED = re.compile(r"!\[\[[^\]\n]*\]\]|!\[[^\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)")  # a_(1).png too
_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+")
FIND_MAX = 20
SNIPPET_CHARS = 30  # with the indent and two …, at most 34 on the line
SNIPPET_BEFORE = 10
_TERM = re.compile(r'"([^"]+)"|“([^”]+)”|(\S+)')


def guide_buttons() -> list[Button]:
    return [Button(t(f"commands.button.{name}"), data, "") for name, data in _GUIDE_BUTTONS]


def task_name(task: str) -> str:
    """"summarize" / "answer" as the user reads it."""
    return t(f"commands.task.{task}")


def _mark(status: str) -> str:
    return t(MARKS[status]) if status in MARKS else ""


__getattr__ = aliases({  # the old constant names, in the configured language
    "HELP": "commands.help", "DELETE_PICK": "commands.delete_pick", "DELETE_USAGE": "commands.delete_usage",
    "NOTHING_SAVED": "commands.nothing_saved", "TRASHED": "commands.trashed", "AMBIGUOUS_ID": "commands.ambiguous_id",
    "SAVE_USAGE": "commands.save_usage", "LIST_USAGE": "commands.list_usage", "READ_USAGE": "commands.read_usage",
    "READ_NOT_FOUND": "commands.read_not_found", "READ_GONE": "commands.read_gone", "FIND_USAGE": "commands.find_usage",
    "FIND_WORDS_NONE": "commands.find_none", "GUIDE_BUTTONS": guide_buttons,
    "RECENT_USAGE": lambda: t("commands.recent_usage", max=RECENT_MAX),
    "RECENT_CAPPED": lambda: t("commands.recent_capped", max=RECENT_MAX), "LIST_HEADER": "commands.list_header",
    "LIST_NONE": "commands.list_none", "READ_NO_BODY": "commands.read_no_body", "READ_TOTAL": "commands.read_total",
    "READ_PART_MARK": "commands.read_part_mark", "READ_MORE": "commands.read_more", "FIND_HEADER": "commands.find_header",
    "FIND_MORE": "commands.find_more", "FIND_READ_HINT": "commands.find_read_hint",
    "TASK_NAMES": lambda: {task: task_name(task) for task in ("summarize", "answer")},
    "STATUS_NAMES": lambda: {k: t(f"commands.status.{k}") for k in STATUSES},
    "CLAWBOT_DISABLED": "commands.clawbot_disabled", "CLAWBOT_CONNECTED": "commands.clawbot_connected",
    "CLAWBOT_QR_REQUESTED": "commands.clawbot_qr_requested",
})


def ambiguous_block(library: Library, token: str, ids: list[str]) -> str:
    """The reply for a short id that names several entries: they are listed (id  title) so the user can type more."""
    lines = []
    for eid in ids[:MAX_AMBIGUOUS_LISTED]:
        try:
            entry = library.get(eid)
        except Exception:
            log.warning("could not read entry %s for its title", eid, exc_info=True)
            entry = None
        lines.append(f"{eid}  {entry.title}" if entry else eid)
    return "\n".join([t("commands.ambiguous_id", token=token)] + lines)


def _listed(rows: list[dict]) -> str:
    return "\n".join(f"{r['id']}  {r['title']}{_mark(r['status'])}" for r in rows)


def _chat_text(body: str) -> str:
    """The body as it reads in chat: attachment paths dropped, picture embeds as the catalog's picture placeholder,
    no # before headings."""
    body = _HEADING.sub("", _IMAGE_EMBED.sub(t("commands.picture"), _ATTACHMENT_LINE.sub("", body)))
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def read_parts(entry: Entry) -> list[str]:
    """/read: the entry's text (title, id, date, link, summary, points, notes, body) in parts of at most
    READ_PART_CHARS, cut at paragraphs (long ones at sentences, else hard)."""
    head = [t("commands.read_title", title=entry.title, mark=_mark(entry.status)),
            t("common.label", label="id", value=entry.id), t("commands.read_saved", date=f"{entry.saved_at:%Y-%m-%d}")]
    if entry.url:
        head.append(t("commands.read_link", url=entry.url))
    blocks = ["\n".join(head)]
    if entry.summary:
        blocks.append(t("commands.read_summary", summary=entry.summary))
    if entry.points:
        blocks.append(t("commands.read_points", points="\n".join(f"· {p}" for p in entry.points)))
    if entry.notes.strip():
        blocks.append(t("commands.read_notes", notes=entry.notes.strip()))
    body = _chat_text(entry.body)
    blocks.append(t("commands.read_body", body=body) if body else t("commands.read_no_body"))
    return chunk_text("\n\n".join(blocks), READ_PART_CHARS, READ_PART_CHARS)


def _fold(text: str) -> str:
    """How /find compares text: full-width letters and punctuation as half-width (as
    NFKC does), any case, runs of
    whitespace as one space. The query and the searched text are folded alike."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text).casefold())


def _terms(query: str) -> list[str]:
    """/find's words, folded: split at spaces, "a b" / “a b” kept as one phrase."""
    found = (next(g for g in m.groups() if g) for m in _TERM.finditer(unicodedata.normalize("NFKC", query)))
    return list(dict.fromkeys(term for term in (_fold(f.strip('"“”')).strip() for f in found) if term))


def _snippet(chunks: list[str], terms: list[str]) -> str | None:
    """About SNIPPET_CHARS of the text as saved around the first match, in the first part (summary, body, notes)
    that has one."""
    for chunk in chunks:
        text = re.sub(r"\s+", " ", chunk).strip()
        folded, where = "", []  # where[i]: the character of text that folded[i] came from
        for i, c in enumerate(text):
            f = _fold(c)
            folded += f
            where += [i] * len(f)
        hits = [i for term in terms if (i := folded.find(term)) >= 0]
        if not hits:
            continue
        at = where[min(hits)]
        start = max(0, min(at - SNIPPET_BEFORE, len(text) - SNIPPET_CHARS))
        end = start + SNIPPET_CHARS
        return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    return None


def _day(text: str) -> date | None:
    """2026-10-14, 20261014, 2026/10/14 or 2026.10.14 (full-width digits too); None when not a real date."""
    m = _DATE.fullmatch(unicodedata.normalize("NFKC", text))
    if not m:
        return None
    y, mo, d = (m.group(1), m.group(3), m.group(4)) if m.group(1) else (m.group(5), m.group(6), m.group(7))
    try:
        return date(int(y), int(mo), int(d))
    except ValueError:
        return None


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class Commands:
    def __init__(self, llm: LLMRouter, settings: Settings, library: Library, index: Index,
                 jobs: JobQueue, pipeline: SavePipeline, data_dir: Path, clawbot=None):
        self.llm = llm
        self.settings = settings
        self.library = library
        self.index = index
        self.jobs = jobs
        self.pipeline = pipeline
        self.data_dir = data_dir
        # the running ClawBotChannel (has request_login() -> bool); set by `octopus run`, None elsewhere
        self.clawbot = clawbot
        self.today: Callable[[], date] = date.today  # /list without a date; App hands over its own clock

    def _models(self) -> str:
        return "\n".join(t("common.label", label=task_name(task),
                           value=t("common.model_fallback", model=chain[0], fallback=chain[1]) if len(chain) > 1
                           else chain[0])
                         for task, chain in self.llm.describe().items())

    async def reply(self, name: str, args: list[str]) -> Reply:
        """The command's reply, with buttons where it has them (the guide, the /delete picker)."""
        name = ALIASES.get(name, name)
        handler = getattr(self, f"cmd_{name}", None)
        if handler is None:
            return Reply(t("commands.unknown", name=name, help=t("commands.help")), guide_buttons())
        out = await handler(args)
        return out if isinstance(out, Reply) else Reply(out)

    async def run(self, name: str, args: list[str]) -> str:
        return (await self.reply(name, args)).text

    async def cmd_help(self, args: list[str]) -> Reply:
        return Reply(t("commands.help"), guide_buttons())

    async def cmd_save(self, args: list[str]) -> str:
        return t("commands.save_usage")  # /s with content never gets here: the router turns it into a save

    async def cmd_model(self, args: list[str]) -> str:
        if not args:
            return t("commands.model_current", models=self._models())
        if args[0] == "local":
            self.settings.set(MODEL_MODE_KEY, "local")
            return t("commands.model_local", models=self._models())
        if args[0] == "default":
            self.settings.delete(MODEL_MODE_KEY)
            return t("commands.model_default", models=self._models())
        return t("commands.model_usage")

    async def cmd_recent(self, args: list[str]) -> str:
        n = RECENT_DEFAULT
        if args:
            count = unicodedata.normalize("NFKC", args[0])  # full-width digits
            if len(args) > 1 or not (count.isascii() and count.isdecimal()) or int(count) <= 0:
                return t("commands.recent_usage", max=RECENT_MAX)
            n = int(count)
        rows = self.index.recent(min(n, RECENT_MAX))
        if not rows:
            return t("commands.nothing_saved")
        capped = n > RECENT_MAX and len(rows) == RECENT_MAX  # only when something was left out
        capped_line = "\n" + t("commands.recent_capped", max=RECENT_MAX) if capped else ""
        return t("commands.recent_header") + "\n" + _listed(rows) + capped_line

    async def cmd_list(self, args: list[str]) -> str:
        """Every entry saved on one local calendar day (the date part of saved_at), newest first."""
        day = self.today() if not args else _day(args[0]) if len(args) == 1 else None
        if day is None:
            return t("commands.list_usage")
        rows = self.index.saved_between(day.isoformat(), day.isoformat(), -1)  # no limit: the whole day
        if not rows:
            return t("commands.list_none", day=day.isoformat())
        return t("commands.list_header", day=day.isoformat(), n=len(rows)) + "\n" + _listed(rows)

    async def cmd_find(self, args: list[str]) -> str:
        """Entries whose title or indexed text contains every word, newest first. A plain substring scan, no
        model: jieba cuts short Chinese words unpredictably (one three-character word into two
        pieces), and the library is small."""
        query = " ".join(args).strip()
        terms = _terms(query)
        if not terms:
            return t("commands.find_usage")
        hits = []
        for row in self.index.texts():
            chunks = row["chunks"]
            if chunks == [row["title"]]:  # an entry with no text yet is indexed as its title
                chunks = []
            if chunks and chunks[0].startswith(f"{row['title']}\n"):  # the summary part starts with the title
                chunks = [chunks[0][len(row["title"]) + 1:], *chunks[1:]]
            chunks = [_chat_text(c) for c in chunks]  # as /read shows it: no attachment paths, no image markup
            text = _fold("\n".join([row["title"], *chunks]))
            if all(term in text for term in terms):
                hits.append((row, _snippet(chunks, terms)))
        if not hits:
            return t("commands.find_none", q=query)
        lines = [t("commands.find_header", q=query, n=len(hits))]
        for row, snippet in hits[:FIND_MAX]:
            lines.append(_listed([row]))
            if snippet:
                lines.append(f"  {snippet}")
        if len(hits) > FIND_MAX:
            lines.append(t("commands.find_more", m=len(hits) - FIND_MAX))
        return "\n".join(lines + [t("commands.find_read_hint")])

    async def cmd_delete(self, args: list[str]) -> str | Reply:
        if not args:
            return self._delete_picker()
        ids = list(dict.fromkeys(tok for tok in _ID_SEPARATORS.split(" ".join(args)) if tok))  # duplicates once
        if not ids:  # "/delete ,": nothing named, same as no id
            return self._delete_picker()
        resolved = [(token, *self.library.resolve(token)) for token in ids]  # before anything moves
        lines, deleted, done = [], False, set()
        for token, eid, matches in resolved:
            if matches:
                lines.append(ambiguous_block(self.library, token, matches))
                continue
            if eid is None:
                lines.append(t("common.not_found", token=token))
                continue
            if eid in done:  # a full id and its short form name the same entry
                continue
            done.add(eid)
            title = self._title(eid)
            if await self.pipeline.delete(eid):  # moves the file without parsing it: corrupt ones go too
                lines.append(t("commands.deleted_titled", title=title, id=eid) if title else t("commands.deleted", id=eid))
                deleted = True
            else:
                lines.append(t("common.not_found", token=eid))
        return "\n".join(lines + ([t("commands.trashed")] if deleted else []))

    def _title(self, entry_id: str) -> str | None:
        """The entry's title, None when its file is missing or unreadable (it can still be deleted)."""
        try:
            entry = self.library.get(entry_id)
        except Exception:
            log.warning("could not read entry %s for its title", entry_id, exc_info=True)
            return None
        return entry.title if entry else None

    def _delete_picker(self) -> str | Reply:
        """The 10 latest entries, one delete button each (pressing one still asks for a confirmation, see App)."""
        rows = self.index.recent(10)
        if not rows:
            return t("commands.nothing_saved")
        text = t("commands.delete_pick") + "\n" + "\n".join(f"{r['id']}  {r['title']}" for r in rows)
        # ClawBot has no buttons: it shows the list plus how to type the command (the last button's fallback)
        buttons = [Button(t("commands.delete_button", title=r["title"][:DELETE_LABEL_CHARS]), f"del:{r['id']}",
                          t("commands.delete_usage") if i == len(rows) - 1 else "") for i, r in enumerate(rows)]
        return Reply(text, buttons)

    async def cmd_resummarize(self, args: list[str]) -> str:
        if not args:
            return t("commands.resummarize_usage")
        eid, matches = self.library.resolve(args[0])
        if matches:
            return ambiguous_block(self.library, args[0], matches)
        out = await self.pipeline.resummarize(eid) if eid else None
        if out is None:
            return t("commands.resummarize_not_found", token=args[0])
        if out.summary_failed:
            return t("commands.resummarize_failed", title=out.entry.title, note=out.note, id=out.entry.id)
        return format_saved(out)

    async def cmd_clawbot(self, args: list[str]) -> str:
        if self.clawbot is None:
            return t("commands.clawbot_disabled")
        # False: connected, nothing to scan; True: a fresh QR attempt starts now, the code comes as a notice
        return t("commands.clawbot_qr_requested" if self.clawbot.request_login() else "commands.clawbot_connected")

    async def cmd_status(self, args: list[str]) -> str:
        counts = self.jobs.counts()
        jobs_line = t("commands.status_jobs_sep").join(
            t("common.label", label=t(f"commands.status.{k}") if k in STATUSES else k, value=v)
            for k, v in counts.items()) or t("commands.status_no_jobs")

        def disk_size() -> int:
            size = 0
            for p in self.data_dir.rglob("*"):
                try:
                    if p.is_file():
                        size += p.stat().st_size
                except FileNotFoundError:
                    continue  # file vanished mid-walk
            return size

        size = await asyncio.to_thread(disk_size)
        lines = [t("commands.status_saved", n=self.index.count()), t("commands.status_jobs", jobs=jobs_line),
                 self._models(), t("commands.status_disk", size=_human_size(size))]
        missing = len(self.index.missing_vectors())
        if missing:
            lines.append(t("commands.status_missing_vectors", n=missing))
        failures = self.jobs.recent_failures(3)
        if failures:
            lines.append(t("commands.status_failures") + "\n" + "\n".join(
                t("commands.status_failure", what=j.payload.get("url") or j.kind, error=j.last_error) for j in failures))
        return "\n".join(lines)
