import re
from pathlib import Path

import httpx
import pytest
import respx

from octopus.fetchers import default_registry, is_manual, manual_source
from octopus.fetchers.base import FetchError
from octopus.fetchers.web import WebFetcher
from octopus.fetchers.wechat_mp import WeChatMPFetcher

FIX = Path(__file__).parent / "fixtures"
HTML = {"content-type": "text/html; charset=utf-8"}


@respx.mock
def test_web_fetcher_extracts_article():
    respx.get("https://blog.example.com/p").mock(
        return_value=httpx.Response(200, text=(FIX / "web_article.html").read_text(), headers=HTML))
    r = WebFetcher().fetch("https://blog.example.com/p")
    assert r.title == "The Power of Compounding"
    assert r.author == "Jane Doe"
    assert "eighth wonder of the world" in r.text
    assert "Copyright 2026" not in r.text
    assert (r.status, r.source, r.url) == ("ok", "web", "https://blog.example.com/p")


@respx.mock
def test_web_fetcher_short_page_is_partial():
    respx.get("https://a.com/x").mock(return_value=httpx.Response(
        200, text="<html><head><title>Only a title</title></head><body><p>hi</p></body></html>", headers=HTML))
    r = WebFetcher().fetch("https://a.com/x")
    assert r.status == "partial"
    assert r.title == "Only a title"
    assert r.note


@respx.mock
def test_web_fetcher_empty_page_needs_manual():
    respx.get("https://a.com/x").mock(return_value=httpx.Response(200, text="<html><body></body></html>", headers=HTML))
    assert WebFetcher().fetch("https://a.com/x").status == "needs_manual"


@respx.mock
@pytest.mark.parametrize("status,retryable", [(403, False), (404, False), (429, True), (503, True)])
def test_http_status_errors(status, retryable):
    respx.get("https://a.com/x").mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as ei:
        WebFetcher().fetch("https://a.com/x")
    assert ei.value.retryable is retryable


@respx.mock
def test_network_error_is_retryable():
    respx.get("https://a.com/x").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(FetchError) as ei:
        WebFetcher().fetch("https://a.com/x")
    assert ei.value.retryable


@respx.mock
def test_wechat_fetcher():
    url = "https://mp.weixin.qq.com/s/abc"
    route = respx.get(url).mock(return_value=httpx.Response(200, text=(FIX / "wechat_article.html").read_text(), headers=HTML))
    r = WeChatMPFetcher().fetch(url)
    assert "MicroMessenger" in route.calls[0].request.headers["user-agent"]
    assert r.title == "复利的力量"
    assert r.author == "投资笔记"
    assert r.source == "wechat_mp"
    assert r.status == "ok"
    paras = r.text.split("\n\n")
    assert paras[0].startswith("复利是指投资的收益继续产生收益")
    assert paras[1].startswith("第二段：定投可以降低择时的难度。")
    assert len(paras) == 3


@respx.mock
def test_wechat_verification_page_is_retryable():
    url = "https://mp.weixin.qq.com/s/abc"
    respx.get(url).mock(return_value=httpx.Response(200, text="<html><body>环境异常，完成验证后即可继续访问</body></html>", headers=HTML))
    with pytest.raises(FetchError) as ei:
        WeChatMPFetcher().fetch(url)
    assert ei.value.retryable


@respx.mock
def test_wechat_page_without_body_is_retryable_and_logged(caplog):
    url = "https://mp.weixin.qq.com/s/abc"
    page = "<html><head><title>请稍候</title></head><body>访问过于频繁</body></html>"
    respx.get(url).mock(return_value=httpx.Response(200, text=page, headers=HTML))
    with pytest.raises(FetchError) as ei:
        WeChatMPFetcher().fetch(url)
    assert ei.value.retryable
    assert str(ei.value) == "页面里找不到正文"
    assert "请稍候" in caplog.text


