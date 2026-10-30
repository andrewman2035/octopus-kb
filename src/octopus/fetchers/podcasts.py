"""Podcasts: Apple Podcasts (iTunes lookup / RSS), Xiaoyuzhou (__NEXT_DATA__), direct audio links."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import feedparser
from bs4 import BeautifulSoup

from octopus.fetchers import http
from octopus.fetchers.base import FetchError, FetchResult
from octopus.i18n import t
from octopus.media import AUDIO_SUFFIXES

SAFARI_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.6 Safari/605.1.15")
_SHOW_ID = re.compile(r"/id(\d+)")
_SERVER_DATA = re.compile(r'<script type="application/json" id="serialized-server-data">(.*?)</script>', re.S)
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _html_text(html: str) -> str:
    return BeautifulSoup(html or "", "lxml").get_text("", strip=True)


def _norm(s: str) -> str:
    return re.sub(r"\W+", "", s or "").lower()


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError as e:
        raise FetchError(t("fetch.unexpected"), retryable=False) from e
    if not isinstance(data, dict):
        raise FetchError(t("fetch.unexpected"), retryable=False)
    return data


def _page_episode(html: str) -> dict:
    """Episode facts from the page's `serialized-server-data` JSON blob.

    Real episode pages carry the episode under data[0].data (title) and its first shelf item
    (releaseDate, contextAction.episodeOffer.guid, mediaEnclosures[0].streamUrl). The page's og:title is
    NOT reliable: storefront-redirected pages are the generic player landing (its title is the localized
    "Apple Podcasts web player").
    Returns {} when the page is not an episode page.
    """
    m = _SERVER_DATA.search(html)
    try:
        page = json.loads(m.group(1))["data"][0]["data"]
        item = page["shelves"][0]["items"][0]
        title = page.get("title")
        if not title:
            return {}
        offer = (item.get("contextAction") or {}).get("episodeOffer") or {}
        enclosure = (item.get("mediaEnclosures") or [{}])[0]
        return {"title": title, "guid": offer.get("guid"), "audio": enclosure.get("streamUrl")}
    except (AttributeError, ValueError, KeyError, IndexError, TypeError):
        return {}


class ApplePodcastsFetcher:
    name = "podcast"

    def __init__(self, download_dir: Path, max_bytes: int):
        self.download_dir = download_dir
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() == "podcasts.apple.com"

    def _page_info(self, page_url: str) -> dict:
        """The page may be redirected to a generic landing by IP geolocation: retry other storefronts."""
        parts = urlsplit(page_url)
        segs = parts.path.split("/")  # ['', 'us', 'podcast', ...]
        tried = [page_url]
        for store in ("cn", "us"):
            if len(segs) > 2 and segs[1] != store:
                tried.append(parts._replace(path="/".join([segs[0], store, *segs[2:]])).geturl())
        for candidate in tried:
            info = _page_episode(http.get(candidate).text)
            if info:
                return info
        return {}

    def _from_rss(self, feed_url: str, page_url: str) -> tuple[str | None, str | None, str]:
        info = self._page_info(page_url)
        wanted, guid = _norm(info.get("title") or ""), info.get("guid")
        feed = feedparser.parse(http.get(feed_url).content)
        for e in feed.entries:
            if not e.get("enclosures"):
                continue
            if (guid and e.get("id") == guid) or (wanted and _norm(e.get("title", "")) == wanted):
                return e.enclosures[0].get("href"), e.get("title"), _html_text(e.get("summary", ""))
        return info.get("audio"), info.get("title"), ""

    def fetch(self, url: str) -> FetchResult:
        parts = urlsplit(url)
        show = _SHOW_ID.search(parts.path)
        episode = parse_qs(parts.query).get("i", [None])[0]
        if not show or not episode:
            raise FetchError(t("fetch.podcast_show"), retryable=False)
        data = _json(http.get(f"https://itunes.apple.com/lookup?id={show.group(1)}&entity=podcastEpisode&limit=200"))
        results = data.get("results", [])
        show_rec = next((r for r in results if r.get("kind") == "podcast"), {})
        ep = next((r for r in results if r.get("kind") == "podcast-episode" and str(r.get("trackId")) == episode), None)
        audio, title, desc = None, None, ""
        if ep:
            audio, title, desc = ep.get("episodeUrl"), ep.get("trackName"), ep.get("description") or ""
        if not audio and show_rec.get("feedUrl"):
            rss_audio, rss_title, rss_desc = self._from_rss(show_rec["feedUrl"], url)
            audio, title, desc = rss_audio, title or rss_title, desc or rss_desc
        author = show_rec.get("collectionName") or (ep or {}).get("collectionName")
        if not audio:
            return FetchResult(url=url, source=self.name, title=title, author=author, status="needs_manual",
                               note=t("fetch.no_episode_audio"))
        path = http.download(audio, self.download_dir, max_bytes=self.max_bytes)
        return FetchResult(url=url, source=self.name, title=title, author=author, text=desc.strip(),
                           media_url=audio, audio_path=path)


class XiaoyuzhouFetcher:
    name = "podcast"

    def __init__(self, download_dir: Path, max_bytes: int):
        self.download_dir = download_dir
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        p = urlsplit(url)
        return (p.hostname or "").lower() in ("www.xiaoyuzhoufm.com", "xiaoyuzhoufm.com") and p.path.startswith("/episode/")

    def fetch(self, url: str) -> FetchResult:
        html = http.get(url, ua=SAFARI_UA).text
        m = _NEXT_DATA.search(html)
        try:
            ep = json.loads(m.group(1))["props"]["pageProps"]["episode"] if m else None
        except (ValueError, KeyError, TypeError):
            ep = None
        if not ep:
            raise FetchError(t("fetch.xyz_no_episode"), retryable=False)
        title, author = ep.get("title"), (ep.get("podcast") or {}).get("title")
        if ep.get("isPrivateMedia") or ep.get("payType") not in (None, "FREE"):
            return FetchResult(url=url, source=self.name, title=title, author=author, status="needs_manual",
                               note=t("fetch.xyz_paid"))
        audio = (ep.get("enclosure") or {}).get("url")
        if not audio:
            raise FetchError(t("fetch.xyz_no_audio"), retryable=False)
        notes = _html_text(ep.get("shownotes") or "") or (ep.get("description") or "")
        path = http.download(audio, self.download_dir, max_bytes=self.max_bytes, ua=SAFARI_UA)
        return FetchResult(url=url, source=self.name, title=title, author=author, text=notes.strip(),
                           media_url=audio, audio_path=path)


class DirectAudioFetcher:
    name = "podcast"

    def __init__(self, download_dir: Path, max_bytes: int):
        self.download_dir = download_dir
        self.max_bytes = max_bytes

    def can_handle(self, url: str) -> bool:
        return urlsplit(url).path.lower().endswith(AUDIO_SUFFIXES)

    def fetch(self, url: str) -> FetchResult:
        path = http.download(url, self.download_dir, max_bytes=self.max_bytes)
        name = unquote(Path(urlsplit(url).path).name)
        return FetchResult(url=url, source=self.name, title=name, media_url=url, audio_path=path)
