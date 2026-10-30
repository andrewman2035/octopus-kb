import pytest

from octopus.urls import extract_urls, normalize_url, without_urls


def test_extract_strips_trailing_cjk_punctuation():
    assert extract_urls("链接：https://a.com/x，很好。还有 https://b.com/y） 完") == [
        "https://a.com/x",
        "https://b.com/y",
    ]


def test_extract_dedupes_and_keeps_order():
    assert extract_urls("https://a.com https://b.com https://a.com") == ["https://a.com", "https://b.com"]


def test_normalize_drops_tracking_and_fragment():
    assert normalize_url("https://WWW.Example.com/p?utm_source=x&id=3&spm=1#frag") == "https://www.example.com/p?id=3"


def test_normalize_wechat_keeps_identity_params():
    url = "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&chksm=zz&scene=21#wechat_redirect"
    assert normalize_url(url) == "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc"


def test_normalize_keeps_xsec_token():
    url = "https://www.xiaohongshu.com/explore/1?xsec_token=T&xsec_source=pc_share"
    assert normalize_url(url) == "https://www.xiaohongshu.com/explore/1?xsec_token=T"


def test_normalize_x_drops_s_and_t():
    assert normalize_url("https://x.com/a/status/1?s=20&t=abc") == "https://x.com/a/status/1"


def test_normalize_trailing_punctuation():
    assert normalize_url("https://a.com/x).") == "https://a.com/x"


def test_extract_bare_link_alone_gets_https():
    assert extract_urls("mp.weixin.qq.com/s/AbCd-EfGh_IjKlMnOpQrSt") == [
        "https://mp.weixin.qq.com/s/AbCd-EfGh_IjKlMnOpQrSt"
    ]


def test_extract_bare_link_alone_any_host_with_path():
    assert extract_urls(" example.org/post/1。") == ["https://example.org/post/1"]


def test_extract_bare_known_platform_inside_text():
    assert extract_urls("看这个b23.tv/AbC12 还有 youtu.be/xyz，不错") == [
        "https://b23.tv/AbC12",
        "https://youtu.be/xyz",
    ]


def test_extract_bare_and_full_links_keep_order_and_dedupe():
    text = "x.com/a/status/1 https://mp.weixin.qq.com/s/abc mp.weixin.qq.com/s/abc"
    assert extract_urls(text) == ["https://x.com/a/status/1", "https://mp.weixin.qq.com/s/abc"]


def test_extract_bare_unknown_host_inside_chat_is_not_a_link():
    assert extract_urls("用 python.org/downloads 下载最新版是不是更好") == []


def test_extract_bare_needs_a_path():
    assert extract_urls("mp.weixin.qq.com") == []
    assert extract_urls("我常看 github.com 上的项目") == []


def test_extract_bare_ignores_file_paths_emails_and_versions():
    assert extract_urls("改了 src/octopus/urls.py 和 config.toml") == []
    assert extract_urls("发到 me@example.com/x 吧") == []
    assert extract_urls("版本 1.2/3.4") == []


def test_extract_bare_alone_needs_a_common_tld():
    for text in ("Node.js/Express", "Vue.js/React", "ASP.NET/Core", "foo.py/bar", "index.html/x", "README.md/a"):
        assert extract_urls(text) == [], text


def test_extract_bare_stops_at_chinese_text():
    assert extract_urls("看b23.tv/abc复制打开") == ["https://b23.tv/abc"]


def test_extract_bare_needs_a_non_empty_path():
    assert extract_urls("x.com/ 是") == []
    assert extract_urls("hello.world/") == []


def test_extract_drops_markdown_backticks():
    assert extract_urls("`b23.tv/x` 和 `https://a.com/y`") == ["https://b23.tv/x", "https://a.com/y"]


@pytest.mark.parametrize("text, url", [
    ("项目在这里：https://github.com/example-org/sample-repo’ 大家可以看看", "https://github.com/example-org/sample-repo"),
    ("“https://x.com/a/status/1”", "https://x.com/a/status/1"),
    ("‘https://x.com/a/status/1’", "https://x.com/a/status/1"),
    ("（https://github.com/a/b）", "https://github.com/a/b"),
    ("链接：https://github.com/a/b，看看", "https://github.com/a/b"),
    ("「https://github.com/a/b」", "https://github.com/a/b"),
    ("『https://github.com/a/b』", "https://github.com/a/b"),
    ("【https://github.com/a/b】", "https://github.com/a/b"),
    ("《https://github.com/a/b》", "https://github.com/a/b"),
    ("〈https://github.com/a/b〉", "https://github.com/a/b"),
    ("看 https://github.com/a/b…… 还有", "https://github.com/a/b"),
    ("看 https://github.com/a/b〕", "https://github.com/a/b"),
    ("看 https://github.com/a/b］", "https://github.com/a/b"),
    ("看 https://github.com/a/b｝", "https://github.com/a/b"),
])
def test_curly_quotes_and_cjk_closing_marks_end_a_link(text, url):
    assert extract_urls(text) == [url]