@respx.mock
def test_wechat_deleted_article_needs_manual():
    url = "https://mp.weixin.qq.com/s/abc"
    respx.get(url).mock(return_value=httpx.Response(200, text="<html><body>该内容已被发布者删除</body></html>", headers=HTML))
    r = WeChatMPFetcher().fetch(url)
    assert r.status == "needs_manual"
    assert "删除" in r.note


def test_registry_and_manual_platforms():
    reg = default_registry()
    assert isinstance(reg.pick("https://mp.weixin.qq.com/s/abc"), WeChatMPFetcher)
    assert isinstance(reg.pick("https://example.com/post"), WebFetcher)
    for url in ["https://www.reddit.com/r/x/comments/1", "https://redd.it/abc",
                "https://channels.weixin.qq.com/web/pages/feed?eid=1", "https://weixin.qq.com/sph/AbC"]:
        assert is_manual(url), url
        assert reg.pick(url) is None
    assert manual_source("https://www.reddit.com/r/x") == "reddit"
    assert manual_source("https://weixin.qq.com/sph/AbC") == "wechat_channels"
    assert not is_manual("https://mp.weixin.qq.com/s/abc")


def _wechat(body_html: str) -> str:
    return f'<html><body><h1 id="activity-name">T</h1><div id="js_content">{body_html}</div></body></html>'


@respx.mock
def test_wechat_nested_blocks_not_duplicated():
    url = "https://mp.weixin.qq.com/s/abc"
    html = _wechat("<blockquote><p>引用内容在这里</p></blockquote><ul><li><p>列表项目一</p></li><li>列表项目二</li></ul><p>普通段落</p>")
    respx.get(url).mock(return_value=httpx.Response(200, text=html, headers=HTML))
    r = WeChatMPFetcher().fetch(url)
    assert r.text.split("\n\n") == ["引用内容在这里", "列表项目一", "列表项目二", "普通段落"]


@respx.mock
def test_wechat_br_between_latin_words_keeps_space():
    url = "https://mp.weixin.qq.com/s/abc"
    respx.get(url).mock(return_value=httpx.Response(200, text=_wechat("<p>hello<br>world</p><p>中文<br>继续</p>"), headers=HTML))
    r = WeChatMPFetcher().fetch(url)
    assert r.text.split("\n\n") == ["hello world", "中文继续"]


def test_wechat_can_handle_ignores_port():
    f = WeChatMPFetcher()
    assert f.can_handle("https://mp.weixin.qq.com:443/s/abc")
    assert not f.can_handle("https://example.com/s/abc")


@respx.mock
@pytest.mark.parametrize("exc", [httpx.TooManyRedirects("loop"), httpx.DecodingError("bad gzip"), httpx.InvalidURL("bad url")])
def test_non_transport_request_errors_are_not_retryable(exc):
    respx.get("https://a.com/x").mock(side_effect=exc)
    with pytest.raises(FetchError) as ei:
        WebFetcher().fetch("https://a.com/x")
    assert ei.value.retryable is False


@respx.mock
@pytest.mark.parametrize("body,expected", [
    ("<blockquote>直接文本<p>子段</p></blockquote><p>后段</p>", ["直接文本", "子段", "后段"]),
    ("<ul><li>标题<p>子段</p></li></ul>", ["标题", "子段"]),
    ("<section>只在section里的文字</section><p>段落</p>", ["只在section里的文字", "段落"]),
    ("<div>前<span>中</span>后<script>var x=1;</script><!-- c --></div><p>尾</p>", ["前中后", "尾"]),
])
def test_wechat_mixed_content_has_no_loss_or_duplication(body, expected):
    url = "https://mp.weixin.qq.com/s/abc"
    respx.get(url).mock(return_value=httpx.Response(200, text=_wechat(body), headers=HTML))
    assert WeChatMPFetcher().fetch(url).text.split("\n\n") == expected


@respx.mock
def test_web_fetcher_rejects_binary_content():
    respx.get("https://a.com/file.zip").mock(return_value=httpx.Response(
        200, content=b"PK\x03\x04", headers={"content-type": "application/zip"}))
    with pytest.raises(FetchError) as ei:
        WebFetcher().fetch("https://a.com/file.zip")
    assert not ei.value.retryable
    assert "不是网页" in str(ei.value)


