"""Chinese-aware tokenization for FTS5 (jieba search-mode segmentation)."""
from __future__ import annotations

import logging
import re

import jieba

jieba.setLogLevel(logging.WARNING)

_WORD = re.compile(r"\w", re.U)
STOPWORDS = {
    "的", "了", "是", "吗", "呢", "啊", "吧", "什么", "怎么", "为什么", "如何", "哪些", "哪个",
    "我", "你", "他", "她", "它", "我们", "有", "在", "和", "与", "这", "那", "这个", "那个",
    "一个", "有没有", "能不能", "是否", "关于", "讲", "说",
    "a", "an", "the", "is", "are", "was", "what", "how", "why", "of", "to", "in", "and", "or",
    "do", "does", "i", "you",
}  # zh: content


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in jieba.cut_for_search(text) if t.strip() and _WORD.search(t)]


def to_fts_doc(text: str) -> str:
    return " ".join(tokenize(text))


def to_fts_query(text: str) -> str | None:
    toks = [t for t in dict.fromkeys(tokenize(text)) if t not in STOPWORDS]
    if not toks:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)
