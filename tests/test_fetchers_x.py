import httpx
import pytest
import respx

from octopus.fetchers.base import FetchError
from octopus.fetchers.pdf_url import PDFFetcher
from octopus.fetchers.x import XFetcher, syndication_token

SYN = "https://cdn.syndication.twimg.com/tweet-result"


def test_token_matches_reference():
    assert syndication_token("20") == "6dq1a2xwd93"


@respx.mock
def test_tweet():
    route = respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json={
        "__typename": "Tweet", "text": "复利是第八大奇迹", "lang": "zh", "created_at": "2026-10-01T00:00:00.000Z",
        "user": {"name": "张三", "screen_name": "zhangsan"},
        "quoted_tweet": {"text": "被引用的推文", "user": {"screen_name": "lisi"}}}))
    f = XFetcher()
    assert f.can_handle("https://x.com/zhangsan/status/1519480761749016577?s=20")
    assert f.can_handle("https://twitter.com/zhangsan/status/1519480761749016577")
    r = f.fetch("https://x.com/zhangsan/status/1519480761749016577")
    q = route.calls[0].request.url.params
    assert q["id"] == "1519480761749016577" and q["token"] == syndication_token("1519480761749016577")
    assert (r.source, r.author, r.language, r.status) == ("x", "张三 (@zhangsan)", "zh", "ok")
    assert r.text == "复利是第八大奇迹\n\n引用 @lisi：被引用的推文"
    assert r.title is None


@respx.mock
def test_long_tweet_is_partial():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json={
        "__typename": "Tweet", "text": "a" * 280, "note_tweet": {"id": "x"}, "user": {"name": "n", "screen_name": "s"}}))
    r = XFetcher().fetch("https://x.com/s/status/1")
    assert r.status == "partial" and "280" in r.note and r.truncated


@respx.mock
def test_tombstone_and_empty_need_manual():
    respx.get(url__startswith=SYN).mock(side_effect=[httpx.Response(200, json={"__typename": "TweetTombstone"}),
                                                     httpx.Response(200, json={})])
    assert XFetcher().fetch("https://x.com/s/status/1").status == "needs_manual"
    assert XFetcher().fetch("https://x.com/s/status/2").status == "needs_manual"


@respx.mock
def test_deleted_tweet_404_is_needs_manual():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(404))
    r = XFetcher().fetch("https://x.com/s/status/1")
    assert r.status == "needs_manual" and "已删除" in r.note


@respx.mock
def test_other_http_errors_still_raise():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(503))
    with pytest.raises(FetchError) as ei:
        XFetcher().fetch("https://x.com/s/status/1")
    assert ei.value.retryable is True


@respx.mock
@pytest.mark.parametrize("body", ["<html>captcha</html>", "[1, 2]"])
def test_non_object_json_is_a_clear_error(body):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, text=body))
    with pytest.raises(FetchError, match="不是预期格式") as ei:
        XFetcher().fetch("https://x.com/s/status/1")
    assert ei.value.retryable is False


def test_profile_url_not_handled():
    assert not XFetcher().can_handle("https://x.com/zhangsan")


@respx.mock
def test_pdf_url_downloads_without_extracting(tmp_path):
    respx.get("https://a.com/paper.pdf").mock(return_value=httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}))
    f = PDFFetcher(tmp_path, max_bytes=1000)
    assert f.can_handle("https://a.com/paper.pdf?dl=1") and not f.can_handle("https://a.com/paper.html")
    r = f.fetch("https://a.com/paper.pdf")
    assert (r.source, r.title, r.status, r.text) == ("file", "paper.pdf", "ok", "")
    # text extraction is the pipeline's job (under its heavy-work lock); the fetcher only downloads
    assert r.doc_path is not None and r.doc_path.parent == tmp_path and r.doc_path.suffix == ".pdf"
    assert r.doc_path.read_bytes() == b"%PDF-1.4"


def test_arxiv_pdf_links_without_extension_are_pdf(tmp_path):
    f = PDFFetcher(tmp_path, max_bytes=1000)
    assert f.can_handle("https://arxiv.org/pdf/1706.03762")
    assert f.can_handle("https://arxiv.org/pdf/1706.03762v7")
    assert not f.can_handle("https://arxiv.org/abs/1706.03762")


# ---- P29: a post's videos are downloaded for transcription; long posts get their full text from FxTwitter ----

FX = "https://api.fxtwitter.com/2/status/"
VID = "https://video.twimg.com/amplify_video/1000/vid/avc1"
AT = "@"  # a URL with a user part, kept apart so it does not read as an e-mail address


