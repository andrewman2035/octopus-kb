import httpx
import pytest
import respx
import yt_dlp.utils

from octopus.fetchers.base import FetchError
from octopus.fetchers.bilibili import BilibiliFetcher
from octopus.fetchers.youtube import YouTubeFetcher, video_id
from octopus.fetchers.ytdlp import BLOCKED_NOTE, download_audio


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behaviour set per test through class attributes."""
    opts = None
    error: Exception | None = None
    info = {"id": "BV1Ab4y1C7dE", "title": "标题", "uploader": "UP主", "duration": 212}

    def __init__(self, opts):
        FakeYDL.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download):
        if FakeYDL.error:
            raise FakeYDL.error
        out = FakeYDL.opts["outtmpl"].replace("%(id)s", self.info["id"]).replace("%(ext)s", "m4a")
        open(out, "wb").write(b"audio")
        return {**self.info, "requested_downloads": [{"filepath": out}]}


@pytest.fixture(autouse=True)
def reset_fake():
    FakeYDL.opts, FakeYDL.error = None, None
    FakeYDL.info = {"id": "BV1Ab4y1C7dE", "title": "标题", "uploader": "UP主", "duration": 212}


def test_download_audio_options(tmp_path):
    info, path = download_audio("https://x", tmp_path, ydl_cls=FakeYDL)
    assert path.exists() and path.parent == tmp_path
    o = FakeYDL.opts
    assert o["format"] == "bestaudio[ext=m4a]/bestaudio/best"
    assert o["postprocessors"] == [] and o["noplaylist"] is True
    assert "cookiesfrombrowser" not in o


def test_download_audio_cookies_opt_in(tmp_path):
    download_audio("https://x", tmp_path, cookies_from_browser="chrome", ydl_cls=FakeYDL)
    assert FakeYDL.opts["cookiesfrombrowser"] == ("chrome",)


def test_download_audio_bot_gate_is_permanent(tmp_path):
    FakeYDL.error = yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot. Use --cookies-from-browser")
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, blocked_note=BLOCKED_NOTE, ydl_cls=FakeYDL)
    assert not ei.value.retryable and str(ei.value) == BLOCKED_NOTE


def test_download_audio_network_error_retryable(tmp_path):
    FakeYDL.error = yt_dlp.utils.DownloadError("ERROR: Unable to download: <urlopen error timed out>")
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, ydl_cls=FakeYDL)
    assert ei.value.retryable


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=x", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/channel/abc", None),
])
def test_video_id(url, vid):
    assert video_id(url) == vid


class Snip:
    def __init__(self, text, start, duration):
        self.text, self.start, self.duration = text, start, duration


class FakeTranscript:
    language_code = "zh-Hans"

    def fetch(self):
        return [Snip("复利", 0.0, 1.0), Snip("需要时间", 1.0, 1.0), Snip("Second", 9.0, 1.0)]


class FakeList:
    def __init__(self, find_ok=True):
        self.find_ok = find_ok

    def find_transcript(self, langs):
        if not self.find_ok:
            raise LookupError("none in preferred languages")
        return FakeTranscript()

    def __iter__(self):
        return iter([FakeTranscript()])


class FakeApi:
    def __init__(self, tl=None, error=None):
        self.tl, self.error = tl, error

    def list(self, vid):
        if self.error:
            raise self.error
        return self.tl


OEMBED = "https://www.youtube.com/oembed"


@respx.mock
def test_youtube_uses_transcript(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "视频标题", "author_name": "频道"}))
    f = YouTubeFetcher(tmp_path, transcript_api=FakeApi(FakeList()), ydl_cls=FakeYDL)
    assert f.can_handle("https://youtu.be/dQw4w9WgXcQ")
    r = f.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert (r.source, r.title, r.author, r.media_id, r.language) == ("youtube", "视频标题", "频道", "dQw4w9WgXcQ", "zh-Hans")
    assert r.text == "复利需要时间\n\nSecond"
    assert r.audio_path is None and FakeYDL.opts is None


@respx.mock
def test_youtube_falls_back_to_any_transcript_language(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t"}))
    r = YouTubeFetcher(tmp_path, transcript_api=FakeApi(FakeList(find_ok=False)), ydl_cls=FakeYDL).fetch(
        "https://youtu.be/dQw4w9WgXcQ")
    assert r.text.startswith("复利")


@respx.mock
def test_youtube_downloads_audio_when_no_transcript(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t", "author_name": "a"}))
    FakeYDL.info = {"id": "dQw4w9WgXcQ", "title": "yt title", "uploader": "up"}
    r = YouTubeFetcher(tmp_path, transcript_api=FakeApi(error=RuntimeError("RequestBlocked")), ydl_cls=FakeYDL).fetch(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert r.audio_path is not None and r.audio_path.exists()
    assert (r.title, r.author, r.text) == ("t", "a", "")


@respx.mock
def test_youtube_blocked_everywhere(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(401))
    FakeYDL.error = yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")
    with pytest.raises(FetchError) as ei:
        YouTubeFetcher(tmp_path, transcript_api=FakeApi(error=RuntimeError("blocked")), ydl_cls=FakeYDL).fetch(
            "https://youtu.be/dQw4w9WgXcQ")
    assert "youtube_cookies_from_browser" in str(ei.value)


def test_youtube_without_video_id(tmp_path):
    with pytest.raises(FetchError):
        YouTubeFetcher(tmp_path, transcript_api=FakeApi(FakeList()), ydl_cls=FakeYDL).fetch("https://www.youtube.com/channel/x")


@respx.mock
def test_bilibili_downloads_audio(tmp_path):
    f = BilibiliFetcher(tmp_path, ydl_cls=FakeYDL)
    assert f.can_handle("https://www.bilibili.com/video/BV1Ab4y1C7dE?spm_id_from=x")
    assert f.can_handle("https://b23.tv/AbCdEf")
    assert not f.can_handle("https://www.bilibili.com/read/cv1")
    r = f.fetch("https://www.bilibili.com/video/BV1Ab4y1C7dE")
    assert (r.source, r.title, r.author, r.media_id) == ("bilibili", "标题", "UP主", "BV1Ab4y1C7dE")
    assert r.audio_path.exists()


@respx.mock
def test_bilibili_short_link_resolved(tmp_path):
    respx.head("https://b23.tv/AbCdEf").mock(return_value=httpx.Response(
        302, headers={"location": "https://www.bilibili.com/video/BV1Ab4y1C7dE?share_source=copy"}))
    respx.head("https://www.bilibili.com/video/BV1Ab4y1C7dE?share_source=copy").mock(return_value=httpx.Response(200))
    r = BilibiliFetcher(tmp_path, ydl_cls=FakeYDL).fetch("https://b23.tv/AbCdEf")
    assert r.media_id == "BV1Ab4y1C7dE"
    assert r.url == "https://www.bilibili.com/video/BV1Ab4y1C7dE"


# ---- fix round 1 ----
LOGIN_NOTE = "这个视频需要登录或会员才能看，没法自动下载。可以发截图或录屏。"
COOKIES_STILL_BLOCKED = "YouTube 还是拦截了访问（已使用浏览器登录状态）。可以发截图或录屏。"
BOT = "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies"


def test_bilibili_login_required_is_generic_note(tmp_path):
    FakeYDL.error = yt_dlp.utils.DownloadError(
        "ERROR: [BiliBili] BV1: This video is premium-only. Use --cookies-from-browser or --cookies for the authentication")
    with pytest.raises(FetchError) as ei:
        BilibiliFetcher(tmp_path, ydl_cls=FakeYDL).fetch("https://www.bilibili.com/video/BV1Ab4y1C7dE")
    assert str(ei.value) == LOGIN_NOTE and not ei.value.retryable
    assert "youtube" not in str(ei.value).lower()


def test_age_gate_is_login_note_not_youtube_block(tmp_path):
    FakeYDL.error = yt_dlp.utils.DownloadError(
        "ERROR: Sign in to confirm your age. Use --cookies-from-browser for the authentication")
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, blocked_note=BLOCKED_NOTE, ydl_cls=FakeYDL)
    assert str(ei.value) == LOGIN_NOTE and not ei.value.retryable


def test_bot_gate_default_note_is_generic_login(tmp_path):
    FakeYDL.error = yt_dlp.utils.DownloadError(BOT)
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, ydl_cls=FakeYDL)
    assert not ei.value.retryable and "youtube" not in str(ei.value).lower()


@respx.mock
@pytest.mark.parametrize("cookies,note", [("", BLOCKED_NOTE), ("chrome", COOKIES_STILL_BLOCKED)])
def test_youtube_bot_gate_notes(tmp_path, cookies, note):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(401))
    FakeYDL.error = yt_dlp.utils.DownloadError(BOT)
    f = YouTubeFetcher(tmp_path, cookies_from_browser=cookies, transcript_api=FakeApi(error=RuntimeError("b")),
                       ydl_cls=FakeYDL)
    with pytest.raises(FetchError) as ei:
        f.fetch("https://youtu.be/dQw4w9WgXcQ")
    assert str(ei.value) == note and not ei.value.retryable


@pytest.mark.parametrize("msg", [
    "ERROR: Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided, or not known>",
    "ERROR: HTTP Error 429: Too Many Requests",
    "ERROR: Connection refused",
    "ERROR: Network is unreachable",
    "ERROR: Remote end closed connection without response",
])
def test_transient_errors_retryable(tmp_path, msg):
    FakeYDL.error = yt_dlp.utils.DownloadError(msg)
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, ydl_cls=FakeYDL)
    assert ei.value.retryable


def test_download_audio_limits(tmp_path):
    download_audio("https://x", tmp_path, max_bytes=1234, ydl_cls=FakeYDL)
    assert FakeYDL.opts["max_filesize"] == 1234
    flt = FakeYDL.opts["match_filter"]
    assert flt({"id": "a", "is_live": True}) is not None  # skipped (returns a reason)
    assert flt({"id": "a", "is_live": False}) is None


def test_download_audio_no_size_limit_by_default(tmp_path):
    download_audio("https://x", tmp_path, ydl_cls=FakeYDL)
    assert "max_filesize" not in FakeYDL.opts and "match_filter" in FakeYDL.opts


class SkippedYDL(FakeYDL):
    def extract_info(self, url, download):
        return {**self.info, "requested_downloads": []}


class LiveYDL(FakeYDL):
    def extract_info(self, url, download):
        return {**self.info, "is_live": True, "requested_downloads": [{"filepath": str(url)}]}


@pytest.mark.parametrize("cls", [SkippedYDL, LiveYDL])
def test_download_audio_live_or_too_big(tmp_path, cls):
    with pytest.raises(FetchError) as ei:
        download_audio("https://x", tmp_path, ydl_cls=cls)
    assert str(ei.value) == "这是直播或文件太大，没法自动下载" and not ei.value.retryable


def test_max_bytes_plumbed(tmp_path):
    BilibiliFetcher(tmp_path, ydl_cls=FakeYDL, max_bytes=99).fetch("https://www.bilibili.com/video/BV1Ab4y1C7dE")
    assert FakeYDL.opts["max_filesize"] == 99


@respx.mock
def test_youtube_cookies_and_max_bytes_plumbed(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t"}))
    YouTubeFetcher(tmp_path, cookies_from_browser="chrome", max_bytes=77, transcript_api=FakeApi(error=RuntimeError("b")),
                   ydl_cls=FakeYDL).fetch("https://youtu.be/dQw4w9WgXcQ")
    assert FakeYDL.opts["cookiesfrombrowser"] == ("chrome",) and FakeYDL.opts["max_filesize"] == 77


def test_bilibili_never_uses_cookies(tmp_path):
    BilibiliFetcher(tmp_path, ydl_cls=FakeYDL).fetch("https://www.bilibili.com/video/BV1Ab4y1C7dE")
    assert "cookiesfrombrowser" not in FakeYDL.opts


class TradTranscript:
    language_code = "zh-Hant"

    def fetch(self):
        return [Snip("學習複利", 0.0, 1.0), Snip("需要時間", 1.0, 1.0)]


class TradList(FakeList):
    def find_transcript(self, langs):
        return TradTranscript()


class JaTranscript:
    language_code = "ja"

    def fetch(self):
        return [Snip("学習の国", 0.0, 1.0)]


class JaList(FakeList):
    def find_transcript(self, langs):
        return JaTranscript()


@respx.mock
def test_youtube_traditional_captions_simplified(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t"}))
    r = YouTubeFetcher(tmp_path, transcript_api=FakeApi(TradList()), ydl_cls=FakeYDL).fetch("https://youtu.be/dQw4w9WgXcQ")
    assert r.text == "学习复利需要时间" and r.language == "zh-Hant"


@respx.mock
def test_youtube_japanese_captions_untouched(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t"}))
    r = YouTubeFetcher(tmp_path, transcript_api=FakeApi(JaList()), ydl_cls=FakeYDL).fetch("https://youtu.be/dQw4w9WgXcQ")
    assert r.text == "学習の国"


class NoisyTranscript:
    language_code = "en"

    def fetch(self):
        return [Snip("[Music]", 0.0, 1.0), Snip(">> Hello\nthere   friend", 1.0, 1.0), Snip("(applause)", 2.0, 1.0),
                Snip("[音乐]", 2.2, 0.1), Snip(">>", 2.3, 0.1), Snip("Bye", 2.5, 1.0)]


class NoisyList(FakeList):
    def find_transcript(self, langs):
        return NoisyTranscript()


@respx.mock
def test_youtube_caption_cleanup(tmp_path):
    respx.get(url__startswith=OEMBED).mock(return_value=httpx.Response(200, json={"title": "t"}))
    r = YouTubeFetcher(tmp_path, transcript_api=FakeApi(NoisyList()), ydl_cls=FakeYDL).fetch("https://youtu.be/dQw4w9WgXcQ")
    assert r.text == "Hello there friend Bye"


@respx.mock
def test_bilibili_short_link_keeps_part_param(tmp_path):
    respx.head("https://b23.tv/AbCdEf").mock(return_value=httpx.Response(
        302, headers={"location": "https://www.bilibili.com/video/BV1Ab4y1C7dE?p=3&share_source=copy"}))
    respx.head("https://www.bilibili.com/video/BV1Ab4y1C7dE?p=3&share_source=copy").mock(return_value=httpx.Response(200))
    r = BilibiliFetcher(tmp_path, ydl_cls=FakeYDL).fetch("https://b23.tv/AbCdEf")
    assert r.url == "https://www.bilibili.com/video/BV1Ab4y1C7dE?p=3"
