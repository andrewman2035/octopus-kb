"""P17: a page the browser already showed (the Chrome extension sends its HTML) is read without a network fetch."""
from pathlib import Path

import pytest

from fakes import StubFetcher, fake_media
from test_pipeline import make

from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchError, FetchResult
from octopus.fetchers.web import WebFetcher, looks_walled
from octopus.fetchers.wechat_mp import WeChatMPFetcher
from octopus.models import Attachment

FIX = Path(__file__).parent / "fixtures"
LONG = "<p>" + "长期主义的投资者相信复利，时间是最好的朋友。" * 20 + "</p>"
ARTICLE_HTML = f"<html><head><title>网页里的标题</title></head><body><article><h1>网页里的标题</h1>{LONG}</article></body></html>"
SHORT_HTML = "<html><head><title>X 上的帖子</title></head><body><div>Log in to see more</div></body></html>"
SHORT_POST = "这是一条很短的帖子，只有几十个字，但它就是全部的正文内容了。"
SHORT_POST_HTML = f"<html><head><title>帖子</title></head><body><article><p>{SHORT_POST}</p></article></body></html>"
EMPTY_HTML = "<html><head></head><body></body></html>"
FETCHED = FetchResult(url="https://a.com/x", source="web", title="抓到的标题", text="抓到的正文" * 100)


class WebStub(WebFetcher):
    """The real page reader (parse); fetch() would go to the network, so it is recorded instead."""

    def __init__(self, result=FETCHED):
        self.result = result
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return self.result


def page(tmp_path, html, name="page.html"):
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return path


def with_registry(pipe, *fetchers):
    pipe.registry = FetcherRegistry(list(fetchers))
    return pipe


def test_web_parse_is_what_fetch_reads():
    html = (FIX / "web_article.html").read_text()
    r = WebFetcher().parse("https://a.com/x", html)
    assert r.url == "https://a.com/x" and r.source == "web" and r.status == "ok" and len(r.text) >= 200


def test_wechat_parse_reads_js_content():
    html = (FIX / "wechat_article.html").read_text()
    r = WeChatMPFetcher().parse("https://mp.weixin.qq.com/s/abc", html)
    assert r.source == "wechat_mp" and r.text


async def test_long_page_is_saved_from_the_html_without_a_fetch(tmp_path):
    pipe, lib, idx, _ = make(tmp_path)
    web = WebStub()
    with_registry(pipe, web)
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, ARTICLE_HTML), title="页面标题")
    e = out.entry
    assert web.calls == []
    assert (e.url, e.title, e.source, e.status) == ("https://a.com/x", "页面标题", "web", "ok")
    assert "长期主义的投资者相信复利" in e.body
    assert idx.find_by_url("https://a.com/x") == e.id


async def test_page_without_a_title_keeps_the_parsed_one(tmp_path):
    pipe, *_ = make(tmp_path)
    with_registry(pipe, WebStub())
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, ARTICLE_HTML), title="")
    assert out.entry.title == "网页里的标题"


async def test_short_page_falls_back_to_the_normal_link_save(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub()
    with_registry(pipe, web)
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, SHORT_HTML), title="X 上的帖子")
    assert web.calls == ["https://a.com/x"]
    assert out.entry.title == "抓到的标题" and out.entry.body.startswith("抓到的正文")


async def test_media_links_are_saved_by_url_even_with_a_page(tmp_path):
    pipe, *_ = make(tmp_path)
    video = StubFetcher(FetchResult(url="https://www.youtube.com/watch?v=x", source="youtube", title="视频",
                                    text="字幕" * 200), handles="youtube.com")
    video.name = "youtube"
    with_registry(pipe, video, WebStub())
    assert not pipe.reads_page("https://www.youtube.com/watch?v=x") and pipe.reads_page("https://a.com/x")
    out = await pipe.save_url("https://www.youtube.com/watch?v=x", "extension",
                              page=page(tmp_path, ARTICLE_HTML), title="页面标题")
    assert video.calls == ["https://www.youtube.com/watch?v=x"] and out.entry.title == "视频"


