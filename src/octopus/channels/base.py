"""Channel adapter interface shared by CLI, Telegram, Feishu and ClawBot."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from octopus.models import IncomingMessage
from octopus.urls import links_as_hosts

log = logging.getLogger(__name__)
OnMessage = Callable[[IncomingMessage], Awaitable[None]]
MAX_NAME_BYTES = 200  # file systems cap names at 255 bytes
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
LOG_PREVIEW_CHARS = 20


@dataclass
class Button:
    label: str      # shown on platforms with buttons
    data: str       # delivered back as IncomingMessage.callback
    fallback: str   # text hint for platforms without buttons (ClawBot)


@dataclass
class Reply:
    text: str
    buttons: list[Button] = field(default_factory=list)
    plain: bool = False  # shown as typed, never as markup (Feishu's button card: /read parts, P35)


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    # why not ok: "error" (the send failed), or the channel did not try: "budget", "no-context", "not-connected"
    reason: str = ""


class Channel(Protocol):
    name: str
    acks_saves: bool  # False for ClawBot: it must not send the "Got it" acknowledgement (rate limits)

    async def start(self, on_message: OnMessage) -> None: ...

    async def send(self, user_id: str, reply: Reply) -> SendResult: ...

    # Optional: async def send_image(self, user_id: str, path: Path, caption: str = "") -> SendResult
    # Optional: async def send_file(self, user_id: str, data: bytes, filename: str) -> SendResult  (a document;
    #   reason "too-big" over the platform's limit; without it /read offers no file)
    # Optional: def is_owner(self, user_id: str) -> bool
    # Optional: def sends_left(self, user_id: str) -> int  (a send budget, ClawBot; without it: unlimited)


def user_tag(user_id: str) -> str:
    """A user as the log shows them: the last 4 hex digits of a hash of the id, never the id itself."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[-4:]


def log_preview(text: str) -> str:
    """The start of a message for the log: one line, no control characters, each link only its host, at most
    LOG_PREVIEW_CHARS."""
    flat = " ".join(_CONTROL_CHARS.sub(" ", links_as_hosts(text)).split())
    return flat if len(flat) <= LOG_PREVIEW_CHARS else flat[:LOG_PREVIEW_CHARS] + "…"


def send_status(result: SendResult) -> str:
    """ok, held(<reason>) (the channel did not try: ClawBot's budget, no reply context …) or failed(error)."""
    if result.ok:
        return "ok"
    word = "failed" if result.reason == "error" else "held"
    return f"{word}({result.reason})" if result.reason else word


def out_line(channel: str, user_id: str, text: str, status: str, *, buttons: int = 0, resend: bool = False) -> str:
    """The log line of a reply: `out <channel> <tag> <n><chars> [buttons=k] "<preview>" [outbox]
    <status>`, where <chars> is the fixed Chinese unit word (log lines are not translated)."""
    fields = [f"{len(text)}字", f"buttons={buttons}" if buttons else "", f'"{log_preview(text)}"',
              "outbox" if resend else "", status]  # zh: log
    return f"out {channel} {user_tag(user_id)} " + " ".join(f for f in fields if f)


def log_dropped(channel: str, user_id: str, reason: str) -> None:
    """A message a channel does not hand over to App (too big, a download failed, nothing readable): App never logs
    it, so the channel does, the way App would. Never raises."""
    try:
        log.info("in %s %s not handed over: %s", channel, user_tag(user_id), reason)
    except Exception:
        log.debug("logging a dropped message failed", exc_info=True)


async def tell_dropped(channel: Channel, user_id: str, reason: str, text: str) -> None:
    """log_dropped, then the channel's own reply about it (it never passes App._deliver) with its out line."""
    log_dropped(channel.name, user_id, reason)
    result = await channel.send(user_id, Reply(text))
    try:
        log.info(out_line(channel.name, user_id, text, send_status(result)))
    except Exception:
        log.debug("logging a reply failed", exc_info=True)


def split_text(text: str, limit: int) -> list[str]:
    """Split into non-empty chunks of at most `limit` characters, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        part = text[:cut].rstrip("\n")
        if part:
            parts.append(part)
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts or [""]


def safe_filename(name, fallback: str) -> str:
    """A platform-supplied file name made safe to stage: bare (no directories, / or \\), no control characters,
    never ""/"."/"..", at most MAX_NAME_BYTES UTF-8 bytes (the suffix is kept when it is a sane one)."""
    bare = Path(str(name or "").replace("\\", "/")).name
    bare = _CONTROL_CHARS.sub("", bare).strip()
    if bare in ("", ".", ".."):
        return fallback
    if len(bare.encode("utf-8")) > MAX_NAME_BYTES:  # keep the suffix, cut the stem on a character boundary
        suffix = Path(bare).suffix
        if len(suffix.encode("utf-8")) > 20:
            suffix = ""
        stem = bare[: len(bare) - len(suffix)] if suffix else bare
        budget = MAX_NAME_BYTES - len(suffix.encode("utf-8"))
        bare = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore") + suffix
    return bare
