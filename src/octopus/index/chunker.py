"""Split entries into ~500-char chunks along paragraph boundaries."""
from __future__ import annotations

import re

from octopus.i18n import t
from octopus.library import Entry

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;.])")  # zh: content


def _split_long(paragraph: str, target: int, hard_max: int) -> list[str]:
    out, buf = [], ""
    for sentence in filter(None, _SENTENCE_END.split(paragraph)):
        for piece in (sentence[i:i + hard_max] for i in range(0, len(sentence), hard_max)):
            if buf and len(buf) + len(piece) > target:
                out.append(buf)
                buf = piece
            else:
                buf += piece
    if buf:
        out.append(buf)
    return out


def chunk_text(text: str, target: int = 500, hard_max: int = 800) -> list[str]:
    pieces: list[str] = []
    for p in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        pieces.extend([p] if len(p) <= hard_max else _split_long(p, target, hard_max))
    chunks, buf = [], ""
    for p in pieces:
        if buf and len(buf) + len(p) + 2 > target:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def entry_chunks(entry: Entry) -> list[str]:
    chunks = []
    if entry.summary or entry.points:
        chunks.append("\n".join([entry.title, entry.summary, *entry.points]).strip())
    chunks.extend(chunk_text(entry.body))
    for own in (entry.preamble, entry.more):  # what the user wrote around the summary in Obsidian (P18)
        chunks.extend(chunk_text(own))
    if entry.notes.strip():
        chunks.append(t("note.comments_label", text=entry.notes.strip()))
    return chunks or [entry.title]
