import json

import httpx
import pytest
import respx

from octopus.fetchers.base import FetchError
from octopus.fetchers.podcasts import ApplePodcastsFetcher, DirectAudioFetcher, XiaoyuzhouFetcher

LOOKUP = "https://itunes.apple.com/lookup"
APPLE = "https://podcasts.apple.com/us/podcast/acquired/id1050462261?i=1000789467893"


@respx.mock
def test_apple_episode(tmp_path):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json={"results": [
        {"kind": "podcast", "collectionName": "Acquired", "feedUrl": "https://feeds.example.com/acq"},
        {"kind": "podcast-episode", "trackId": 1000789467893, "trackName": "The Home Depot",
         "collectionName": "Acquired", "episodeUrl": "https://pscrb.fm/ep.mp3", "description": "节目介绍"},
    ]}))
    respx.get("https://pscrb.fm/ep.mp3").mock(return_value=httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"}))
    f = ApplePodcastsFetcher(tmp_path, max_bytes=10_000)
    assert f.can_handle(APPLE)
    r = f.fetch(APPLE)
    assert (r.source, r.title, r.author, r.media_url, r.text) == ("podcast", "The Home Depot", "Acquired", "https://pscrb.fm/ep.mp3", "节目介绍")
    assert r.audio_path.read_bytes() == b"ID3"


def apple_page(title: str, guid: str, audio: str | None = None) -> str:
    """Trimmed excerpt of a real episode page (storefront cn, 2026-10): og:title and <title> are in the head;
    the episode lives in the `serialized-server-data` blob (data[0].data.title / first shelf item)."""
    item = {"title": title, "releaseDate": "2026-09-14T03:35:03Z",
            "contextAction": {"episodeOffer": {"guid": guid, "title": title}},
            "mediaEnclosures": [{"streamUrl": audio}] if audio else []}
    blob = {"data": [{"intent": {"storefront": "cn"}, "data": {"title": title, "shelves": [{"items": [item]}]}}],
            "userTokenHash": "x"}
    return (f"<html><head><title>{title} - Acquired - Apple 播客</title>"
            f'<meta property="og:title" content="{title}"></head><body>'
            f'<script type="application/json" id="serialized-server-data">{json.dumps(blob)}</script></body></html>')


LANDING = ('<html><head><title>Apple 播客（CN）- 网页播放器</title>'
           '<meta property="og:title" content="Apple 播客网页播放器"></head><body>'
           '<script type="application/json" id="serialized-server-data">'
           '{"data": [{"data": {"shelves": [{"items": [{"showAdamId": "1"}]}]}}], "userTokenHash": "x"}</script></body></html>')
HTML = {"content-type": "text/html"}
FEED = "https://feeds.example.com/acq"


def rss(title: str, guid: str) -> str:
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Acquired</title>'
            "<item><title>Other</title><guid>zzz</guid><enclosure url=\"https://cdn.example.com/other.mp3\" type=\"audio/mpeg\" length=\"3\"/></item>"
            f'<item><title>{title}</title><guid>{guid}</guid><enclosure url="https://cdn.example.com/old.mp3" type="audio/mpeg" length="3"/></item>'
            "</channel></rss>")