async def test_a_manual_platform_page_is_read_from_its_html(tmp_path):
    pipe, *_ = make(tmp_path)
    with_registry(pipe, WebStub())
    url = "https://www.reddit.com/r/x/comments/1"
    assert pipe.reads_page(url)
    out = await pipe.save_url(url, "extension", page=page(tmp_path, ARTICLE_HTML), title="帖子")
    assert (out.entry.status, out.entry.title) == ("ok", "帖子")
    assert out.entry.source == "reddit"  # the platform's name (M8), not "web"
    short = await pipe.save_url(url + "2", "extension", page=page(tmp_path, SHORT_POST_HTML, "b.html"), title="帖子")
    # no fetch can do better on a platform with no automated path: the short text is kept (fix round 1, I2)
    assert (short.entry.status, short.entry.source, short.entry.body) == ("partial", "reddit", SHORT_POST)
    empty = await pipe.save_url(url + "3", "extension", page=page(tmp_path, EMPTY_HTML, "c.html"), title="帖子")
    assert empty.entry.status == "needs_manual"  # nothing at all on the page: as if the link was sent in chat


async def test_a_missing_page_file_falls_back_to_the_link(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub()
    with_registry(pipe, web)
    await pipe.save_url("https://a.com/x", "extension", page=tmp_path / "gone.html", title="t")
    assert web.calls == ["https://a.com/x"]


async def test_wechat_page_is_read_by_the_wechat_parser(tmp_path):
    pipe, *_ = make(tmp_path)
    wx = WeChatMPFetcher()
    wx.fetch = lambda url: (_ for _ in ()).throw(AssertionError("fetched"))
    with_registry(pipe, wx, WebStub())
    body = "<p>" + "公众号文章的正文内容。" * 30 + "</p>"
    html = f'<html><head><meta property="og:title" content="公众号标题"></head><body><div id="js_content">{body}</div></body></html>'
    out = await pipe.save_url("https://mp.weixin.qq.com/s/abc", "extension", page=page(tmp_path, html), title="")
    assert (out.entry.title, out.entry.source) == ("公众号标题", "wechat_mp")
    assert "公众号文章的正文内容" in out.entry.body


async def test_refetch_of_a_failed_link_uses_the_page(tmp_path):
    pipe, lib, idx, _ = make(tmp_path)
    web = WebStub(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="读不到"))
    with_registry(pipe, web)
    first = await pipe.save_url("https://a.com/x", "tg")
    assert first.entry.status == "needs_manual"
    out = await pipe.refetch(first.entry.id, "https://a.com/x", page=page(tmp_path, ARTICLE_HTML), title="页面标题")
    assert web.calls == ["https://a.com/x"]  # only the first, failed save fetched
    assert (out.entry.id, out.entry.status, out.entry.title) == (first.entry.id, "ok", "页面标题")


async def test_a_selection_note_keeps_its_page_link_and_title(tmp_path):
    pipe, lib, idx, provider = make(tmp_path)
    out = await pipe.save_note("选中的一段话", "extension", url="https://a.com/x", title="页面标题")
    e = out.entry
    assert (e.url, e.title, e.source, e.body) == ("https://a.com/x", "页面标题", "manual", "选中的一段话")
    # a later save of that whole page is not "already saved": only link saves count for that
    assert idx.find_by_url("https://a.com/x") is None


async def test_an_image_entry_keeps_its_page_link(tmp_path):
    pipe, lib, idx, _ = make(tmp_path)
    media = fake_media(tmp_path)
    pipe.media = media
    staged = media.incoming_dir / "u1"
    staged.mkdir(parents=True)
    (staged / "image.png").write_bytes(b"png")
    out = await pipe.save_attachments([Attachment("image", staged / "image.png", "image.png")], "", "extension",
                                      url="https://a.com/x")
    assert out.entry.url == "https://a.com/x" and out.entry.source == "manual"
    assert "图片文字" in out.entry.body
    assert idx.find_by_url("https://a.com/x") is None



