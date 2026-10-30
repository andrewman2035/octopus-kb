"""Fetcher registry and platform tiers."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from octopus.fetchers.base import Fetcher, FetchError, FetchResult

if TYPE_CHECKING:
    from octopus.config import MediaConfig

# Platforms with no automated path: ask the user for screenshot / recording / pasted text.
MANUAL = {
    "reddit.com": "reddit",
    "redd.it": "reddit",
    "channels.weixin.qq.com": "wechat_channels",
}


# Platforms whose content needs the browser the user is logged in to (P36): fetched here they give a login wall, a
# 403 or a part only (or, Reddit, are never fetched), while the Chrome extension sends the page as the user sees it.
# A link of theirs that ends partial or needs_manual gets the extension's line in the reply. WeChat Channels is not
# here: a video the extension cannot read either. README's "Which platforms need the extension" table lists these
# (by name, without the short domains) and the video platforms whose page gives text only (not here: no line).
BROWSER = (
    "reddit.com", "redd.it",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn",  # the iOS app shares on xhslink.cn (P37)
    "zhihu.com",  # zhuanlan.zhihu.com too
    "instagram.com", "instagr.am", "facebook.com", "fb.com", "fb.watch", "linkedin.com", "lnkd.in",
    "threads.net", "threads.com",
    "medium.com",  # member-only stories (a publication on its own domain is not known as Medium)
    "weibo.com", "weibo.cn", "t.cn",
)


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().split(":")[0].rstrip(".")  # "reddit.com." is reddit.com


def manual_source(url: str) -> str | None:
    host = _host(url)
    if host == "weixin.qq.com" and urlsplit(url).path.startswith("/sph"):
        return "wechat_channels"
    for domain, source in MANUAL.items():
        if host == domain or host.endswith("." + domain):
            return source
    return None


def needs_browser(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith("." + domain) for domain in BROWSER)


def is_manual(url: str) -> bool:
    return manual_source(url) is not None


class FetcherRegistry:
    def __init__(self, fetchers: list[Fetcher]):
        self.fetchers = list(fetchers)

    def add_first(self, fetcher: Fetcher) -> None:
        self.fetchers.insert(0, fetcher)

    def pick(self, url: str) -> Fetcher | None:
        if is_manual(url):
            return None
        return next((f for f in self.fetchers if f.can_handle(url)), None)


def default_registry(downloads_dir: Path | None = None, media: MediaConfig | None = None) -> FetcherRegistry:
    from octopus.fetchers.web import WebFetcher
    from octopus.fetchers.wechat_mp import WeChatMPFetcher

    fetchers: list[Fetcher] = []
    if downloads_dir is not None:
        from octopus.config import MediaConfig
        from octopus.fetchers.bilibili import BilibiliFetcher
        from octopus.fetchers.pdf_url import PDFFetcher
        from octopus.fetchers.podcasts import ApplePodcastsFetcher, DirectAudioFetcher, XiaoyuzhouFetcher
        from octopus.fetchers.x import XFetcher
        from octopus.fetchers.youtube import YouTubeFetcher

        media = media or MediaConfig()
        cap = media.max_download_mb * 1_048_576  # max_download_mb is MiB
        fetchers = [
            XiaoyuzhouFetcher(downloads_dir, cap), ApplePodcastsFetcher(downloads_dir, cap),
            YouTubeFetcher(downloads_dir, media.youtube_cookies_from_browser, max_bytes=cap),
            BilibiliFetcher(downloads_dir, max_bytes=cap),
            XFetcher(downloads_dir, cap, media.x_full_text_service), PDFFetcher(downloads_dir, cap),
            DirectAudioFetcher(downloads_dir, cap),
        ]
    wechat = WeChatMPFetcher(downloads_dir, cap) if downloads_dir is not None else WeChatMPFetcher()
    return FetcherRegistry([*fetchers, wechat, WebFetcher()])


__all__ = ["Fetcher", "FetchError", "FetchResult", "FetcherRegistry", "default_registry", "is_manual", "manual_source",
           "needs_browser"]
