"""Decide what an incoming message means (plain text is chat, /s or "save:" saves)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from octopus.models import IncomingMessage
from octopus.urls import extract_urls, without_urls


class RouteKind(StrEnum):
    SAVE_URLS = "save_urls"
    SAVE_NOTE = "save_note"
    SAVE_ATTACHMENTS = "save_attachments"
    QUESTION = "question"
    COMMAND = "command"
    CORRECTION = "correction"  # a bare "save" / "ask" (or their Chinese words): flip the user's last save / answer
    VOICE = "voice"  # hold-to-talk recording: transcribed, then handled as typed text
    GREETING = "greeting"  # only "hi" / "hello" … (either language): answered with the usage guide


@dataclass
class Route:
    kind: RouteKind
    text: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    # SAVE_URLS: the links to read. A note or files: the links kept in its text, not read
    urls: list[str] = field(default_factory=list)
    pasted: bool = False  # a note without /s or "save:": pasted material, which a failed link's window takes


LONG_PASTE_CHARS = 300  # this long is pasted material, never a management request: no intent call
# 存 / 保存 ("save") with a colon, comma or space; English "save" only with a colon or comma ("save the world?" is a
# question)
SAVE_PREFIX = re.compile(r"^(?:(?:存|保存)(?:[ \t]*[：:，,][ \t]*|\s+)|(?i:save)[ \t]*[：:，,][ \t]*)")  # zh: input
SLASH_SAVE = re.compile(r"^/(?:s|save)(?=\s|$)", re.I)
ASK_PREFIX = re.compile(r"^(?:问|ask)\s*[：:]\s*", re.I)  # zh: input
GREETING = re.compile(
    r"^(?:hi|hello|hey|yo|嗨|哈喽|哈啰|你好|您好|在吗|在不在|早|早上好|中午好|下午好|晚上好)"
    r"(?:\s*(?:oct|octo|octopus|章鱼))?$", re.I)  # zh: input
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")
_GREETING_TAIL = re.compile(r"[\s!！~～。.,，?？啊呀呢]+$")  # zh: input
TRAILING_PARTICLES = "啊呀吧呢嘛哈"  # zh: input
# 补充 ("add") <id> [text]: a full id, or 3-6 hex characters with a digit (so "补充 add" or "补充一下" ("add a bit")
# stay chat)
ADD_ALIAS = re.compile(
    r"^(?:补充|add)\s*[：:]?\s*(\d{8}-[0-9a-f]{6}|(?=[0-9a-f]*\d)[0-9a-f]{3,6})(?=\s|$)", re.I)  # zh: input
# same as /new: the next things sent are saved on their own (compared without spaces, in lower case)
NEW_WORDS = {"新的", "新的一条", "单独存下一条", "new", "newone", "newentry", "newnote"}  # zh: input
# same as /split: the latest merge was the wrong entry
SPLIT_WORDS = {"单独存", "单独保存", "分开存", "另存", "separate", "separately", "saveseparately", "split"}  # zh: input
# the bare corrections: save the last answer (存) or turn the last note into a question (问)
CORRECTIONS = {"存": "存", "问": "问", "save": "存", "ask": "问"}  # zh: input
# "save them", "keep these screenshots as one note": about the files just sent (app.about_files_sent), never a note
# that says "them" (spoken with a period or "please" too)
SAVE_THEM = re.compile(
    r"(?:please )?(?:save|keep|combine|merge)(?: (?:it|them|these|those|this|that|all|everything|the above|"
    r"(?:(?:all )?(?:the |these |those )?(?:above )?)?(?:images?|pictures?|photos?|screenshots?|pics?)(?: above)?))+"
    r"(?: (?:as|into|in) (?:one|a single)(?: (?:note|entry|article|post))?)?(?: please)?")


def words(text: str) -> list[str]:
    """The words of a short reply, punctuation and emoji dropped ("yes, delete it." → yes,
    delete, it)."""
    return [p for p in re.split(r"[\W_]+", text) if p]


def bare_word(text: str) -> str | None:
    """The "save" or "ask" correction when that is all the message says, typed or transcribed ("Save.", "ask", or
    the Chinese words with punctuation or a particle); the same normalisation as a typed confirmation. "save:" (and
    its Chinese form) stays the save prefix without content."""
    if "：" in text or ":" in text:  # zh: input
        return None
    parts = words(text)
    word = parts[0].rstrip(TRAILING_PARTICLES).lower() if len(parts) == 1 else ""
    # /save and /ask are commands; the Chinese words after a slash are corrections
    if text.lstrip().startswith("/") and word.isascii():
        return None
    return CORRECTIONS.get(word)


def about_saving_them(text: str) -> bool:
    """English "save them" / "keep these screenshots as one note": the files just sent, not a note's content."""
    return bool(SAVE_THEM.fullmatch(" ".join(words(text)).lower()))


