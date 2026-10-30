"""What a chat message asks to do with the user's records: find, delete, edit, save."""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from octopus import i18n
from octopus.i18n import text_in
from octopus.library import ENTRY_ID_RE, SHORT_ID_RE
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.text import text_language

log = logging.getLogger(__name__)

# only messages with one of these words (or an entry id, see wants_manage) cost an intent call; other
# questions go straight to qa
MANAGE_WORDS = re.compile(
    r"删|去掉|移除|改|换成|标题|备注|总结|找|搜|查|列出|哪些|存过|存了|存一下|存下|保存一下|记一下|记下|记住|帮我存|这条|那条|"
    r"上面|刚才|条目|记录|笔记")  # zh: input
# English requests (only in a message with no Chinese character): a leading verb, or a pointer at the library
MANAGE_WORDS_EN = re.compile(
    r"^(?:please |can you |could you )?(?:delete|remove|rename|retitle|find|search|list|show|summari[sz]e)\b|"
    r"\b(?:this one|that one|the one above|this entry|that entry|my notes|my entries|my saves|i saved|did i save)\b",
    re.I)
_HAN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff]")
# an id with none of the words above, asked about ("b305ad 讲了什么" "what does b305ad say", "20261015-b305ad 的要点"
# "its key points"): a question for qa
ABOUT_ENTRY = re.compile(r"讲|说|要点|内容|观点|介绍|意思|什么|啥|怎么|为什么|哪|谁|吗|[？?]")  # zh: input
# English ones count only in a message with no Chinese character, like MANAGE_WORDS_EN
ABOUT_ENTRY_EN = re.compile(r"\b(?:what|about|say|says|said|mean|means|explain|points|who|why|how|tell)\b", re.I)
KINDS = {"answer", "find", "delete", "retitle", "annotate", "resummarize", "save"}
TARGETS = {"", "focus", "search"}
MAX_RECENT = 10
MAX_INTENT_TEXT = 300  # a management request is short; a long paste is cut for the intent call
# the Chinese prompt's parts (the catalog holds both languages; the prompt follows the message's language, P25)
SYSTEM = text_in("zh", "prompt.manage.system")
INSTRUCTIONS = text_in("zh", "prompt.manage.instructions")
_FENCE = re.compile(r"^```[A-Za-z]*\s*(.*?)\s*```$", re.S)
# a short id inside a sentence: hex, bounded by non-alphanumerics (Chinese text around it is fine)
_SHORT_TOKEN = re.compile(r"(?<![0-9A-Za-z])" + SHORT_ID_RE.pattern + r"(?![0-9A-Za-z])")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# "id 731542", "编号：123" ("number: 123") just before the token; not the end of a word ("covid 2019", "paid 100"),
# not the verb 记录 ("record")
_ID_MARK = re.compile(r"(?:(?<![A-Za-z])(?i:id)|编号|记录号)[ \t]*[：:]?[ \t]*$")  # zh: input
UNMARKED_ID_MIN = 5  # an unmarked short id in a question: as long as the ids people copy, so B2B / e2e / A100 are words
FULL_ID_TOKEN = re.compile(r"(?<![0-9A-Za-z])" + ENTRY_ID_RE.pattern + r"(?![0-9A-Za-z])")
# "@b305ad 讲了什么" ("what does it say"): @ (or a full-width ＠) and an id-shaped token at the start; Chinese right
# after it is fine, a letter is not
_AT_ID = re.compile(r"[@＠](\d{8}-[0-9a-fA-F]{6}|" + SHORT_ID_RE.pattern + r")(?![0-9A-Za-z_])")  # zh: input
# between two @ids: spaces, commas, 和 / 与 / 跟 / 及 / 还有 ("and") / & ("@b305ad 和 @5cc8 有什么不同", "how do they
# differ")
_AT_JOIN = re.compile(r"(?:[\s,，、&]|和|与|跟|及|还有|and\b|vs\.?|or\b)*", re.I)  # zh: input
_WORD_CHAR = re.compile(r"[^\W_]")  # a question of only punctuation or emoji ("@b305ad?", full-width "?" too) is none


@dataclass
class Intent:
    kind: str
    ids: list[str] = field(default_factory=list)
    target: str = ""
    query: str = ""
    title: str = ""
    note: str = ""
    content: str = ""
    since: str | None = None
    until: str | None = None
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)  # (typed token, ids it could be)


def short_id_tokens(text: str) -> list[str]:
    """Tokens that may be a short id (see Library.match_short): they need a digit, so words like "add" or "cafe"
    are not."""
    rest = ENTRY_ID_RE.sub(" ", text)  # the tail of a full id is not a token of its own
    return list(dict.fromkeys(t for t in _SHORT_TOKEN.findall(rest) if any(c.isdigit() for c in t)))


def question_id_tokens(text: str) -> list[tuple[str, bool]]:
    """The short-id tokens a question may name an entry by, each with whether it was marked as an id: marked
    ("id 731542", or the Chinese word for "number" before it) 3-6 characters with a digit; unmarked 5-6 with a
    digit and a letter a-f ("b305ad").
    Plain numbers (an amount, a year) and words like B2B, e2e, A100 are not."""
    rest = ENTRY_ID_RE.sub(" ", text)
    found: dict[str, bool] = {}
    for m in _SHORT_TOKEN.finditer(rest):
        t = m.group()
        if not any(c.isdigit() for c in t):
            continue
        if _ID_MARK.search(rest, 0, m.start()):
            found[t] = True
        elif len(t) >= UNMARKED_ID_MIN and any(c in "abcdefABCDEF" for c in t):
            found.setdefault(t, False)
    return list(found.items())