def test_registry_with_media_fetchers(tmp_path):
    from octopus.config import MediaConfig
    from octopus.fetchers.bilibili import BilibiliFetcher
    from octopus.fetchers.x import XFetcher
    from octopus.fetchers.youtube import YouTubeFetcher

    reg = default_registry(tmp_path, MediaConfig(youtube_cookies_from_browser="chrome"))
    yt = reg.pick("https://youtu.be/dQw4w9WgXcQ")
    assert isinstance(yt, YouTubeFetcher) and yt.cookies_from_browser == "chrome"
    assert isinstance(reg.pick("https://b23.tv/x"), BilibiliFetcher)
    assert isinstance(reg.pick("https://x.com/a/status/1"), XFetcher)
    assert isinstance(reg.pick("https://mp.weixin.qq.com/s/abc"), WeChatMPFetcher)
    assert isinstance(reg.pick("https://example.com/post"), WebFetcher)
    assert reg.pick("https://www.reddit.com/r/x") is None


def test_registry_caps_and_routing(tmp_path):
    from octopus.config import MediaConfig
    from octopus.fetchers.bilibili import BilibiliFetcher
    from octopus.fetchers.pdf_url import PDFFetcher
    from octopus.fetchers.podcasts import ApplePodcastsFetcher, DirectAudioFetcher, XiaoyuzhouFetcher
    from octopus.fetchers.x import XFetcher
    from octopus.fetchers.youtube import YouTubeFetcher

    reg = default_registry(tmp_path, MediaConfig(max_download_mb=2))
    cap = 2 * 1_048_576
    downloaders = [f for f in reg.fetchers if hasattr(f, "download_dir")]
    assert {type(f) for f in downloaders} == {XiaoyuzhouFetcher, ApplePodcastsFetcher, YouTubeFetcher,
                                              BilibiliFetcher, PDFFetcher, DirectAudioFetcher, XFetcher}
    assert all(f.max_bytes == cap and f.download_dir == tmp_path for f in downloaders)
    assert isinstance(reg.pick("https://www.xiaoyuzhoufm.com/episode/abc"), XiaoyuzhouFetcher)
    assert isinstance(reg.pick("https://podcasts.apple.com/us/podcast/a/id1?i=2"), ApplePodcastsFetcher)
    assert isinstance(reg.pick("https://a.com/paper.pdf"), PDFFetcher)
    assert isinstance(reg.pick("https://a.com/talk.mp3"), DirectAudioFetcher)


def test_registry_passes_the_x_full_text_service(tmp_path):
    from octopus.config import MediaConfig

    assert default_registry(tmp_path, MediaConfig()).pick("https://x.com/a/status/1").full_text_service == ""
    x = default_registry(tmp_path, MediaConfig(x_full_text_service="fxtwitter")).pick("https://x.com/a/status/1")
    assert x.full_text_service == "fxtwitter"


def test_registry_without_downloads_dir_is_text_only():
    assert [type(f) for f in default_registry().fetchers] == [WeChatMPFetcher, WebFetcher]


def _picture_page(n=2, caption=r"第一行\x0a第二行 A\x26amp;B", extra_head=""):
    items = "".join(
        f"{{ cdn_url: 'https://mmbiz.qpic.cn/p{i}/0?wx_fmt=png\\x26amp;from=appmsg', width: '1680' * 1, height: '900' * 1,"
        f" watermark_info: {{ cdn_url: 'https://mmbiz.qpic.cn/wm{i}/0', is_uploader: true }},"
        f" share_cover: {{ file_id: '9', cdn_url: 'https://mmbiz.qpic.cn/sc{i}/0' }} }},"
        for i in range(1, n + 1))
    return (f"<html><head><meta property=\"og:title\" content=\"图集标题\">{extra_head}</head><body>"
            f"<script>window.cgiDataNew = {{ item_show_type: '8' * 1, nick_name: '图集号', "
            f"content_noencode: '{caption}', picture_page_info_list: [ {items} ] }};</script></body></html>")


