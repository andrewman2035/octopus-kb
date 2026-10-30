"""A post's page title made short (P38): on a platform whose page title is the post's whole text plus the site's name
(Xiaohongshu), the entry is titled with its first sentence instead."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from octopus.urls import host_of

# the platforms whose page title is the post text, and the site name their pages add to it (seen on saved notes:
# "<the note's text> - 小红书"). Douyin, Weibo, TikTok, X, Threads, Instagram, Facebook are not here: see p5-notes P38
POST_TITLE_SITES = {
    ("xiaohongshu.com", "xhslink.com", "xhslink.cn"): re.compile(r"\s+-\s+小红书$"),  # zh: content
}
MAX_CHARS = 40  # longer: the first sentence only, cut here with "…"; up to this the text is kept whole
MIN_CHARS = 6  # a first sentence shorter than this (its end mark aside) takes the next one too

# a topic as the post shows it: "#露营" or "＃露营" ending at a space, the next "#" or the end, or the app's
# "#露营[话题]#"; at most 20 characters, no sentence punctuation, not after a Latin letter or digit ("C#" is a word).
# A "#" whose words run on into the text is not a topic: only the "#" goes
_HASHTAG = re.compile(  # zh: content
    r"(?<![A-Za-z0-9])[#＃]([^\s#＃\[\]，。！？!?、,.;；:：]{1,20})(?:\[话题\]#|(?=[\s#＃]|$))")
_STRAY_HASH = re.compile(r"(?<![A-Za-z0-9])[#\uff03]")  # a "#" or full-width one left in the text
_STICKER = re.compile(r"\[[^\[\]\s]{1,6}R\]")  # the app's stickers as text: a word or two and "R" in brackets
_EMOJI = "\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff\U0001f000-\U0001faff"
_IN_EMOJI = "\ufe0e\ufe0f\u200d\u20e3\U000e0020-\U000e007f"  # variation selectors, joiner, keycap, tag letters
# where the first sentence ends: after "！？!?" (kept); at "。", a newline, a "." before a space, or an emoji (with what
# joins it) before a space or the end (dropped)
_END = re.compile(rf"[！？!?]+|。|\n|\.+(?=\s)|[{_EMOJI}][{_EMOJI}{_IN_EMOJI}]*(?=\s|$)")  # zh: content
_KEPT_END = "！？!?"  # zh: content
_TRAILING = " ,，、;；:："  # zh: content


def _is_regional(ch: str) -> bool:
    return "\U0001f1e6" <= ch <= "\U0001f1ff"


def _inside(s: str, i: int) -> bool:
    """s[i] continues the character or emoji before it: cutting at i would split it."""
    ch, before = s[i], s[i - 1]
    if ch in _IN_EMOJI or "\U0001f3fb" <= ch <= "\U0001f3ff" or before == "\u200d" or unicodedata.combining(ch):
        return True
    if "\udc00" <= ch <= "\udfff" and "\ud800" <= before <= "\udbff":  # a pair not joined into one character
        return True
    if _is_regional(ch):  # a flag is two letters: inside one after an odd number of them
        n = i
        while n > 0 and _is_regional(s[n - 1]):
            n -= 1
        return (i - n) % 2 == 1
    return False


def _cut(s: str) -> str:
    if len(s) <= MAX_CHARS:
        return s
    i = MAX_CHARS
    while i > 0 and _inside(s, i):
        i -= 1
    i = i or MAX_CHARS  # one emoji sequence of 40 characters: cut it all the same
    if s[i - 1].isascii() and s[i - 1].isalnum() and s[i].isascii() and s[i].isalnum():  # not inside a word
        space = s.rfind(" ", 0, i)
        i = space if space > MAX_CHARS // 2 else i
    return s[:i].rstrip(_TRAILING) + "…"


def _first_sentence(s: str) -> str:
    for m in _END.finditer(s):
        piece = s[:m.end()] if m.group()[0] in _KEPT_END else s[:m.start()]
        if len(piece.rstrip(_KEPT_END).strip()) >= MIN_CHARS:
            return piece.strip()
    return s.strip()


def _suffix(urls: Iterable[str | None]) -> re.Pattern | None:
    hosts = [host_of(u).rstrip(".") for u in urls]
    for domains, suffix in POST_TITLE_SITES.items():
        if any(h == d or h.endswith("." + d) for h in hosts for d in domains):
            return suffix
    return None


def post_title(title: str | None, urls: Iterable[str | None]) -> str | None:
    """The title an entry gets from its page's title: on a post-text platform (urls: the post's link and the links it
    was reached by; None for none) the text without the site's name, the hashtags (they are in the tags) and the
    app's stickers; longer than MAX_CHARS, its first sentence, at most MAX_CHARS characters ("…" added). A title of
    hashtags only keeps their words. Any other title as it is."""
    suffix = _suffix(urls)
    if not title or suffix is None:
        return title
    text = _STICKER.sub("", suffix.sub("", title.strip()))
    words = _HASHTAG.findall(text)
    text = _STRAY_HASH.sub("", _HASHTAG.sub(" ", text))
    if not text.strip():
        text = " ".join(words)
    whole = re.sub(r"\s+", " ", text).strip()
    if len(whole) <= MAX_CHARS:
        return whole or title.strip()
    return _cut(re.sub(r"\s+", " ", _first_sentence(text)).strip())