def is_greeting(text: str) -> bool:
    """The whole message is a greeting: case, emoji and trailing punctuation or a Chinese particle ignored."""
    return bool(GREETING.match(_GREETING_TAIL.sub("", _EMOJI.sub("", text).strip())))


def _save_route(msg: IncomingMessage, text: str, *, prefixed: bool) -> Route | None:
    """prefixed: after /s or "save:". Its links are read only when they are all it says (punctuation aside); without
    the prefix, unless the text beside them is LONG_PASTE_CHARS long (an article pasted with its links). Read
    or not, the text is kept."""
    urls = extract_urls(text)
    rest = without_urls(text)
    if urls and not (words(rest) if prefixed else len(rest.strip()) >= LONG_PASTE_CHARS):
        return Route(RouteKind.SAVE_URLS, text=text, urls=urls)
    if msg.attachments:
        return Route(RouteKind.SAVE_ATTACHMENTS, text=text, urls=urls)
    if text:
        return Route(RouteKind.SAVE_NOTE, text=text, urls=urls, pasted=not prefixed)
    return None


def classify(msg: IncomingMessage, alias: bool = True) -> Route | None:
    text = msg.text.strip()
    if not msg.attachments and (word := bare_word(text)):  # a photo captioned "save" is still a photo to save
        return Route(RouteKind.CORRECTION, text=word)
    # "separate" / "new" with punctuation or a particle ("New!"), as bare_word allows
    bare = "".join(words(text)).rstrip(TRAILING_PARTICLES).lower()
    if not msg.attachments and bare in NEW_WORDS:
        return Route(RouteKind.COMMAND, text=text, command="new")
    if not msg.attachments and bare in SPLIT_WORDS:
        return Route(RouteKind.COMMAND, text=text, command="split")
    if not msg.attachments and is_greeting(text):
        return Route(RouteKind.GREETING, text=text)
    if m := SLASH_SAVE.match(text):
        return (_save_route(msg, text[m.end():].strip(), prefixed=True)
                or Route(RouteKind.COMMAND, text=text, command="save"))
    if (m := SAVE_PREFIX.match(text)) and not about_saving_them(text):  # "save:" alone: a /s without content
        return (_save_route(msg, text[m.end():].strip(), prefixed=True)
                or Route(RouteKind.COMMAND, text=text, command="save"))
    if m := ASK_PREFIX.match(text):
        return Route(RouteKind.QUESTION, text=text[m.end():].strip())
    if alias and (m := ADD_ALIAS.match(text)):  # the text stays ONE argument, spacing kept
        rest = text[m.end():].strip()
        return Route(RouteKind.COMMAND, text=text, command="add", args=[m.group(1).lower(), *([rest] if rest else [])])
    if text.startswith("/"):
        parts = text[1:].split()
        if parts:
            return Route(RouteKind.COMMAND, text=text, command=parts[0].lower(), args=parts[1:])
    if msg.attachments and all(a.voice for a in msg.attachments):
        return Route(RouteKind.VOICE, text=text)
    if extract_urls(text) or msg.attachments:
        return _save_route(msg, text, prefixed=False)
    if not text:
        return None
    return Route(RouteKind.QUESTION, text=text)