def _mock_images(n=2):
    for i in range(1, n + 1):
        respx.get(f"https://mmbiz.qpic.cn/p{i}/0?wx_fmt=png&from=appmsg").mock(
            return_value=httpx.Response(200, content=f"img{i}".encode(), headers={"content-type": "image/png"}))


@respx.mock
def test_wechat_picture_post_downloads_images_in_order(tmp_path):
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(), headers=HTML))
    _mock_images()
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert (r.source, r.status, r.title, r.author) == ("wechat_mp", "ok", "图集标题", "图集号")
    assert r.text == "第一行\n第二行 A&B"
    assert [p.name for p in r.image_paths] == ["01.png", "02.png"]
    assert [p.read_bytes() for p in r.image_paths] == [b"img1", b"img2"]
    assert r.image_paths[0].parent.parent == tmp_path and r.image_paths[0].parent.name.startswith("wechat-")


@respx.mock
def test_wechat_js_content_page_keeps_article_path(tmp_path):
    url = "https://mp.weixin.qq.com/s/abc"
    html = _picture_page().replace("</body>", '<div id="js_content"><p>正文很长' + "字" * 200 + "</p></div></body>")
    respx.get(url).mock(return_value=httpx.Response(200, text=html, headers=HTML))
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert r.image_paths == [] and r.text.startswith("正文很长")


@respx.mock
def test_wechat_picture_post_caps_images(tmp_path):
    from octopus.fetchers.wechat_mp import MAX_POST_IMAGES
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(MAX_POST_IMAGES + 5), headers=HTML))
    _mock_images(MAX_POST_IMAGES + 5)
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert len(r.image_paths) == MAX_POST_IMAGES == 30
    assert "只保存了前 30 张" in r.note


@respx.mock
def test_wechat_picture_post_failed_download_is_skipped(tmp_path):
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(), headers=HTML))
    respx.get("https://mmbiz.qpic.cn/p1/0?wx_fmt=png&from=appmsg").mock(return_value=httpx.Response(404))
    respx.get("https://mmbiz.qpic.cn/p2/0?wx_fmt=png&from=appmsg").mock(
        return_value=httpx.Response(200, content=b"img2", headers={"content-type": "image/png"}))
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert [p.read_bytes() for p in r.image_paths] == [b"img2"]
    assert r.status == "partial" and "1 张图片下载失败" in r.note


@respx.mock
def test_wechat_picture_post_without_downloads_dir_is_caption_only():
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(), headers=HTML))
    r = WeChatMPFetcher().fetch(url)
    assert r.image_paths == [] and r.text == "第一行\n第二行 A&B"
    assert r.status == "partial" and "没有配置图片识别" in r.note


@respx.mock
def test_wechat_picture_post_nothing_downloaded(tmp_path):
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(), headers=HTML))
    respx.get(host="mmbiz.qpic.cn").mock(return_value=httpx.Response(404))
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)  # caption kept
    assert r.status == "partial" and r.note == "图片下载失败" and r.image_paths == []
    assert not any(tmp_path.iterdir())
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(caption=""), headers=HTML))
    with pytest.raises(FetchError, match="图片下载失败") as ei:
        WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert ei.value.retryable


def test_registry_wechat_gets_downloads_dir_and_cap(tmp_path):
    from octopus.config import MediaConfig
    f = next(f for f in default_registry(tmp_path, MediaConfig(max_download_mb=2)).fetchers
             if isinstance(f, WeChatMPFetcher))
    assert f.downloads_dir == tmp_path and f.max_bytes == 2 * 1_048_576
    assert next(f for f in default_registry().fetchers if isinstance(f, WeChatMPFetcher)).downloads_dir is None


@respx.mock
def test_wechat_picture_post_decodes_surrogate_pairs(tmp_path):
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(caption=r"好😀\'x\'\\"), headers=HTML))
    _mock_images()
    assert WeChatMPFetcher(tmp_path, 1_000_000).fetch(url).text == "好\U0001F600'x'\\"


