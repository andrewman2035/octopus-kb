import httpx
import pytest
import respx

from octopus.fetchers import http
from octopus.fetchers.base import FetchError


@respx.mock
def test_get_rejects_oversized_body():
    respx.get("https://a.com/big").mock(return_value=httpx.Response(200, content=b"x" * 2000))
    with pytest.raises(FetchError) as ei:
        http.get("https://a.com/big", max_bytes=1000)
    assert not ei.value.retryable
    assert "太大" in str(ei.value)


@respx.mock
def test_get_returns_text_with_encoding():
    respx.get("https://a.com/p").mock(return_value=httpx.Response(
        200, content="复利".encode("utf-8"), headers={"content-type": "text/html; charset=utf-8"}))
    r = http.get("https://a.com/p")
    assert r.text == "复利"
    assert r.headers["content-type"].startswith("text/html")


@respx.mock
def test_resolve_follows_redirects():
    respx.head("https://b23.tv/x").mock(return_value=httpx.Response(302, headers={"location": "https://www.bilibili.com/video/BV1Ab4y1C7dE?share=1"}))
    respx.head("https://www.bilibili.com/video/BV1Ab4y1C7dE?share=1").mock(return_value=httpx.Response(200))
    assert http.resolve("https://b23.tv/x") == "https://www.bilibili.com/video/BV1Ab4y1C7dE?share=1"


@respx.mock
def test_download_streams_to_file(tmp_path):
    respx.get("https://cdn.a.com/ep.mp3").mock(return_value=httpx.Response(
        200, content=b"ID3" + b"0" * 100, headers={"content-type": "audio/mpeg"}))
    path = http.download("https://cdn.a.com/ep.mp3", tmp_path, max_bytes=10_000)
    assert path.parent == tmp_path and path.suffix == ".mp3"
    assert path.read_bytes().startswith(b"ID3")


@respx.mock
def test_download_uses_content_type_when_no_suffix(tmp_path):
    respx.get("https://cdn.a.com/media?id=1").mock(return_value=httpx.Response(
        200, content=b"x", headers={"content-type": "audio/mp4"}))
    assert http.download("https://cdn.a.com/media?id=1", tmp_path, max_bytes=10).suffix == ".m4a"


@respx.mock
def test_download_too_big_removes_partial_file(tmp_path):
    respx.get("https://cdn.a.com/ep.mp3").mock(return_value=httpx.Response(200, content=b"0" * 5000))
    with pytest.raises(FetchError) as ei:
        http.download("https://cdn.a.com/ep.mp3", tmp_path, max_bytes=1000)
    assert not ei.value.retryable
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_download_retryable_on_5xx(tmp_path):
    respx.get("https://cdn.a.com/ep.mp3").mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError) as ei:
        http.download("https://cdn.a.com/ep.mp3", tmp_path, max_bytes=1000)
    assert ei.value.retryable


@respx.mock
def test_resolve_falls_back_to_get_when_head_rejected():
    respx.head("https://b23.tv/y").mock(return_value=httpx.Response(405))
    respx.get("https://b23.tv/y").mock(return_value=httpx.Response(302, headers={"location": "https://www.bilibili.com/video/BV1"}))
    respx.get("https://www.bilibili.com/video/BV1").mock(return_value=httpx.Response(200, content=b"<html>"))
    assert http.resolve("https://b23.tv/y") == "https://www.bilibili.com/video/BV1"


@respx.mock
def test_resolve_get_fallback_still_checks_status():
    respx.head("https://b23.tv/z").mock(return_value=httpx.Response(405))
    respx.get("https://b23.tv/z").mock(return_value=httpx.Response(404))
    with pytest.raises(FetchError) as ei:
        http.resolve("https://b23.tv/z")
    assert not ei.value.retryable


@respx.mock
def test_size_message_uses_real_cap_in_mib(tmp_path):
    respx.get("https://a.com/big").mock(return_value=httpx.Response(200, content=b"x" * 3_000_000))
    with pytest.raises(FetchError) as ei:
        http.get("https://a.com/big", max_bytes=2 * 1_048_576)
    assert "2.0 MB" in str(ei.value)
    respx.get("https://a.com/huge").mock(return_value=httpx.Response(200, content=b"x" * 20))
    with pytest.raises(FetchError) as ei:
        http.download("https://a.com/huge", tmp_path, max_bytes=10)
    msg = str(ei.value)
    assert "太大" in msg and "10 B" in msg
    assert "0 MB" not in msg and "0.0 MB" not in msg
    assert http._fmt_size(500_000) == "488 KB"
    assert http._fmt_size(1024 * 1_048_576) == "1024 MB"
    assert http._fmt_size(1_048_575) == "1.0 MB"  # P2 deferred minor: was "1024 KB"
    assert http._fmt_size(1_047_552) == "1023 KB"


