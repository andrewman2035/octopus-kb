"""Audio-only downloads through yt-dlp (self-use on this Mac only; never on a server)."""
from __future__ import annotations

from pathlib import Path

from octopus.fetchers.base import FetchError
from octopus.i18n import aliases, t

__getattr__ = aliases({"BLOCKED_NOTE": "fetch.youtube_blocked", "COOKIES_STILL_BLOCKED_NOTE": "fetch.youtube_still_blocked",
                       "LOGIN_NOTE": "fetch.video_login", "LIVE_OR_BIG_NOTE": "fetch.live_or_big"})
_BOT_MARKERS = ("confirm you're not a bot", "confirm you’re not a bot")
_LOGIN_MARKERS = ("Sign in", "login", "premium", "members-only", "private")
_TRANSIENT_MARKERS = ("timed out", "Connection reset", "Connection refused", "Network is unreachable",
                      "nodename nor servname", "Temporary failure", "Remote end closed", "HTTP Error 5",
                      "HTTP Error 429", "urlopen error")


def download_audio(url: str, dest_dir: Path, *, cookies_from_browser: str = "", ydl_cls=None,
                   blocked_note: str | None = None, max_bytes: int | None = None) -> tuple[dict, Path]:
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "postprocessors": [],
        "match_filter": yt_dlp.utils.match_filter_func("!is_live"),
    }
    if max_bytes:
        opts["max_filesize"] = max_bytes
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    try:
        with (ydl_cls or yt_dlp.YoutubeDL)(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if any(m in msg for m in _BOT_MARKERS):
            raise FetchError(blocked_note or t("fetch.video_login"), retryable=False) from e
        if any(m in msg for m in _TRANSIENT_MARKERS):
            raise FetchError(t("fetch.download_failed", error=msg[:200]), retryable=True) from e
        if any(m.lower() in msg.lower() for m in _LOGIN_MARKERS):
            raise FetchError(t("fetch.video_login"), retryable=False) from e
        raise FetchError(t("fetch.download_failed", error=msg[:200]), retryable=False) from e
    downloads = info.get("requested_downloads") or []
    if info.get("is_live") or not downloads:  # skipped by the live filter or max_filesize
        raise FetchError(t("fetch.live_or_big"), retryable=False)
    return info, Path(downloads[0]["filepath"])