@respx.mock
def test_wechat_cap_note_survives_failed_downloads(tmp_path):
    from octopus.fetchers.wechat_mp import MAX_POST_IMAGES
    url = "https://mp.weixin.qq.com/s/pic"
    respx.get(url).mock(return_value=httpx.Response(200, text=_picture_page(MAX_POST_IMAGES + 1), headers=HTML))
    respx.get(host="mmbiz.qpic.cn").mock(return_value=httpx.Response(404))
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert r.note == "图片太多，只保存了前 30 张；图片下载失败"
    _mock_images(2)
    respx.get("https://mmbiz.qpic.cn/p3/0?wx_fmt=png&from=appmsg").mock(return_value=httpx.Response(404))
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(url)
    assert r.note.startswith("图片太多，只保存了前 30 张；") and "下载失败" in r.note


SHARED_URL = "https://mp.weixin.qq.com/s/SharedPage0"
ORIGINAL_URL = "https://mp.weixin.qq.com/s?__biz=FAKEBIZ00&mid=100000001&idx=1&sn=0fake0sn0"


def _shared_page(noencode: bool = True, share_url: str | None = None) -> str:
    html = (FIX / "wechat_shared.html").read_text()
    if not noencode:
        html = re.sub(r"content_noencode: '(?:[^'\\]|\\.)*'", "content_noencode: ''", html, flags=re.S)
    if share_url is not None:
        html = re.sub(r'data-url="[^"]*"', f'data-url="{share_url}"', html)
    return html


@respx.mock
def test_wechat_shared_page_reads_inline_article():
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(), headers=HTML))
    r = WeChatMPFetcher().fetch(SHARED_URL)  # the original is not fetched: respx fails on an unmocked request
    assert (r.url, r.source, r.status, r.title, r.author) == (SHARED_URL, "wechat_mp", "ok", "阳台种菜的一年", "示例转载号")
    paras = r.text.split("\n\n")
    assert len(paras) == 3
    assert paras[0].startswith("第一段：去年春天在阳台放了三个花盆")
    assert paras[1] == "第二段：土要松，盆底要有排水孔；每天早上看一眼叶子，比定时浇水更靠得住。小葱最省心，剪了又长。"
    assert paras[2].startswith("第三段：It's A & B, a back\\slash, and a line break. 到了秋天")
    assert paras[2].endswith("这一年最大的收获是学会了耐心。")  # a backslash-newline continues the string
    assert "hidden" not in r.text


@respx.mock
def test_wechat_shared_page_follows_share_link_once():
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(noencode=False), headers=HTML))
    original = respx.get(ORIGINAL_URL).mock(
        return_value=httpx.Response(200, text=(FIX / "wechat_article.html").read_text(), headers=HTML))
    r = WeChatMPFetcher().fetch(SHARED_URL)
    assert original.call_count == 1
    assert str(original.calls[0].request.url) == ORIGINAL_URL  # https, no #wechat_redirect
    assert "MicroMessenger" in original.calls[0].request.headers["user-agent"]
    assert (r.url, r.source, r.status, r.title, r.author) == (SHARED_URL, "wechat_mp", "ok", "复利的力量", "投资笔记")
    paras = r.text.split("\n\n")
    assert paras[0] == f"原文链接：{ORIGINAL_URL}"
    assert paras[1].startswith("复利是指投资的收益继续产生收益") and len(paras) == 4


@respx.mock
def test_wechat_shared_page_without_article_or_link_is_partial():
    page = re.sub(r'<span[^>]*id="js_share_source".*?</span>', "", _shared_page(noencode=False), flags=re.S)
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=page, headers=HTML))
    r = WeChatMPFetcher().fetch(SHARED_URL)
    assert (r.status, r.title, r.author, r.text, r.note) == ("partial", "阳台种菜的一年", "示例转载号", "", "只拿到了标题或很短的内容")