@respx.mock
@pytest.mark.parametrize("ctype", ["audio/aiff", "audio/x-aiff"])
def test_download_knows_aiff_by_content_type(tmp_path, ctype):
    respx.get("https://cdn.a.com/media?id=2").mock(return_value=httpx.Response(
        200, content=b"x", headers={"content-type": ctype}))
    assert http.download("https://cdn.a.com/media?id=2", tmp_path, max_bytes=10).suffix == ".aiff"


@respx.mock
def test_download_prefers_content_type_for_unknown_url_suffix(tmp_path):
    respx.get("https://cdn.a.com/m.php").mock(return_value=httpx.Response(
        200, content=b"x", headers={"content-type": "audio/mpeg"}))
    assert http.download("https://cdn.a.com/m.php", tmp_path, max_bytes=10).suffix == ".mp3"


@respx.mock
def test_download_oserror_mid_write_removes_partial_file(tmp_path, monkeypatch):
    from pathlib import Path

    def chunks():
        yield b"aaaa"
        yield b"bbbb"

    respx.get("https://cdn.a.com/ep.mp3").mock(return_value=httpx.Response(200, content=chunks()))
    real_open = Path.open

    class FailingSecondWrite:
        def __init__(self, f):
            self.f, self.calls = f, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, data):
            self.calls += 1
            if self.calls == 2:
                raise OSError("disk full")
            return self.f.write(data)

    seen = {}

    def fake_open(self, *a, **k):
        seen["w"] = FailingSecondWrite(real_open(self, *a, **k))
        return seen["w"]

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(FetchError) as ei:
        http.download("https://cdn.a.com/ep.mp3", tmp_path, max_bytes=100)
    assert seen["w"].calls == 2  # file existed with the first chunk written before the failure
    assert not ei.value.retryable and "保存文件失败" in str(ei.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("host", ["en.wikipedia.org", "commons.wikimedia.org", "www.wikidata.org", "wikipedia.org"])
def test_wikimedia_hosts_get_the_descriptive_bot_ua(host):
    assert http.user_agent_for(f"https://{host}/wiki/Octopus", http.DESKTOP_UA) == http.BOT_UA
    assert http.BOT_UA.startswith("Octopus/") and "personal knowledge base" in http.BOT_UA
    assert "https://github.com/andrewman2035/octopus-kb" in http.BOT_UA  # where a Wikimedia admin can look us up


@pytest.mark.parametrize("url", ["https://example.com/wiki", "https://notwikipedia.org/x", "https://wikipedia.org.evil.com/x"])
def test_other_hosts_keep_the_given_ua(url):
    assert http.user_agent_for(url, http.DESKTOP_UA) == http.DESKTOP_UA
    assert http.user_agent_for(url, http.WECHAT_UA) == http.WECHAT_UA


@respx.mock
def test_get_download_and_resolve_send_the_bot_ua_to_wikipedia(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/html"})

    respx.route(host="en.wikipedia.org").mock(side_effect=handler)
    http.get("https://en.wikipedia.org/wiki/Octopus")
    http.download("https://en.wikipedia.org/wiki/Octopus", tmp_path, max_bytes=1000)
    http.resolve("https://en.wikipedia.org/wiki/Octopus")
    assert seen == [http.BOT_UA] * 3


# ---- P29 fix round 1: redirects can be refused (M3); a cancelled job's download stops (M8) ----

@respx.mock
def test_get_and_download_can_refuse_redirects(tmp_path):
    respx.get("https://a.com/x").mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/x"}))
    elsewhere = respx.get("https://evil.example/x").mock(return_value=httpx.Response(200, content=b"x"))
    with pytest.raises(FetchError) as ei:
        http.get("https://a.com/x", follow_redirects=False)
    assert "302" in str(ei.value) and not ei.value.retryable
    with pytest.raises(FetchError):
        http.download("https://a.com/x", tmp_path, max_bytes=100, follow_redirects=False)
    assert elsewhere.call_count == 0 and list(tmp_path.iterdir()) == []


@respx.mock
def test_download_stops_when_the_fetch_is_cancelled(tmp_path):
    import threading

    from octopus.fetchers.base import FETCH_CANCEL, FetchCancelled

    respx.get("https://a.com/a.mp3").mock(return_value=httpx.Response(200, content=b"abc"))
    stop = threading.Event()
    stop.set()
    token = FETCH_CANCEL.set(stop)
    try:
        with pytest.raises(FetchCancelled):
            http.download("https://a.com/a.mp3", tmp_path, max_bytes=100)
    finally:
        FETCH_CANCEL.reset(token)
    assert list(tmp_path.iterdir()) == []
    assert http.download("https://a.com/a.mp3", tmp_path, max_bytes=100).read_bytes() == b"abc"  # no scope: as before