# ---- fix round 1, I2: a short page is kept unless fetching the link gets more ----

@pytest.mark.parametrize("fetched", [
    FetchError("网站拒绝访问（HTTP 403）", retryable=False),
    FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="要登录"),
    FetchResult(url="https://a.com/x", source="web", title="t", text="登录", status="partial"),
])
async def test_a_short_page_is_kept_when_the_fetch_cannot_do_better(tmp_path, fetched):
    pipe, *_ = make(tmp_path)
    web = WebStub(fetched)
    web.fetch = lambda url: (web.calls.append(url), (_ for _ in ()).throw(fetched) if isinstance(fetched, Exception)
                             else fetched)[1]
    with_registry(pipe, web)
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, SHORT_POST_HTML), title="页面标题")
    assert web.calls == ["https://a.com/x"]
    assert (out.entry.status, out.entry.title, out.entry.body) == ("partial", "页面标题", SHORT_POST)


async def test_an_empty_page_still_raises_the_fetch_error(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub()
    web.fetch = lambda url: (_ for _ in ()).throw(FetchError("网络错误", retryable=True))
    with_registry(pipe, web)
    with pytest.raises(FetchError):
        await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, EMPTY_HTML), title="t")


async def test_an_x_page_keeps_the_x_source(tmp_path):
    """P29: an X link from the extension goes through the X fetcher too; when that fails, the page is the post."""
    from octopus.fetchers.x import XFetcher

    pipe, *_ = make(tmp_path)
    x = XFetcher()
    x.fetch = lambda url: (_ for _ in ()).throw(FetchError("网络错误：ConnectError", retryable=True))
    with_registry(pipe, x, WebStub())
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert (out.entry.source, out.entry.status) == ("x", "ok")
    assert "长期主义的投资者" in out.entry.body


REAL_POST_WITH_LOGIN = ("今天终于搞明白了网银的登录流程。原来要先在柜台开通，再下载证书，最后用手机验证码确认。"
                        "整个过程花了一个多小时，记下来给以后参考。")


# ---- fix round 2: a login wall is no text; a network blip is retried, not settled with the short text ----

@pytest.mark.parametrize("text,walled", [
    ("Log in to see more", True), ("Sign up to read the rest", True), ("Continue reading with a free account", True),
    ("登录后查看更多内容", True), ("打开 App 查看全文", True), ("开通会员即可阅读", True), ("扫码查看", True),
    (SHORT_POST, False), ("这是我的短评：今天读完了一本好书。", False), ("登录" * 150, False),  # not short: an article
    ("I wrote this on my blog in 2020 and still agree.", False), ("The catalog includes everything.", False),
    (REAL_POST_WITH_LOGIN, False),  # 登录 in one sentence of a real post: the rest is content (fix round 3)
    ("Don't miss what's happening\nLog in\nSign up", True),
])
def test_looks_walled(text, walled):
    assert looks_walled(text) is walled


WALL_HTML = "<html><head><title>X 上的帖子</title></head><body><article><p>Log in to see more</p></article></body></html>"


async def test_a_login_wall_on_a_manual_platform_needs_manual(tmp_path):
    pipe, *_ = make(tmp_path)
    with_registry(pipe, WebStub())
    out = await pipe.save_url("https://www.reddit.com/r/x/comments/9", "extension", page=page(tmp_path, WALL_HTML),
                              title="帖子")
    assert out.entry.status == "needs_manual"


async def test_a_login_wall_is_never_kept_when_the_fetch_fails(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub()
    web.fetch = lambda url: (_ for _ in ()).throw(FetchError("网站拒绝访问（HTTP 403）", retryable=False))
    with_registry(pipe, web)
    with pytest.raises(FetchError):  # the worker saves it as needs_manual, as for a chat link
        await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, WALL_HTML), title="t")


