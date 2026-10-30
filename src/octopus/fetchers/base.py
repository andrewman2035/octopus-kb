from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# set by the pipeline for the fetch it runs in a thread: the event is set when the job is cancelled, so a long
# download stops instead of finishing for nobody (P29). asyncio.to_thread copies the context into the thread.
FETCH_CANCEL: ContextVar[threading.Event | None] = ContextVar("octopus_fetch_cancel", default=None)


@dataclass
class FetchResult:
    url: str | None
    source: str
    title: str | None = None
    author: str | None = None
    text: str = ""
    language: str | None = None
    status: str = "ok"  # ok / partial / needs_manual
    media_url: str | None = None
    media_id: str | None = None
    audio_path: Path | None = None  # set by media fetchers (P2); pipeline transcribes it
    doc_path: Path | None = None  # a downloaded PDF; the pipeline extracts its text (under its heavy lock)
    image_paths: list[Path] = field(default_factory=list)  # downloaded post images; the pipeline OCRs them
    video_paths: list[Path] = field(default_factory=list)  # downloaded post videos (X); the pipeline transcribes them
    video_numbers: list[int] = field(default_factory=list)  # each one's place among the post's videos; empty: 1, 2, …
    note: str | None = None  # why partial / needs_manual, shown to the user
    truncated: bool = False  # the text is only the start of the post (a long X post's first 280 characters)
    # other links of this page (P36 fix round 1): the redirects that led to it, its canonical / og:url; the entry is
    # found by them too, so a phone's share link and the browser tab's link are one entry
    aliases: list[str] = field(default_factory=list)


class FetchError(Exception):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class FetchCancelled(Exception):
    """The job this fetch runs for was cancelled (not a FetchError: never taken for a failed download)."""


def check_cancelled() -> None:
    event = FETCH_CANCEL.get()
    if event is not None and event.is_set():
        raise FetchCancelled()


class Fetcher(Protocol):
    name: str

    def can_handle(self, url: str) -> bool: ...

    def fetch(self, url: str) -> FetchResult: ...
