"""YouTube: captions first, otherwise audio for local transcription."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from octopus.fetchers import http
from octopus.fetchers.base import FetchError, FetchResult
from octopus.fetchers.ytdlp import download_audio
from octopus.i18n import t
from octopus.media.asr import paragraphs
from octopus.text import is_cjk, to_simplified

log = logging.getLogger(__name__)
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")
CAPTION_LANGS = ["zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "en", "en-US"]
_SOUND_TAG = re.compile(r"^[\[(（【][^\])）】]*[\])）】]$")  # zh: content


def _clean_snippet(text: str) -> str:
    """Collapse whitespace, drop '>>' speaker markers and pure sound tags like [Music] / (applause)."""
    line = " ".join(text.replace(">>", " ").split())
    return "" if _SOUND_TAG.match(line) else line


def video_id(url: str) -> str | None:
    m = _ID.search(url)
    return m.group(1) if m else None


class YouTubeFetcher:
    name = "youtube"

    def __init__(self, download_dir: Path, cookies_from_browser: str = "", transcript_api=None, ydl_cls=None,
                 max_bytes: int | None = None):
        self.download_dir = download_dir
        self.cookies_from_browser = cookies_from_browser
        self.transcript_api = transcript_api
        self.ydl_cls = ydl_cls
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() in HOSTS

    def _oembed(self, canonical: str) -> tuple[str | None, str | None]:
        try:
            data = http.get(f"https://www.youtube.com/oembed?format=json&url={quote(canonical, safe='')}").json()
            return data.get("title"), data.get("author_name")
        except Exception as e:  # metadata only; never fatal
            log.info("youtube oembed failed: %s", e)
            return None, None

    def _captions(self, vid: str) -> tuple[str | None, str | None]:
        try:
            if self.transcript_api is None:
                from youtube_transcript_api import YouTubeTranscriptApi

                self.transcript_api = YouTubeTranscriptApi()
            tl = self.transcript_api.list(vid)
            try:
                track = tl.find_transcript(CAPTION_LANGS)
            except Exception:
                track = next(iter(tl))
            snippets = track.fetch()
            segs = [{"text": _clean_snippet(s.text), "start": s.start, "end": s.start + s.duration} for s in snippets]
            text = paragraphs(segs)
            lang = track.language_code
            if text and not lang.startswith(("ja", "ko")) and any(is_cjk(c) for c in text):
                text = to_simplified(text)
            return (text or None), lang
        except Exception as e:  # blocked, disabled, none available: fall back to audio
            log.info("youtube captions unavailable for %s: %s", vid, e)
            return None, None

    def fetch(self, url: str) -> FetchResult:
        vid = video_id(url)
        if not vid:
            raise FetchError(t("fetch.youtube_no_id"), retryable=False)
        canonical = f"https://www.youtube.com/watch?v={vid}"
        title, author = self._oembed(canonical)
        text, lang = self._captions(vid)
        if text:
            return FetchResult(url=url, source=self.name, title=title, author=author, text=text,
                               language=lang, media_id=vid)
        info, path = download_audio(canonical, self.download_dir, cookies_from_browser=self.cookies_from_browser,
                                    ydl_cls=self.ydl_cls, max_bytes=self.max_bytes,
                                    blocked_note=t("fetch.youtube_still_blocked" if self.cookies_from_browser
                                                   else "fetch.youtube_blocked"))
        return FetchResult(url=url, source=self.name, title=title or info.get("title"),
                           author=author or info.get("uploader") or info.get("channel"),
                           media_id=vid, audio_path=path)