async def test_a_login_wall_takes_whatever_the_fetch_gets(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub(FetchResult(url="https://a.com/x", source="web", title="t", text="短", status="partial"))
    with_registry(pipe, web)
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, WALL_HTML), title="t")
    assert out.entry.body == "短"


async def test_a_network_error_keeps_the_short_page_at_once(tmp_path):
    """Fix round 3 (N1): the browser could read the page, the service cannot reach the site (a proxy Chrome has and
    launchd does not): the short text is kept now, not retried for half an hour and then lost."""
    from octopus.fetchers.x import XFetcher

    pipe, *_ = make(tmp_path)
    x = XFetcher()
    calls = []
    x.fetch = lambda url: (calls.append(url), (_ for _ in ()).throw(FetchError("网络错误：ConnectError", retryable=True)))[1]
    with_registry(pipe, x, WebStub())
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, SHORT_POST_HTML), title="帖")
    assert calls == ["https://x.com/a/status/1"]
    assert (out.entry.status, out.entry.source, out.entry.body) == ("partial", "x", SHORT_POST)


# ---- P29 fix round 1: X links from the extension go through the X fetcher, the page is the fallback ----

def _x_with(pipe, tmp_path, result):
    """An X fetcher whose fetch returns `result` (with a downloaded video when it names one)."""
    from octopus.fetchers.x import XFetcher

    pipe.media = media = fake_media(tmp_path)
    x = XFetcher()
    calls = []
    x.fetch = lambda url: (calls.append(url), result)[1]
    with_registry(pipe, x, WebStub())
    return media, calls


def _video_file(tmp_path, name="v.mp4"):
    d = tmp_path / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"v")
    return d / name


POST_TEXT = "这条帖子本身就有足够长的正文，" * 20  # 300 characters: a whole post, the page adds nothing


async def test_an_x_page_is_fetched_too_and_its_video_transcribed(tmp_path):
    pipe, *_ = make(tmp_path)
    video = _video_file(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", author="测试 (@a)", text=POST_TEXT,
                          language="zh", video_paths=[video])
    media, calls = _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert calls == ["https://x.com/a/status/1"]
    assert (out.entry.source, out.entry.status, out.entry.author) == ("x", "ok", "测试 (@a)")
    assert out.entry.body == f"{POST_TEXT}\n\n转写稿"
    assert not video.exists()


async def test_an_x_article_takes_the_pages_text_and_keeps_the_fetchs_video(tmp_path):
    """Fix round 2 (OQ1): an X Article's post is only a t.co link; the page has the article."""
    pipe, *_ = make(tmp_path)
    video = _video_file(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", author="测试 (@a)", text="https://t.co/abc123",
                          language="zh", video_paths=[video])
    media, _ = _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert (out.entry.source, out.entry.status, out.entry.author) == ("x", "ok", "测试 (@a)")
    assert "长期主义的投资者相信复利" in out.entry.body and "t.co" not in out.entry.body
    assert out.entry.body.endswith("\n\n转写稿") and media.calls == [("asr", str(video))] and not video.exists()


THREAD = [f"第 {i} 条：长期投资靠的是复利和耐心，别被短期波动吓跑，坚持定投，坚持读书，坚持记录自己的判断和理由。" for i in range(1, 6)]
THREAD_HTML = ("<html><head><title>帖子</title></head><body><article>"
               + "".join(f"<p>{line}</p>" for line in THREAD) + "</article></body></html>")


async def test_an_x_thread_page_beats_its_short_first_post(tmp_path):
    """Fix round 2 (OQ1): a self-thread; the fetch has the first post only, the page has all of them."""
    pipe, *_ = make(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", author="测试 (@a)", text=THREAD[0] + " https://t.co/x")
    _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, THREAD_HTML), title="帖")
    assert all(line in out.entry.body for line in THREAD)
    assert (out.entry.status, out.entry.author) == ("ok", "测试 (@a)")