def _video(*variants, kind="video"):
    return {"type": kind, "video_info": {"duration_millis": 60000, "variants": list(variants)}}


def _mp4(url, bitrate):
    return {"bitrate": bitrate, "content_type": "video/mp4", "url": url}


HLS = {"content_type": "application/x-mpegURL", "url": f"{VID}/pl/list.m3u8"}


def _post(**extra):
    return {"__typename": "Tweet", "text": "看这个视频 https://t.co/abc", "lang": "zh",
            "user": {"name": "测试", "screen_name": "someone"}, **extra}


@respx.mock
def test_video_post_downloads_the_lowest_bitrate_mp4(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[_video(
        HLS, _mp4(f"{VID}/1280x720/hi.mp4", 2176000), _mp4(f"{VID}/480x270/lo.mp4", 256000),
        _mp4(f"{VID}/640x360/mid.mp4", 832000))])))
    lo = respx.get(f"{VID}/480x270/lo.mp4").mock(return_value=httpx.Response(
        200, content=b"mp4", headers={"content-type": "video/mp4"}))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert lo.call_count == 1 and len(respx.calls) == 2  # the syndication JSON and the one variant
    assert (r.status, r.note, r.text) == ("ok", None, "看这个视频 https://t.co/abc")
    assert len(r.video_paths) == 1 and r.video_paths[0].parent == tmp_path
    assert r.video_paths[0].suffix == ".mp4" and r.video_paths[0].read_bytes() == b"mp4"
    assert r.audio_path is None


@respx.mock
def test_several_videos_in_order_capped_and_quoted_post_videos_after_its_own(tmp_path):
    own = [_video(_mp4(f"{VID}/own{i}.mp4", 256000)) for i in range(1, 4)]
    quoted = {"text": "被引用的视频", "user": {"screen_name": "other"},
              "mediaDetails": [_video(_mp4(f"{VID}/q{i}.mp4", 256000)) for i in range(1, 3)]}
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/p.jpg"}, *own], quoted_tweet=quoted)))
    respx.get(url__startswith=VID).mock(side_effect=lambda req: httpx.Response(
        200, content=req.url.path.encode(), headers={"content-type": "video/mp4"}))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    got = [p.read_bytes().decode().rsplit("/", 1)[1] for p in r.video_paths]
    assert got == ["own1.mp4", "own2.mp4", "own3.mp4", "q1.mp4"]  # MAX_VIDEOS = 4
    assert r.status == "partial" and "4" in r.note  # M1: something was skipped, and the reply shows why
    assert "被引用的视频" in r.text


@respx.mock
def test_animated_gifs_and_foreign_or_plain_http_urls_are_not_downloaded(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4("https://video.twimg.com/tweet_video/g.mp4", 0), kind="animated_gif"),
        _video(_mp4("https://evil.example/v.mp4", 100), _mp4("http://video.twimg.com/plain.mp4", 200),
               _mp4("https://video.twimg.com.evil.example/v.mp4", 50), _mp4("https://video.twimg.com:8443/p.mp4", 10),
               _mp4(f"https://user:pw{AT}video.twimg.com/u.mp4", 20), _mp4(f"https://user{AT}video.twimg.com/u.mp4", 30)),
    ])))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert len(respx.calls) == 1  # only the syndication JSON
    assert (r.status, r.video_paths) == ("ok", [])


@respx.mock
def test_a_video_that_cannot_be_downloaded_keeps_the_text_as_partial(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000)), _video(_mp4(f"{VID}/b.mp4", 256000))])))
    respx.get(f"{VID}/a.mp4").mock(return_value=httpx.Response(200, content=b"x" * 2000,
                                                               headers={"content-type": "video/mp4"}))
    respx.get(f"{VID}/b.mp4").mock(return_value=httpx.Response(200, content=b"b", headers={"content-type": "video/mp4"}))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert r.status == "partial" and "视频没能下载" in r.note and "文件太大" in r.note
    assert r.text.startswith("看这个视频") and [p.read_bytes() for p in r.video_paths] == [b"b"]
    assert sorted(tmp_path.iterdir()) == r.video_paths  # the oversized one was removed