def at_ids(text: str) -> tuple[list[str], str] | None:
    """The @ tokens leading the message (lower case, each once) and the question after them ("" when it is only
    punctuation); None when it does not start with one (an @name greeting, "@all", an @ further on). Full-width
    characters count as half-width (a full-width "@b305ad")."""
    text = unicodedata.normalize("NFKC", text)
    tokens, pos, end = [], 0, 0
    while m := _AT_ID.match(text, pos):
        tokens.append(m.group(1).lower())
        end = m.end()
        pos = _AT_JOIN.match(text, end).end()  # taken only when another @id follows
    if not tokens:
        return None
    question = text[end:].lstrip(" \t\n,，、").strip()  # zh: input
    return list(dict.fromkeys(tokens)), question if _WORD_CHAR.search(question) else ""


def wants_manage(text: str, resolve: Callable[[str], tuple[str | None, list[str]]] | None = None) -> bool:
    """A management word, or an entry id (given resolve, also a short-id token that names at least one existing
    entry) in a message that does not just ask about it."""
    if MANAGE_WORDS.search(text) or (not _HAN.search(text) and MANAGE_WORDS_EN.search(text.strip())):
        return True
    named = bool(ENTRY_ID_RE.search(text)) or (resolve is not None and any(
        eid or matches for eid, matches in map(resolve, short_id_tokens(text))))
    return named and not (ABOUT_ENTRY.search(text) or (not _HAN.search(text) and ABOUT_ENTRY_EN.search(text)))


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _date(value: object) -> str | None:
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _load(text: str) -> object:
    text = text.strip()
    if m := _FENCE.match(text):
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")  # e.g. a sentence before the object
        if start < 0 or end <= start:
            raise ValueError(f"intent is not JSON: {text[:200]!r}") from None
        return json.loads(text[start:end + 1])  # JSONDecodeError is a ValueError


def parse_intent(text: str) -> Intent:
    """The validator for the intent call: raises ValueError so the router tries the next model."""
    data = _load(text)
    if not isinstance(data, dict):
        raise ValueError("intent JSON is not an object")
    kind = _str(data.get("intent")).lower()
    if kind not in KINDS:
        raise ValueError(f"unknown intent {kind!r}")
    ids = data.get("ids")
    target = _str(data.get("target")).lower()
    return Intent(
        kind=kind,
        ids=list(dict.fromkeys(i.strip() for i in ids if isinstance(i, str) and ENTRY_ID_RE.fullmatch(i.strip())))
        if isinstance(ids, list) else [],
        target=target if target in TARGETS else "",
        query=_str(data.get("query")),
        title=_str(data.get("title")),
        note=_str(data.get("note")),
        content=_str(data.get("content")),
        since=_date(data.get("since")),
        until=_date(data.get("until")),
    )


def prompt_language(text: str) -> str:
    return text_language(text, i18n.get_language())


def build_prompt(text: str, history: list[tuple[str, str]], focus: tuple[str, str] | None,
                 recent: list[tuple[str, str]], today: date) -> str:
    """In the message's language (P25): an English request gets an English prompt."""
    lang = prompt_language(text)
    none = text_in(lang, "prompt.none")
    focus_line = text_in(lang, "prompt.manage.focus", id=focus[0], title=focus[1]) if focus else none
    recent_lines = "\n".join(text_in(lang, "prompt.manage.recent", id=i, title=title)
                             for i, title in recent[:MAX_RECENT]) or none
    hist = "\n".join(text_in(lang, "prompt.turn", who=text_in(lang, "prompt.user" if role == "user" else "prompt.assistant"),
                             text=said) for role, said in history) or none
    return text_in(lang, "prompt.manage.request", today=f"{today:%Y-%m-%d}", focus=focus_line, recent=recent_lines,
                   history=hist, text=text, instructions=text_in(lang, "prompt.manage.instructions"))


async def detect(llm: LLMRouter, text: str, history: list[tuple[str, str]], focus: tuple[str, str] | None,
                 recent: list[tuple[str, str]], today: date,
                 resolve: Callable[[str], tuple[str | None, list[str]]] | None = None) -> Intent | None:
    """None: answer it as an ordinary question (no management words, or no model could tell).
    resolve (Library.resolve) turns short ids the user typed into entry ids."""
    if not wants_manage(text, resolve):
        return None
    prompt = build_prompt(text[:MAX_INTENT_TEXT], history, focus, recent, today)
    try:
        res = await llm.complete("summarize", prompt, text_in(prompt_language(text), "prompt.manage.system"),
                                 validate=parse_intent)
    except LLMError as e:
        log.info("intent detection failed, answering instead: %s", e)
        return None
    intent: Intent = res.value
    explicit = list(dict.fromkeys(ENTRY_ID_RE.findall(text)))
    if resolve:
        for token in short_id_tokens(text):
            eid, matches = resolve(token)
            if eid:
                explicit.append(eid)
            elif matches:
                intent.ambiguous.append((token, matches))
        explicit = list(dict.fromkeys(explicit))
    if explicit:
        # ids the user typed are the ones meant; an ambiguous token beside them is still reported by the caller
        intent.ids = explicit
    else:  # the model may only point at entries it was shown (incl. our recent listings), never invent one
        known = {i for i, _ in recent[:MAX_RECENT]} | ({focus[0]} if focus else set())
        known |= {i for role, t in history if role == "assistant" for i in ENTRY_ID_RE.findall(t)}
        intent.ids = [i for i in intent.ids if i in known]
    return intent