@respx.mock
@pytest.mark.parametrize("share_url", ["https://evil.example.com/s?x=1", "http://mp.weixin.qq.com.evil.example/s",
                                       "ftp://mp.weixin.qq.com/s", "javascript:alert(1)", ""])
def test_wechat_share_link_elsewhere_is_not_followed(share_url):
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(False, share_url), headers=HTML))
    assert WeChatMPFetcher().fetch(SHARED_URL).status == "partial"  # any other request would fail the test


@respx.mock
def test_wechat_share_link_is_one_hop():
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(noencode=False), headers=HTML))
    again = _shared_page(False, "https://mp.weixin.qq.com/s/ThirdPage")
    original = respx.get(ORIGINAL_URL).mock(return_value=httpx.Response(200, text=again, headers=HTML))
    r = WeChatMPFetcher().fetch(SHARED_URL)
    assert original.call_count == 1 and r.status == "partial" and r.url == SHARED_URL


NO_BODY_PAGE = "<html><head><title>提示</title></head><body>未知错误，请稍后再试</body></html>"


@respx.mock
@pytest.mark.parametrize("answer,reason", [
    (httpx.Response(200, text="<html><body>环境异常，完成验证后即可继续访问</body></html>"), "微信要求验证"),
    (httpx.Response(200, text=NO_BODY_PAGE), "页面里找不到正文"),
    (httpx.Response(503), "503"),
    (httpx.ConnectTimeout("slow"), "网络"),
    (httpx.Response(404), "404"),
    (httpx.Response(200, text="<html><body>该内容已被发布者删除</body></html>"), "文章已删除"),
    (httpx.Response(200, text="<div id=\"js_content\">" + "<div>" * 3000 + "字" * 300 + "</div>" * 3000 + "</div>"),
     "返回的内容不是预期格式"),
])
def test_wechat_original_not_read_keeps_shared_page(answer, reason):
    # never worse than before: the shared page's title and author, saved now (no retry), with the reason
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(noencode=False), headers=HTML))
    respx.get(ORIGINAL_URL).mock(side_effect=answer) if isinstance(answer, Exception) else \
        respx.get(ORIGINAL_URL).mock(return_value=answer)
    r = WeChatMPFetcher().fetch(SHARED_URL)
    assert (r.status, r.title, r.author, r.url, r.text) == ("partial", "阳台种菜的一年", "示例转载号", SHARED_URL, "")
    assert r.note.startswith("只拿到了标题或很短的内容；原文没读到（") and reason in r.note
    assert "重试" not in r.note  # nothing retries the original


def test_wechat_original_verify_note_has_no_retry_promise():
    from octopus.i18n import CATALOGS
    assert CATALOGS["zh"]["fetch.wechat_verify_short"] == "微信要求验证"
    assert "again" not in CATALOGS["en"]["fetch.wechat_verify_short"]


def test_wechat_share_tag_search_is_linear():
    import time
    from octopus.fetchers.wechat_mp import _share_source
    start = time.monotonic()
    assert _share_source("<" + "a<b " * 20_000) is None  # 80 KB of "<" with no ">": seconds when quadratic
    assert time.monotonic() - start < 1.0


@respx.mock
def test_wechat_original_without_title_or_author_uses_shared_page_ones():
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(noencode=False), headers=HTML))
    respx.get(ORIGINAL_URL).mock(return_value=httpx.Response(
        200, text='<html><body><div id="js_content"><p>' + "字" * 250 + "</p></div></body></html>", headers=HTML))
    r = WeChatMPFetcher().fetch(SHARED_URL)
    assert (r.status, r.title, r.author, r.url) == ("ok", "阳台种菜的一年", "示例转载号", SHARED_URL)


