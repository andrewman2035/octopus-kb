"""Small text helpers shared by fetchers and media."""
from __future__ import annotations

import re
import unicodedata

from opencc import OpenCC

_T2S = OpenCC("t2s")
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_NOT_WORDS = re.compile(r"https?://\S+|[@\uff20][0-9A-Za-z-]+|\d{8}-[0-9a-f]{6}|[A-Za-z]*\d[0-9A-Za-z]*")


def is_cjk(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch[0])
    return (0x2E80 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
            or 0xFF00 <= cp <= 0xFFEF or unicodedata.east_asian_width(ch[0]) in ("W", "F"))


def join_text(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    return a + ("" if is_cjk(a[-1]) or is_cjk(b[0]) else " ") + b


def to_simplified(s: str) -> str:
    return _T2S.convert(s)


def text_language(text: str, configured: str = "zh") -> str:
    """The language a text is in, for its prompt (P25): "zh" or "en", biased to the configured language. Fenced and
    inline code, links, @ids, entry ids and tokens with digits are left out; Chinese characters are counted against
    Latin words. With a Chinese setup only a text with no Chinese character at all is English; with an English one
    only a text with at least two Chinese characters and twice as many as Latin words is Chinese."""
    sample = _NOT_WORDS.sub(" ", _CODE.sub(" ", text[:8000]))
    han = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
    latin = len(_LATIN_WORD.findall(sample))
    if configured == "en":
        return "zh" if han >= 2 and han >= 2 * latin else "en"
    return "en" if han == 0 and latin > 0 else "zh"