@pytest.mark.parametrize("text, url", [
    ("项目：github.com/example-org/sample-repo’ 看看", "https://github.com/example-org/sample-repo"),
    ("“github.com/a/b”", "https://github.com/a/b"),
    ("‘x.com/a/status/1’", "https://x.com/a/status/1"),
    ("（github.com/a/b）", "https://github.com/a/b"),
    ("github.com/a/b…", "https://github.com/a/b"),
])
def test_curly_quotes_and_cjk_closing_marks_end_a_bare_link(text, url):
    assert extract_urls(text) == [url]


def test_normalize_drops_a_trailing_curly_quote():
    assert normalize_url("https://github.com/example-org/sample-repo’") == "https://github.com/example-org/sample-repo"


def test_without_urls_keeps_the_other_text():
    assert without_urls("看看 https://a.com/x，和 github.com/a/b。") == "看看 ，和 。"
    assert without_urls("只有字") == "只有字"
    assert without_urls("python.org/downloads 上哪个") == "python.org/downloads 上哪个"  # not a link here



@pytest.mark.parametrize("text, url", [  # fix round 1, F5: only punctuation ends a full link
    ("https://example.com/a?q=x～y", "https://example.com/a?q=x～y"),
    ("https://example.com/a〜b", "https://example.com/a〜b"),
    ("https://ja.wikipedia.org/wiki/ｶﾀｶﾅ", "https://ja.wikipedia.org/wiki/ｶﾀｶﾅ"),
    ("https://example.com/ａｂｃ１２３", "https://example.com/ａｂｃ１２３"),
])
def test_tildes_katakana_and_full_width_letters_stay_in_a_link(text, url):
    assert extract_urls(text) == [url]


@pytest.mark.parametrize("mark", list("，。！？；：、＂＇（）［］｛｝＜＞｡｢｣､"))
def test_full_width_punctuation_ends_a_link(mark):
    assert extract_urls(f"看 https://github.com/a/b{mark}后面") == ["https://github.com/a/b"]


# ---- P19: a link urllib cannot parse is just text ----

MALFORMED = ["http://[abc/def", "http://]x/", "https://[", "http://[::1/x", "http://:80/x", "http:///path",
             "http://a.com:99999/x", "http://a.com:abc/x", "http://a.com:-1/x", "http://", "https://?q=1"]


@pytest.mark.parametrize("link", MALFORMED)
def test_a_link_urllib_cannot_parse_is_not_a_link(link):
    text = f"看看 {link} 这个"
    assert extract_urls(text) == []
    assert without_urls(text) == text
    assert normalize_url(link) == link.strip()  # never raises


def test_unicode_and_ipv6_hosts_are_still_links():
    assert extract_urls("看 http://例子.中国/x 和 http://[::1]:8080/a") == ["http://例子.中国/x", "http://[::1]:8080/a"]
    assert normalize_url("http://例子.中国/x?utm_source=a") == "http://例子.中国/x"


def test_an_unparsable_link_still_keeps_a_bare_one_beside_it_from_counting_as_the_whole_message():
    assert extract_urls("http://[abc/def python.org/downloads") == []


def test_a_closing_paren_that_belongs_to_the_link_is_kept():
    # P1 deferred minor: Wikipedia titles end in ")"; one wrapping the whole link is still not part of it
    wiki = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    assert extract_urls(f"看 {wiki} 这页") == [wiki]
    assert extract_urls(f"看 {wiki}。") == [wiki]
    assert extract_urls(f"(见 {wiki})") == [wiki]
    assert extract_urls("(见 https://a.com/x)") == ["https://a.com/x"]
    assert extract_urls("https://a.com/x)") == ["https://a.com/x"]
    assert without_urls(f"看 {wiki})") == "看 )"
    assert normalize_url(wiki) == wiki and normalize_url(wiki + "?utm_source=x") == wiki


def test_a_closing_bracket_that_belongs_to_the_link_is_kept():
    # P19 fix round 1 (M6): an IPv6 host ending the link
    assert extract_urls("看 http://[::1]。") == ["http://[::1]"]
    assert extract_urls("[见 https://a.com/x]") == ["https://a.com/x"]


# P37: the 小红书 iOS app shares its short link on xhslink.cn (made-up code)

XHS_CN_SHARE = "周末 南瓜、山药…都是厨房... {} 先复制这段内容，再进入【小红书】即可阅读笔记。"


def test_the_xiaohongshu_apps_xhslink_cn_share_text_gives_its_link():
    assert extract_urls(XHS_CN_SHARE.format("https://xhslink.cn/o/9Zx8Yw7Vu6T")) == ["https://xhslink.cn/o/9Zx8Yw7Vu6T"]


def test_a_bare_xhslink_cn_link_in_share_text_is_a_link():
    assert extract_urls(XHS_CN_SHARE.format("xhslink.cn/o/9Zx8Yw7Vu6T")) == ["https://xhslink.cn/o/9Zx8Yw7Vu6T"]