@respx.mock
def test_wechat_original_picture_post_is_used(tmp_path):
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=_shared_page(noencode=False), headers=HTML))
    respx.get(ORIGINAL_URL).mock(return_value=httpx.Response(200, text=_picture_page(), headers=HTML))
    _mock_images()
    r = WeChatMPFetcher(tmp_path, 1_000_000).fetch(SHARED_URL)
    assert (r.status, r.url, r.title, len(r.image_paths)) == ("ok", SHARED_URL, "图集标题", 2)
    assert r.text == f"原文链接：{ORIGINAL_URL}\n\n第一行\n第二行 A&B"


@respx.mock
def test_wechat_share_link_to_itself_is_not_followed():
    page = _shared_page(False, f"{SHARED_URL}?scene=21#wechat_redirect")
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=page, headers=HTML))
    assert WeChatMPFetcher().fetch(SHARED_URL).status == "partial"


@respx.mock
def test_wechat_share_link_spaces_and_attribute_order():
    page = re.sub(r'<span class="original_panel_tool" id="js_share_source" data-url="([^"]*)"',
                  lambda m: f"<span data-url=' {m.group(1)} ' class=\"x\" id='js_share_source'", _shared_page(noencode=False))
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=page, headers=HTML))
    original = respx.get(ORIGINAL_URL).mock(
        return_value=httpx.Response(200, text=(FIX / "wechat_article.html").read_text(), headers=HTML))
    assert WeChatMPFetcher().fetch(SHARED_URL).status == "ok" and original.call_count == 1


@respx.mock
def test_wechat_shared_page_with_short_own_text():
    page = _shared_page().replace('aria-hidden="true"></div>', 'aria-hidden="true"><p>推荐一读</p></div>')
    respx.get(SHARED_URL).mock(return_value=httpx.Response(200, text=page, headers=HTML))
    assert WeChatMPFetcher().fetch(SHARED_URL).text.startswith("第一段：")  # the inline article is longer
    respx.get(SHARED_URL).mock(return_value=httpx.Response(
        200, text=page.replace("content_noencode: '\\x3csection", "content_noencode: '', x: '"), headers=HTML))
    respx.get(ORIGINAL_URL).mock(
        return_value=httpx.Response(200, text=(FIX / "wechat_article.html").read_text(), headers=HTML))
    assert WeChatMPFetcher().fetch(SHARED_URL).text.startswith(f"原文链接：{ORIGINAL_URL}\n\n复利是指")


def test_wechat_inline_article_failure_keeps_page_result():
    deep = "\\x3cdiv\\x3e" * 3000 + "字"
    page = re.sub(r"content_noencode: '(?:[^'\\]|\\.)*'", lambda m: f"content_noencode: '{deep}'", _shared_page(), flags=re.S)
    r = WeChatMPFetcher().parse(SHARED_URL, page)
    assert (r.status, r.title, r.text) == ("partial", "阳台种菜的一年", "")


def test_wechat_js_decode_rare_escapes_and_doctype():
    from octopus.fetchers.wechat_mp import _inline_article, _js_decode
    assert _js_decode(r"a\bb\fc\vd\0e\u{1F600}\u{110000}") == "abcde\U0001F600\ufffd"
    assert _js_decode("x\\\ny") == "xy"  # a line continuation
    assert _inline_article(r"content_noencode: '\x3c!DOCTYPE html\x3e\x3cp\x3e正文\x3c/p\x3e'") == "正文"


def test_wechat_parse_does_not_follow_share_link():
    # the page the Chrome extension sent: parse reads it, never fetches (the pipeline fetches the link if short)
    r = WeChatMPFetcher().parse(SHARED_URL, _shared_page(noencode=False))
    assert r.status == "partial"
    assert WeChatMPFetcher().parse(SHARED_URL, _shared_page()).status == "ok"


@respx.mock
def test_wechat_short_body_keeps_longer_of_page_and_inline_text():
    url = "https://mp.weixin.qq.com/s/abc"
    html = _picture_page().replace("</body>", '<div id="js_content"><p>正文比图片说明更长一些的一段话</p></div></body>')
    respx.get(url).mock(return_value=httpx.Response(200, text=html, headers=HTML))
    assert WeChatMPFetcher().fetch(url).text == "正文比图片说明更长一些的一段话"