@respx.mock
def test_a_cancel_during_the_downloads_removes_the_videos_already_downloaded(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000)), _video(_mp4(f"{VID}/b.mp4", 256000))])))
    respx.get(f"{VID}/a.mp4").mock(return_value=httpx.Response(200, content=b"a", headers={"content-type": "video/mp4"}))

    def interrupt(request):
        raise KeyboardInterrupt  # stands in for the job being cancelled while the second video downloads

    respx.get(f"{VID}/b.mp4").mock(side_effect=interrupt)
    with pytest.raises(KeyboardInterrupt):
        XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_without_a_downloads_folder_videos_are_left_alone():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000))])))
    r = XFetcher().fetch("https://x.com/someone/status/1000")
    assert (r.status, r.video_paths, len(respx.calls)) == ("ok", [], 1)


LONG = "长" * 600


@respx.mock
def test_long_post_full_text_from_fxtwitter_when_on():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(
        text="长" * 270 + " https://t.co/abc", note_tweet={"id": "Tm90ZQ"},
        quoted_tweet={"text": "被引用", "user": {"screen_name": "other"}})))
    fx = respx.get(f"{FX}1000").mock(return_value=httpx.Response(200, json={
        "code": 200, "status": {"type": "status", "id": "1000", "text": LONG, "is_note_tweet": True}}))
    r = XFetcher(full_text_service="fxtwitter").fetch("https://x.com/someone/status/1000")
    assert fx.call_count == 1
    assert (r.status, r.note, r.truncated) == ("ok", None, False)
    assert r.text == f"{LONG}\n\n引用 @other：被引用"


@respx.mock
@pytest.mark.parametrize("reply", [
    httpx.Response(503), httpx.Response(404, json={"code": 404, "message": "NOT_FOUND"}),
    httpx.Response(200, text="<html>oops</html>"), httpx.Response(200, json={"code": 200, "status": {"text": ""}}),
    httpx.Response(200, json={"code": 401, "status": {"type": "tombstone"}}), httpx.Response(200, json=[1]),
    httpx.Response(200, json={"code": 200}), httpx.Response(200, json={"code": 200, "status": "text"}),
    httpx.Response(200, json={"code": 200, "status": {"text": 5}}),
    httpx.Response(302, headers={"location": "https://evil.example/status"}),
])
def test_long_post_falls_back_to_the_hint_when_fxtwitter_fails(reply):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(
        text="a" * 280, note_tweet={"id": "x"})))
    respx.get(f"{FX}1000").mock(return_value=reply)
    r = XFetcher(full_text_service="fxtwitter").fetch("https://x.com/someone/status/1000")
    assert r.status == "partial" and "280" in r.note and r.text == "a" * 280 and r.truncated


@respx.mock
def test_long_post_falls_back_when_fxtwitter_is_unreachable():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(text="a" * 280, note_tweet={"id": "x"})))
    respx.get(f"{FX}1000").mock(side_effect=httpx.ConnectError("down"))
    r = XFetcher(full_text_service="fxtwitter").fetch("https://x.com/someone/status/1000")
    assert r.status == "partial" and "280" in r.note


@respx.mock
def test_fxtwitter_is_never_asked_when_off_or_when_the_post_is_short():
    fx = respx.get(url__startswith="https://api.fxtwitter.com/").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__startswith=SYN).mock(side_effect=[
        httpx.Response(200, json=_post(text="a" * 280, note_tweet={"id": "x"})), httpx.Response(200, json=_post())])
    assert XFetcher().fetch("https://x.com/someone/status/1000").status == "partial"
    assert XFetcher(full_text_service="fxtwitter").fetch("https://x.com/someone/status/1001").status == "ok"
    assert fx.call_count == 0


# ---- P29 fix round 1 ----

@respx.mock
def test_a_variant_without_a_bitrate_ranks_last(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[_video(
        {"content_type": "video/mp4", "url": f"{VID}/unknown.mp4"}, _mp4(f"{VID}/hd.mp4", 2176000),
        _mp4(f"{VID}/sd.mp4", 832000))])))
    sd = respx.get(f"{VID}/sd.mp4").mock(return_value=httpx.Response(200, content=b"sd", headers={"content-type": "video/mp4"}))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert sd.call_count == 1 and [p.read_bytes() for p in r.video_paths] == [b"sd"]


@respx.mock
def test_a_video_redirected_elsewhere_is_not_followed(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000))])))
    respx.get(f"{VID}/a.mp4").mock(return_value=httpx.Response(302, headers={"location": "http://10.0.0.1/v.mp4"}))
    inside = respx.get("http://10.0.0.1/v.mp4").mock(return_value=httpx.Response(200, content=b"v"))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert inside.call_count == 0
    assert (r.status, r.video_paths) == ("partial", []) and "视频没能下载" in r.note
    assert r.text.startswith("看这个视频")


