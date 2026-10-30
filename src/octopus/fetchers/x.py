"""X / Twitter single posts via the public syndication endpoint (no login); their videos are downloaded for the
pipeline to transcribe (P29), a long post's full text optionally comes from FxTwitter."""
from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

from octopus.fetchers import http
from octopus.fetchers.base import FetchError, FetchResult, check_cancelled
from octopus.fetchers.web import MIN_TEXT
from octopus.i18n import t

_STATUS = re.compile(r"^/([^/]+)/status(?:es)?/(\d+)")
_LINK = re.compile(r"https?://\S+")
HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "mobile.x.com"}
log = logging.getLogger(__name__)
MAX_VIDEOS = 4  # a post shows at most four media; its quoted post's come after its own
VIDEO_HOST = "video.twimg.com"  # the only host a video is downloaded from (URLs come from the post's JSON)
FXTWITTER = "https://api.fxtwitter.com/2/status/{id}"  # FxTwitter API v2 "Get post" (docs.fxembed.com)


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _bitrate_key(variant: dict) -> tuple[bool, int]:
    bitrate = variant.get("bitrate")
    known = isinstance(bitrate, int) and not isinstance(bitrate, bool)
    return (not known, bitrate if known else 0)  # one without a bitrate may be the HD file: it ranks last


def video_urls(data: dict) -> list[str]:
    """The lowest-bitrate mp4 (enough for speech) of each video in the post, then in its quoted post: https on
    video.twimg.com only. Animated GIFs are silent loops: nothing to transcribe. Media JSON of an unexpected shape
    is skipped, never fatal: the post's text is saved anyway."""
    media = [*_list(data.get("mediaDetails")), *_list(_dict(data.get("quoted_tweet")).get("mediaDetails"))]
    urls = []
    for m in media:
        if not isinstance(m, dict) or m.get("type") != "video":
            continue
        variants = [v for v in _list(_dict(m.get("video_info")).get("variants"))
                    if isinstance(v, dict) and v.get("content_type") == "video/mp4" and _is_video_url(v.get("url"))]
        if variants:
            urls.append(min(variants, key=_bitrate_key)["url"])
    return list(dict.fromkeys(urls))


def _is_video_url(url) -> bool:
    """https://video.twimg.com/… exactly: no other host, port or user part (the download follows no redirect)."""
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        return False
    return (p.scheme == "https" and p.hostname == VIDEO_HOST and port is None
            and p.username is None and p.password is None)


def syndication_token(tweet_id: str) -> str:
    # Verified against Node's `((id / 1e15) * Math.PI).toString(36)` for ids 20 and 1519480761749016577.
    from yt_dlp.jsinterp import js_number_to_string

    return re.sub(r"(0+|\.)", "", js_number_to_string((int(tweet_id) / 1e15) * math.pi, 36))