def mock_feed_episode(title: str = "Old Episode"):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json={"results": [
        {"kind": "podcast", "collectionName": "Acquired", "feedUrl": FEED}]}))
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(title, "g-1"), headers={"content-type": "application/rss+xml"}))
    respx.get("https://cdn.example.com/old.mp3").mock(return_value=httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"}))


@respx.mock
def test_apple_episode_found_via_rss_when_not_in_lookup(tmp_path):
    mock_feed_episode()
    respx.get(APPLE).mock(return_value=httpx.Response(200, text=apple_page("Old Episode", "g-1"), headers=HTML))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert (r.title, r.media_url) == ("Old Episode", "https://cdn.example.com/old.mp3")


@respx.mock
def test_apple_rss_match_by_guid_when_titles_differ(tmp_path):
    mock_feed_episode(title="Old Episode (re-titled in feed)")
    respx.get(APPLE).mock(return_value=httpx.Response(200, text=apple_page("Old Episode", "g-1"), headers=HTML))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert r.media_url == "https://cdn.example.com/old.mp3"


@respx.mock
def test_apple_landing_page_retries_other_storefront(tmp_path):
    """From some IPs /us/ pages are redirected to the generic player landing (og:title 'Apple 播客网页播放器', the localized web-player title)."""
    mock_feed_episode()
    respx.get(APPLE).mock(return_value=httpx.Response(200, text=LANDING, headers=HTML))
    cn = APPLE.replace("/us/", "/cn/")
    respx.get(cn).mock(return_value=httpx.Response(200, text=apple_page("Old Episode", "g-1"), headers=HTML))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert r.media_url == "https://cdn.example.com/old.mp3"


@respx.mock
def test_apple_no_rss_match_uses_stream_url_from_page(tmp_path):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json={"results": [
        {"kind": "podcast", "collectionName": "Acquired", "feedUrl": FEED}]}))
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss("Unrelated", "zzz-0"), headers={"content-type": "application/rss+xml"}))
    respx.get(APPLE).mock(return_value=httpx.Response(200, text=apple_page("Old Episode", "g-9", "https://cdn.example.com/page.mp3"), headers=HTML))
    respx.get("https://cdn.example.com/page.mp3").mock(return_value=httpx.Response(200, content=b"ID3"))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert (r.title, r.media_url) == ("Old Episode", "https://cdn.example.com/page.mp3")


@respx.mock
def test_apple_only_landing_pages_means_needs_manual(tmp_path):
    mock_feed_episode()
    for u in (APPLE, APPLE.replace("/us/", "/cn/")):
        respx.get(u).mock(return_value=httpx.Response(200, text=LANDING, headers=HTML))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert r.status == "needs_manual" and r.audio_path is None


@respx.mock
def test_apple_lookup_episode_without_url_falls_back_to_rss(tmp_path):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, json={"results": [
        {"kind": "podcast", "collectionName": "Acquired", "feedUrl": FEED},
        {"kind": "podcast-episode", "trackId": 1000789467893, "trackName": "Old Episode", "description": "d"}]}))
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss("Old Episode", "g-1"), headers={"content-type": "application/rss+xml"}))
    respx.get(APPLE).mock(return_value=httpx.Response(200, text=apple_page("Old Episode", "g-1"), headers=HTML))
    respx.get("https://cdn.example.com/old.mp3").mock(return_value=httpx.Response(200, content=b"ID3"))
    r = ApplePodcastsFetcher(tmp_path, max_bytes=10_000).fetch(APPLE)
    assert (r.title, r.text, r.media_url) == ("Old Episode", "d", "https://cdn.example.com/old.mp3")


@respx.mock
def test_apple_lookup_returning_html_is_a_clear_error(tmp_path):
    respx.get(url__startswith=LOOKUP).mock(return_value=httpx.Response(200, text="<html>captcha</html>", headers=HTML))
    with pytest.raises(FetchError, match="不是预期格式") as ei:
        ApplePodcastsFetcher(tmp_path, max_bytes=10).fetch(APPLE)
    assert ei.value.retryable is False


def test_apple_show_page_without_episode(tmp_path):
    with pytest.raises(FetchError, match="具体某一集"):
        ApplePodcastsFetcher(tmp_path, max_bytes=10).fetch("https://podcasts.apple.com/us/podcast/acquired/id1050462261")


def next_data(episode: dict) -> str:
    return ('<html><head><meta property="og:title" content="x"></head><body>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"pageProps": {"episode": episode}}})}</script>'
            "</body></html>")


XYZ = "https://www.xiaoyuzhoufm.com/episode/6512345678901234567890ab"