@respx.mock
def test_fxtwitter_redirected_elsewhere_is_not_followed():
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(text="a" * 280, note_tweet={"id": "x"})))
    respx.get(f"{FX}1000").mock(return_value=httpx.Response(302, headers={"location": "https://evil.example/s"}))
    elsewhere = respx.get("https://evil.example/s").mock(return_value=httpx.Response(200, json={
        "code": 200, "status": {"text": LONG}}))
    r = XFetcher(full_text_service="fxtwitter").fetch("https://x.com/someone/status/1000")
    assert elsewhere.call_count == 0 and r.status == "partial" and r.text == "a" * 280


@respx.mock
@pytest.mark.parametrize("media", [
    {"mediaDetails": [{"type": "video", "video_info": ["not", "a", "dict"]}]},
    {"mediaDetails": [{"type": "video", "video_info": "text"}]},
    {"mediaDetails": [{"type": "video", "video_info": {"variants": "text"}}]},
    {"mediaDetails": [{"type": "video", "video_info": {"variants": [{"content_type": "video/mp4", "url": 5}]}}]},
    {"mediaDetails": {"type": "video"}},
    {"mediaDetails": "text"},
    {"quoted_tweet": "text"},
    {"quoted_tweet": {"text": "q", "mediaDetails": 5}},
])
def test_malformed_media_json_never_loses_the_post(tmp_path, media):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(**media)))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert r.status == "ok" and r.text.startswith("看这个视频") and r.video_paths == []
    assert len(respx.calls) == 1


@respx.mock
def test_video_numbers_are_the_posts_own_when_one_did_not_download(tmp_path):
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000)), _video(_mp4(f"{VID}/b.mp4", 256000)),
        _video(_mp4(f"{VID}/c.mp4", 256000))])))
    respx.get(f"{VID}/a.mp4").mock(return_value=httpx.Response(503))
    respx.get(f"{VID}/b.mp4").mock(return_value=httpx.Response(200, content=b"b", headers={"content-type": "video/mp4"}))
    respx.get(f"{VID}/c.mp4").mock(return_value=httpx.Response(200, content=b"c", headers={"content-type": "video/mp4"}))
    r = XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    assert [p.read_bytes() for p in r.video_paths] == [b"b", b"c"] and r.video_numbers == [2, 3]
    assert r.status == "partial"


@respx.mock
def test_a_cancelled_fetch_stops_between_videos_and_removes_them(tmp_path):
    import threading

    from octopus.fetchers.base import FETCH_CANCEL, FetchCancelled

    stop = threading.Event()
    respx.get(url__startswith=SYN).mock(return_value=httpx.Response(200, json=_post(mediaDetails=[
        _video(_mp4(f"{VID}/a.mp4", 256000)), _video(_mp4(f"{VID}/b.mp4", 256000))])))

    def first(request):
        stop.set()  # the job is cancelled while the first video downloads (its last chunk is already here)
        return httpx.Response(200, content=b"a", headers={"content-type": "video/mp4"})

    respx.get(f"{VID}/a.mp4").mock(side_effect=first)
    second = respx.get(f"{VID}/b.mp4").mock(return_value=httpx.Response(200, content=b"b"))
    token = FETCH_CANCEL.set(stop)
    try:
        with pytest.raises(FetchCancelled):
            XFetcher(tmp_path, max_bytes=1000).fetch("https://x.com/someone/status/1000")
    finally:
        FETCH_CANCEL.reset(token)
    assert second.call_count == 0 and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("other", ["视频没能下载：HTTP 503", None])
def test_with_page_drops_the_hint_whole_even_when_it_contains_the_separator(monkeypatch, other):
    """Re-review 2 (R2-m1): the hint is taken off as a prefix, not as the first separator-split piece."""
    from octopus import i18n
    from octopus.fetchers.base import FetchResult
    from octopus.i18n import t

    sep = t("common.note_sep")
    monkeypatch.setitem(i18n.CATALOGS[i18n.get_language()], "fetch.long_tweet", f"这是长推文{sep}只拿到前 280 字")
    hint = t("fetch.long_tweet")
    fetched = FetchResult(url="https://x.com/s/status/1", source="x", text="开头", status="partial", truncated=True,
                          note=f"{hint}{sep}{other}" if other else hint)
    r = XFetcher().with_page(fetched, FetchResult(url=None, source="x", text="开头和后面的全部内容"))
    assert r.text == "开头和后面的全部内容" and not r.truncated
    assert (r.note, r.status) == ((other, "partial") if other else (None, "ok"))