class XFetcher:
    name = "x"

    def __init__(self, download_dir: Path | None = None, max_bytes: int = 1024 * 1_048_576, full_text_service: str = ""):
        self.download_dir = download_dir  # None: videos are not downloaded
        self.max_bytes = max_bytes
        self.full_text_service = full_text_service

    def can_handle(self, url: str) -> bool:
        p = urlsplit(url)
        return (p.hostname or "").lower() in HOSTS and bool(_STATUS.match(p.path))

    def fetch(self, url: str) -> FetchResult:
        tweet_id = _STATUS.match(urlsplit(url).path).group(2)
        gone = FetchResult(url=url, source=self.name, status="needs_manual", note=t("fetch.tweet_gone"))
        try:
            resp = http.get(f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}"
                            f"&token={syndication_token(tweet_id)}&lang=zh")
        except FetchError as e:
            if "HTTP 404" in str(e):  # the endpoint answers 404 for deleted / missing tweets
                return gone
            raise
        try:
            data = resp.json()
        except ValueError as e:
            raise FetchError(t("fetch.unexpected"), retryable=False) from e
        if not isinstance(data, dict):
            raise FetchError(t("fetch.unexpected"), retryable=False)
        if not data or data.get("__typename") == "TweetTombstone" or "text" not in data:
            return gone
        user = _dict(data.get("user"))
        status, notes, truncated = "ok", [], False
        text = data["text"].strip()
        if data.get("note_tweet"):
            full = self._full_text(tweet_id) if self.full_text_service == "fxtwitter" else None
            if full:
                text = full
            else:
                status, truncated = "partial", True
                notes.append(t("fetch.long_tweet"))  # always the first note (with_page drops it by place)
        quoted = _dict(data.get("quoted_tweet"))
        if isinstance(quoted.get("text"), str) and quoted["text"].strip():
            quoter = _dict(quoted.get("user")).get("screen_name", "")
            text += "\n\n" + t("note.quoted", user=quoter, text=quoted["text"].strip())
        videos: list[Path] = []
        numbers: list[int] = []
        urls = video_urls(data) if self.download_dir is not None else []
        if len(urls) > MAX_VIDEOS:  # partial: the reply then says some were skipped (an ok save shows no note)
            urls = urls[:MAX_VIDEOS]
            status = "partial"
            notes.append(t("fetch.too_many_videos", n=MAX_VIDEOS))
        try:
            for n, video_url in enumerate(urls, start=1):
                check_cancelled()  # the job was cancelled: the rest is not downloaded for nobody
                try:
                    videos.append(http.download(video_url, self.download_dir, max_bytes=self.max_bytes,
                                                follow_redirects=False))
                    numbers.append(n)
                except FetchError as e:  # the text is still worth saving: partial, with why
                    status = "partial"
                    notes.append(t("fetch.video_failed", error=e))
        except BaseException:
            for path in videos:
                path.unlink(missing_ok=True)
            raise
        return FetchResult(url=url, source=self.name, author=f"{user.get('name', '')} (@{user.get('screen_name', '')})",
                           text=text, language=data.get("lang"), status=status,
                           note=t("common.note_sep").join(dict.fromkeys(notes)) or None, video_paths=videos,
                           video_numbers=numbers, truncated=truncated)

    def with_page(self, fetched: FetchResult, page: FetchResult) -> FetchResult:
        """A fetch of a link the extension also sent the page of (P29): the page's text is the post's when it has more
        than the fetch could get: a long post's first 280 characters (`truncated`) and a longer page, or a fetch with
        under MIN_TEXT characters of its own (links left out: an X Article is only a t.co link, a thread's first post
        is short) and a page with MIN_TEXT or more. Everything else of the fetch stays (videos, author, language, the
        other notes)."""
        page_text = page.text.strip()
        if fetched.truncated:
            wins = len(page_text) > len(fetched.text.strip())
        else:
            wins = len(_LINK.sub("", fetched.text).strip()) < MIN_TEXT <= len(_LINK.sub("", page_text).strip())
        if not wins:
            return fetched
        fetched.text = page.text
        if fetched.truncated:  # the long-post hint is the first note (fetch): taken off whole, the rest stays
            rest = (fetched.note or "").removeprefix(t("fetch.long_tweet")).removeprefix(t("common.note_sep"))
            fetched.truncated, fetched.note = False, rest or None
            if not rest:
                fetched.status = "ok"
        return fetched

    def _full_text(self, tweet_id: str) -> str | None:
        """A long post's whole text from FxTwitter (opt-in); None when it cannot be had (the caller falls back)."""
        try:  # a third-party service: whatever it answers, the post is saved (with the hint, as before)
            data = http.get(FXTWITTER.format(id=tweet_id), follow_redirects=False).json()
            post = data.get("status") if isinstance(data, dict) and data.get("code") == 200 else None
            text = post.get("text") if isinstance(post, dict) else None
        except Exception as e:
            log.info("x: no full text from FxTwitter (%s)", type(e).__name__)
            return None
        return text.strip() if isinstance(text, str) and text.strip() else None
