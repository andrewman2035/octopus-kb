"""Answer questions from the knowledge base with numbered citations."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from octopus import i18n
from octopus.i18n import text_in
from octopus.index import Index
from octopus.library import Entry
from octopus.llm.router import LLMRouter
from octopus.text import text_language

# the Chinese prompt's parts (the catalog holds both languages; an answer follows its question's language, P25)
GENERAL_NOTE = text_in("zh", "prompt.qa.general_note")
SYSTEM = text_in("zh", "prompt.qa.system")
GENERAL_NOTES = tuple(text_in(lang, "prompt.qa.general_note") for lang in i18n.LANGUAGES)
# entries the question names (an id, a title, "this one"): their whole text goes first in the material
MAX_PINNED = 3
PINNED_ENTRY_CHARS = 12000
PINNED_TOTAL_CHARS = 20000
CUT = text_in("zh", "prompt.qa.cut")
PINNED_MARK = text_in("zh", "prompt.qa.pinned_mark")
PINNED_MARKS = tuple(text_in(lang, "prompt.qa.pinned_mark") for lang in i18n.LANGUAGES)
QUOTED_TITLE_MIN = 4  # a quoted part of a title, in any quotes (Chinese title marks too)
FULL_TITLE_MIN = 6  # a whole title typed without quotes
_QUOTED = re.compile(r"《([^《》]+)》|“([^“”]+)”|\"([^\"]+)\"|「([^「」]+)」")  # zh: input
# "这篇讲了什么" ("what does this one say"): a short question about the entry just saved or talked about
POINTING = re.compile(r"这篇|这条|这个(?:视频|链接|文章|播客|帖子)|这个笔记(?!本)|刚(?:才|刚)?(?:存|保存|发)的|"
                      r"刚才(?:那|这)(?:篇|条)|上面(?:那|这)(?:篇|条)|"
                      r"\bthis (?:one|article|video|link|post|podcast|note|entry|page)\b|\bthe one above\b|"
                      r"\bthe (?:one|article|video|link|post) (?:i|you) (?:just )?(?:saved|sent)\b|"
                      r"\b(?:just|recently) saved\b|\bthat (?:one|article|video|post)\b", re.I)  # zh: input
POINTING_MAX_CHARS = 40


@dataclass
class Source:
    n: int
    entry_id: str
    title: str
    url: str | None


@dataclass
class Answer:
    text: str
    sources: list[Source]
    provider: str
    fell_back: bool


def _clean_url(url: str | None) -> str:
    """The entry's link for the prompt; none when it is not one plain token (a newline could smuggle text in)."""
    url = (url or "").strip()
    return "" if any(c.isspace() for c in url) else url


def title_pins(question: str, titles: list[tuple[str, str]]) -> list[str]:
    """Ids of the entries the question names by title: quoted text in exactly one title, or a whole title."""
    found = []
    for m in _QUOTED.finditer(question):
        quoted = next(g for g in m.groups() if g).strip().casefold()
        if len(quoted) >= QUOTED_TITLE_MIN:
            ids = [i for i, t in titles if quoted in t.casefold()]
            found += ids if len(ids) == 1 else []  # several titles have it: search instead, no question back
    q = question.casefold()
    whole = [(i, t.casefold()) for i, t in titles if len(t) >= FULL_TITLE_MIN and t.casefold() in q]
    shared = Counter(t.casefold() for _, t in titles)
    # not a part of a longer one, and only a title no other entry has
    found += [i for i, t in whole if shared[t] == 1 and not any(t != u and t in u for _, u in whole)]
    return list(dict.fromkeys(found))


def points_back(question: str) -> bool:
    """A short question about "this one" ("the one just saved", "the one above" …, in either language), with no id or
    title of its own."""
    return len(question.strip()) <= POINTING_MAX_CHARS and bool(POINTING.search(question))


def _full_text(e: Entry, lang: str = "zh") -> str:
    parts = [e.summary.strip() and text_in(lang, "prompt.qa.summary", text=e.summary.strip()),
             e.points and text_in(lang, "prompt.qa.points", points="\n".join(f"- {p}" for p in e.points)),
             e.notes.strip() and text_in(lang, "prompt.qa.comments", text=e.notes.strip()),
             e.body.strip() and text_in(lang, "prompt.qa.body", text=e.body.strip())]
    return "\n\n".join(p for p in parts if p)


