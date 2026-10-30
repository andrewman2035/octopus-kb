"""Bilibili videos: audio via yt-dlp, transcribed locally (subtitles need login)."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from octopus.fetchers import http
from octopus.fetchers.base import FetchResult
from octopus.fetchers.ytdlp import download_audio

_BV = re.compile(r"(BV[0-9A-Za-z]{10})")


class BilibiliFetcher:
    name = "bilibili"

    def __init__(self, download_dir: Path, ydl_cls=None, max_bytes: int | None = None):
        self.download_dir = download_dir
        self.ydl_cls = ydl_cls
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return host == "b23.tv" or ((host == "bilibili.com" or host.endswith(".bilibili.com"))
                                    and parts.path.startswith("/video/"))

    def fetch(self, url: str) -> FetchResult:
        if (urlsplit(url).hostname or "").lower() == "b23.tv":
            p = urlsplit(http.resolve(url))
            part = parse_qs(p.query).get("p", [""])[0]  # keep the part number of multi-part videos
            url = urlunsplit((p.scheme, p.netloc, p.path, f"p={part}" if part.isdigit() else "", ""))
        info, path = download_audio(url, self.download_dir, ydl_cls=self.ydl_cls, max_bytes=self.max_bytes)
        m = _BV.search(url)
        return FetchResult(url=url, source=self.name, title=info.get("title"),
                           author=info.get("uploader") or info.get("channel"),
                           media_id=m.group(1) if m else info.get("id"), audio_path=path)