@respx.mock
def test_xiaoyuzhou_episode(tmp_path):
    ep = {"title": "第 1 期：复利", "description": "简介", "shownotes": "<p>本期<b>聊</b>复利</p>",
          "enclosure": {"url": "https://media.xyzcdn.net/a.m4a"}, "podcast": {"title": "投资节目"},
          "isPrivateMedia": False, "payType": "FREE"}
    respx.get(XYZ).mock(return_value=httpx.Response(200, text=next_data(ep), headers={"content-type": "text/html"}))
    respx.get("https://media.xyzcdn.net/a.m4a").mock(return_value=httpx.Response(200, content=b"m4a", headers={"content-type": "audio/mp4"}))
    f = XiaoyuzhouFetcher(tmp_path, max_bytes=10_000)
    assert f.can_handle(XYZ) and not f.can_handle("https://www.xiaoyuzhoufm.com/podcast/abc")
    r = f.fetch(XYZ)
    assert (r.title, r.author, r.media_url, r.text) == ("第 1 期：复利", "投资节目", "https://media.xyzcdn.net/a.m4a", "本期聊复利")
    assert r.audio_path.suffix == ".m4a"


@respx.mock
def test_xiaoyuzhou_paid_episode_needs_manual(tmp_path):
    ep = {"title": "付费", "enclosure": {"url": "https://media.xyzcdn.net/a.m4a"}, "podcast": {"title": "p"},
          "isPrivateMedia": False, "payType": "PAY_EPISODE"}
    respx.get(XYZ).mock(return_value=httpx.Response(200, text=next_data(ep), headers={"content-type": "text/html"}))
    r = XiaoyuzhouFetcher(tmp_path, max_bytes=10).fetch(XYZ)
    assert r.status == "needs_manual" and "付费" in r.note and r.audio_path is None


@respx.mock
def test_direct_audio_link(tmp_path):
    respx.get("https://cdn.example.com/talk.mp3").mock(return_value=httpx.Response(200, content=b"ID3", headers={"content-type": "audio/mpeg"}))
    f = DirectAudioFetcher(tmp_path, max_bytes=100)
    assert f.can_handle("https://cdn.example.com/talk.mp3?x=1")
    assert not f.can_handle("https://cdn.example.com/page.html")
    r = f.fetch("https://cdn.example.com/talk.mp3")
    assert (r.source, r.title, r.media_url) == ("podcast", "talk.mp3", "https://cdn.example.com/talk.mp3")


@respx.mock
@pytest.mark.parametrize("html,msg", [
    ("<html><body>nothing</body></html>", "找不到节目信息"),
    ("<script id=\"__NEXT_DATA__\" type=\"application/json\">not json</script>", "找不到节目信息"),
    (next_data({"title": "无音频", "podcast": {"title": "p"}}), "找不到音频地址"),
])
def test_xiaoyuzhou_bad_pages(tmp_path, html, msg):
    respx.get(XYZ).mock(return_value=httpx.Response(200, text=html, headers={"content-type": "text/html"}))
    with pytest.raises(FetchError, match=msg) as ei:
        XiaoyuzhouFetcher(tmp_path, max_bytes=10).fetch(XYZ)
    assert ei.value.retryable is False


@respx.mock
def test_failed_download_leaves_dir_empty(tmp_path):
    respx.get("https://cdn.example.com/talk.mp3").mock(return_value=httpx.Response(200, content=b"x" * 50, headers={"content-type": "audio/mpeg"}))
    with pytest.raises(FetchError, match="太大"):
        DirectAudioFetcher(tmp_path, max_bytes=10).fetch("https://cdn.example.com/talk.mp3")
    assert list(tmp_path.iterdir()) == []


def test_direct_audio_link_accepts_aiff(tmp_path):
    f = DirectAudioFetcher(tmp_path, max_bytes=100)
    assert f.can_handle("https://cdn.example.com/voice.aiff")
    assert f.can_handle("https://cdn.example.com/voice.AIF")