def _without_marks(text: str) -> str:
    """The material cannot pose as the pinned mark, in either language."""
    for mark in PINNED_MARKS:
        text = text.replace(mark, "")
    return text


def _pinned_texts(entries: list[Entry], lang: str = "zh") -> list[str]:
    """Each entry's full text within PINNED_ENTRY_CHARS, all of them within PINNED_TOTAL_CHARS: shorter
    ones are taken whole, the room left is shared evenly by the longer ones."""
    cut = text_in(lang, "prompt.qa.cut")
    texts = [_without_marks(_full_text(e, lang)) for e in entries]  # the text cannot pose as the mark
    room, left = [0] * len(texts), PINNED_TOTAL_CHARS
    for done, i in enumerate(sorted(range(len(texts)), key=lambda i: len(texts[i]))):
        room[i] = min(len(texts[i]), PINNED_ENTRY_CHARS, left // (len(texts) - done))
        left -= room[i]
    return [text if len(text) <= r else text[:max(r - len(cut), 0)] + cut for text, r in zip(texts, room)]


async def answer(question: str, index: Index, llm: LLMRouter,
                 history: list[tuple[str, str]], k: int = 8, pinned: list[Entry] | None = None,
                 search: bool = True) -> Answer:
    """pinned: entries the question names; they come first with their full text, search hits after them.
    search=False: the pinned entries are all the material ("@id question"). The prompt, and so the answer, is in the
    question's language (P25)."""
    lang = text_language(question, i18n.get_language())
    pinned = (pinned or [])[:MAX_PINNED]
    sources = {e.id: Source(n, e.id, e.title, e.url) for n, e in enumerate(pinned, 1)}
    # one search slot per pinned entry; their own chunks are left out (already there in full)
    hits = [h for h in await index.search(question, k * 4 if pinned else k)
            if h.entry_id not in sources][:k - len(pinned)] if search else []
    for h in hits:
        if h.entry_id not in sources:
            sources[h.entry_id] = Source(len(sources) + 1, h.entry_id, h.title, h.url)
    linked: set[str] = set()

    def head(entry_id: str, title: str, url: str | None) -> str:
        link = _clean_url(url) if entry_id not in linked else ""  # once per entry, on its first snippet
        linked.add(entry_id)
        shown = text_in(lang, "prompt.qa.link", url=link) if link else ""
        return text_in(lang, "prompt.qa.source", n=sources[entry_id].n, title=title, id=entry_id, link=shown)

    mark = text_in(lang, "prompt.qa.pinned_mark")
    parts = [f"{head(e.id, e.title, e.url)}\n{mark}\n{text}" for e, text in zip(pinned, _pinned_texts(pinned, lang))]
    parts += [f"{head(h.entry_id, h.title, h.url)}\n{_without_marks(h.text)}" for h in hits]
    snippets = "\n\n".join(parts)
    hist = "\n".join(text_in(lang, "prompt.turn", who=text_in(lang, "prompt.user" if role == "user" else "prompt.assistant"),
                             text=said) for role, said in history)
    none = text_in(lang, "prompt.none")
    prompt = text_in(lang, "prompt.qa.request", history=hist or none, material=snippets or none, question=question,
                     pinned_mark=mark, cut=text_in(lang, "prompt.qa.cut"),
                     general_note=text_in(lang, "prompt.qa.general_note"))
    res = await llm.complete("answer", prompt, text_in(lang, "prompt.qa.system"))
    text = res.text.strip()
    if text.startswith(GENERAL_NOTES):  # general knowledge: any [n] in it does not refer to the library
        return Answer(text, [], res.provider, res.fell_back)
    cited = {int(n) for group in re.findall(r"[\[【]([\d,，、\s]+)[\]】]", res.text)
             for n in re.findall(r"\d+", group)}  # zh: content
    return Answer(text, [s for s in sources.values() if s.n in cited], res.provider, res.fell_back)