async def test_a_short_x_post_keeps_the_fetch_when_the_page_is_short_too(tmp_path):
    pipe, *_ = make(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", author="测试 (@a)", text="短帖")
    _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, SHORT_POST_HTML), title="帖")
    assert out.entry.body == "短帖"


async def test_a_long_x_post_takes_the_pages_text_and_keeps_the_fetchs_video(tmp_path):
    from octopus.i18n import t

    pipe, *_ = make(tmp_path)
    video = _video_file(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", author="测试 (@a)", text="长期主义" * 10,
                          language="zh", status="partial", note=t("fetch.long_tweet"), video_paths=[video],
                          truncated=True)
    media, calls = _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert (out.entry.status, out.note, out.entry.author) == ("ok", None, "测试 (@a)")
    assert "长期主义的投资者相信复利" in out.entry.body and out.entry.body.endswith("\n\n转写稿")
    assert media.calls == [("asr", str(video))] and not video.exists()


async def test_a_long_x_post_with_a_failed_video_keeps_that_note(tmp_path):
    from octopus.i18n import t

    pipe, *_ = make(tmp_path)
    note = t("common.note_sep").join([t("fetch.long_tweet"), t("fetch.video_failed", error="HTTP 503")])
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", text="开头", status="partial", note=note,
                          truncated=True)
    _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert out.entry.status == "partial" and out.note.startswith(t("fetch.video_failed", error="HTTP 503"))
    assert t("fetch.long_tweet") not in out.note and "长期主义的投资者" in out.entry.body


async def test_a_long_x_post_keeps_the_fetch_when_the_page_has_less(tmp_path):
    from octopus.i18n import t

    pipe, *_ = make(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", text="正" * 280, status="partial",
                          note=t("fetch.long_tweet"), truncated=True)
    _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, SHORT_POST_HTML), title="帖")
    assert (out.entry.status, out.entry.body) == ("partial", "正" * 280) and "280" in out.note


async def test_an_x_post_the_fetch_cannot_see_keeps_the_page_and_drops_the_video(tmp_path):
    pipe, *_ = make(tmp_path)
    video = _video_file(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", status="needs_manual", note="看不到",
                          video_paths=[video])
    media, _ = _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, SHORT_POST_HTML), title="帖")
    assert (out.entry.source, out.entry.body) == ("x", SHORT_POST)
    assert not video.exists() and media.calls == []


async def test_a_walled_x_page_with_a_failing_fetch_still_fails(tmp_path):
    from octopus.fetchers.x import XFetcher

    pipe, *_ = make(tmp_path)
    x = XFetcher()
    x.fetch = lambda url: (_ for _ in ()).throw(FetchError("网络错误：ConnectError", retryable=True))
    with_registry(pipe, x, WebStub())
    with pytest.raises(FetchError):
        await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, SHORT_HTML), title="帖")


async def test_other_sites_keep_the_page_rule(tmp_path):
    pipe, *_ = make(tmp_path)
    web = WebStub()
    with_registry(pipe, web)
    out = await pipe.save_url("https://a.com/x", "extension", page=page(tmp_path, ARTICLE_HTML), title="t")
    assert web.calls == [] and "长期主义的投资者" in out.entry.body


async def test_the_long_post_decision_is_the_flag_not_the_note_text(tmp_path):
    """Fix round 2 (R1-m1): a partial fetch that is not marked truncated keeps its note, whatever it says."""
    from octopus.i18n import t

    pipe, *_ = make(tmp_path)
    fetched = FetchResult(url="https://x.com/a/status/1", source="x", text=POST_TEXT, status="partial",
                          note=t("fetch.long_tweet"))
    _x_with(pipe, tmp_path, fetched)
    out = await pipe.save_url("https://x.com/a/status/1", "extension", page=page(tmp_path, ARTICLE_HTML), title="帖")
    assert (out.entry.status, out.entry.body) == ("partial", POST_TEXT)
