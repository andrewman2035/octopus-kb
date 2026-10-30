"""P36: a link of a platform that needs the browser the user is logged in to (or one that read as a login wall) says
how the Chrome extension saves it; the extension's page fills an entry saved earlier as partial (its id kept)."""
import json

import pytest
from fakes import StubFetcher
from test_app import m, make_app
from test_extension_saves import URL, build, items, page
from test_pipeline_page import ARTICLE_HTML, SHORT_HTML, SHORT_POST, WebStub

from octopus import i18n
from octopus.fetchers import needs_browser
from octopus.fetchers.base import FetchError, FetchResult
from octopus.i18n import t
from octopus.models import IncomingMessage

HINT = "在电脑上用 Chrome 打开这个页面（需要的话先登录），点 Octopus 插件的「存这个页面」就能存全文。"
INSTALL = "装上 Octopus 的 Chrome 插件后，在电脑上登录并打开这个页面就能存全文（见 README）。"
FILLED = "已补全之前存的那一条（id 不变）。"
ZHIHU = "https://zhuanlan.zhihu.com/p/123"
XHS = "https://www.xiaohongshu.com/explore/abc"


# ---- the table ----

@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/x/comments/1/", "https://old.reddit.com/r/x", "https://redd.it/abc",
    "https://www.xiaohongshu.com/explore/abc", "http://xhslink.com/a/b", "https://www.zhihu.com/question/1",
    ZHIHU, "https://www.instagram.com/p/x/", "https://www.facebook.com/a/posts/1", "https://www.linkedin.com/posts/x",
    "https://www.threads.net/@a/post/x", "https://www.threads.com/@a/post/x", "https://medium.com/@a/b-1",
    "https://weibo.com/1/x", "https://m.weibo.cn/status/1",
])
def test_these_need_the_browser(url):
    assert needs_browser(url)


@pytest.mark.parametrize("url", [
    "https://a.com/x", "https://notzhihu.com/x", "https://channels.weixin.qq.com/x", "https://weixin.qq.com/sph/x",
    "https://mp.weixin.qq.com/s/x", "https://www.youtube.com/watch?v=x", "https://x.com/a/status/1",
])
def test_these_do_not(url):
    assert not needs_browser(url)


# ---- the chat reply's line ----

def refused():
    return StubFetcher(FetchError("网站拒绝访问（HTTP 403）", retryable=False))


async def saved_reply(app, ch, url):
    await app.handle(m(url))
    await app.process_due_jobs()
    return ch.sent[-1][1]


async def test_a_refused_zhihu_link_says_how_to_install_the_extension(tmp_path):
    app, ch, _ = make_app(tmp_path, refused())
    text = await saved_reply(app, ch, ZHIHU)
    assert text.startswith(f"⚠️ 没能获取内容：{ZHIHU}") and "HTTP 403" in text
    assert t("formatting.merge_ask") in text  # the screenshot / paste suggestion stays
    assert text.endswith("\n" + INSTALL) and HINT not in text


async def test_with_the_extension_on_it_says_to_save_the_page_with_it(tmp_path):
    app, ch, _ = make_app(tmp_path, refused())
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, ZHIHU)
    assert text.endswith("\n" + HINT) and INSTALL not in text


async def test_a_partial_xiaohongshu_link_gets_the_line_after_its_supplement_ask(tmp_path):
    footer = FetchResult(url=XHS, source="web", title="小红书", text="© 2014-2026 行吟信息科技", status="partial",
                         note=t("fetch.short"))
    app, ch, _ = make_app(tmp_path, StubFetcher(footer))
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, XHS)
    assert text.startswith("⚠️ 已保存（内容不完整）") and t("formatting.supplement_ask") in text
    assert text.endswith("\n" + HINT)


async def test_reddit_keeps_its_manual_note_and_gets_the_line(tmp_path):
    app, ch, _ = make_app(tmp_path)
    text = await saved_reply(app, ch, "https://www.reddit.com/r/x/comments/1/")
    assert t("pipeline.manual_note") in text and text.endswith("\n" + INSTALL)


async def test_wechat_channels_keeps_only_its_own_words(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, "https://channels.weixin.qq.com/x")
    assert t("pipeline.manual_note") in text and HINT not in text and INSTALL not in text


async def test_any_page_that_read_as_a_login_wall_gets_the_line(tmp_path):
    wall = FetchResult(url="https://a.com/x", source="web", title="帖子", text="登录后查看全文", status="partial",
                       note=t("fetch.short"))
    app, ch, _ = make_app(tmp_path, StubFetcher(wall))
    text = await saved_reply(app, ch, "https://a.com/x")
    assert text.endswith("\n" + INSTALL)


async def test_a_short_page_elsewhere_gets_no_line(tmp_path):
    short = FetchResult(url="https://a.com/x", source="web", title="帖子", text=SHORT_POST, status="partial",
                        note=t("fetch.short"))
    app, ch, _ = make_app(tmp_path, StubFetcher(short))
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, "https://a.com/x")
    assert text.startswith("⚠️ 已保存（内容不完整）") and HINT not in text and INSTALL not in text


async def test_a_zhihu_article_that_was_read_gets_no_line(tmp_path):
    article = FetchResult(url=ZHIHU, source="web", title="专栏", text="长期主义的投资者相信复利。" * 30)
    app, ch, _ = make_app(tmp_path, StubFetcher(article))
    text = await saved_reply(app, ch, ZHIHU)
    assert text.startswith("✅ 已保存") and INSTALL not in text


async def test_in_english(tmp_path):
    app, ch, _ = make_app(tmp_path, refused())
    i18n.set_language("en")
    text = await saved_reply(app, ch, ZHIHU)
    assert text.endswith("\nWith Octopus's Chrome extension installed, log in and open this page on your computer to "
                         "save the full text (see the README).")
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, "https://www.zhihu.com/question/1")
    assert text.endswith("\nOn your computer, open this page in Chrome (log in first if needed) and click Save this "
                         "page in the Octopus extension to save the full text.")


# ---- the extension fills a partial entry ----

PARTIAL = FetchResult(url=URL, source="web", title="抓到的标题", text=SHORT_POST, status="partial",
                      note="只拿到了标题或很短的内容")


async def partial_from_chat(app):
    app.web.result = PARTIAL
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    assert app.library.get(first).status == "partial"
    return first


async def test_the_page_fills_an_entry_saved_partial(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page(telegram=True))
    [(kind, payload)] = app.conn.execute("SELECT kind, payload FROM jobs WHERE status = 'queued'").fetchall()
    assert kind == "refetch" and json.loads(payload)["entry_id"] == first
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert (entry.status, entry.title) == ("ok", "页面标题") and "长期主义的投资者相信复利" in entry.body
    assert SHORT_POST not in entry.body
    assert app.web.calls == [URL]  # the chat link's own fetch only
    assert len(app.index.recent(10)) == 1
    [item] = await items(intake)
    assert (item["state"], item["entry_id"], item["filled"]) == ("已存", first, t("extension.filled"))
    assert not item["incomplete"]
    [(_, text)] = app.tg.sent
    assert FILLED in text and f"id：{first}" in text and INSTALL not in text and HINT not in text


async def test_a_chat_resend_of_the_partial_link_is_still_a_duplicate(tmp_path):
    app, _ = build(tmp_path)
    first = await partial_from_chat(app)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    assert app.chat.sent[-1][1] == t("formatting.duplicate", title=app.library.get(first).title, id=first)
    assert app.jobs.claim_due(5) == []


async def test_a_page_save_without_its_html_is_a_duplicate(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page(html=None))
    [item] = await items(intake)
    assert (item["state"], item["entry_id"]) == ("已存", first) and "filled" not in item
    assert app.jobs.claim_due(5) == []


async def test_a_page_that_reads_no_better_leaves_the_entry_as_it_was(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    before = app.library.get(first)
    await intake.save(page(html=SHORT_HTML))  # a login wall: the link is fetched again, as partial as before
    await app.process_due_jobs()
    after = app.library.get(first)
    assert (after.status, after.body, after.title) == ("partial", before.body, before.title)
    [item] = await items(intake)
    assert item["entry_id"] == first and item["incomplete"] and "filled" not in item


async def test_a_users_own_partial_entry_with_that_link_is_never_replaced(tmp_path):
    app, intake = build(tmp_path)
    out = await app.pipeline.store(FetchResult(url=URL, source="manual", text="图片没能识别", status="partial"), "tg")
    await intake.save(page())
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != out.entry.id and "filled" not in item  # the page is an entry of its own
    assert app.library.get(out.entry.id).body == "图片没能识别"


async def test_a_needs_manual_entry_filled_from_the_page_says_so_too(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", status="needs_manual", note="要登录")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    await intake.save(page(telegram=True))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert (item["entry_id"], item["filled"]) == (first, t("extension.filled"))
    assert FILLED in app.tg.sent[-1][1]


async def test_an_extension_save_never_gets_the_extension_line(tmp_path):
    app, intake = build(tmp_path, [WebStub(FetchResult(url=ZHIHU, source="web", status="needs_manual", note="要登录"))])
    app.cfg.extension.enabled = True
    await intake.save(page(url=ZHIHU, html=SHORT_HTML, telegram=True))
    await app.process_due_jobs()
    [(_, text)] = app.tg.sent
    assert HINT not in text and INSTALL not in text


# ---- fix round 1 ----

# I-1: what the owner added stays

async def test_a_title_set_with_title_is_kept_by_the_fill(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await app.pipeline.retitle(first, "我自己起的标题")
    assert app.library.get(first).title_set
    await intake.save(page())
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert (entry.title, entry.status) == ("我自己起的标题", "ok") and "长期主义的投资者相信复利" in entry.body
    assert "title_set: true" in app.library.find(first).read_text(encoding="utf-8")


async def test_a_body_edited_by_hand_stays_and_the_page_goes_below_it(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    entry = app.library.get(first)
    entry.body += "\n\n我手动补的一段很重要的内容。"
    app.library.save(entry)
    await intake.save(page(telegram=True))
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert entry.status == "ok" and SHORT_POST in entry.body and "我手动补的一段很重要的内容。" in entry.body
    mine, _, added = entry.body.partition("## 页面内容（插件）")
    assert "我手动补的" in mine and "长期主义的投资者相信复利" in added
    assert entry.fetched is None and FILLED in app.tg.sent[-1][1]


async def test_an_untouched_body_is_replaced_and_its_sha_kept_while_partial(tmp_path):
    from octopus.pipeline import body_sha

    app, _ = build(tmp_path)
    first = await partial_from_chat(app)
    entry = app.library.get(first)
    assert entry.fetched == body_sha(entry.body) and "fetched: " in app.library.find(first).read_text(encoding="utf-8")


async def test_the_supplements_stay_and_the_length_guard_looks_before_them(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await app.pipeline.merge(first, [], "我补充的文字" * 40)  # longer than the page's text
    entry = app.library.get(first)
    entry.status = "partial"  # say a screenshot in it could not be read
    app.library.save(entry)
    await app.index.upsert(entry, app.library.find(first))
    longer = "<html><head><title>帖子</title></head><body><article><p>" + SHORT_POST * 2 + "</p></article></body></html>"
    app.web.result = FetchResult(url=URL, source="web", title="抓到的标题", text="", status="needs_manual", note="要登录")
    await intake.save(page(html=longer, telegram=True))
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert entry.body.startswith(SHORT_POST * 2) and "我补充的文字" in entry.body
    assert entry.body.count(SHORT_POST) == 2  # the text before the supplement was replaced, not added to
    [item] = await items(intake)
    assert item["filled"] == t("extension.filled_partial") and item["incomplete"]
    assert t("formatting.filled_partial") in app.tg.sent[-1][1]


@pytest.mark.parametrize("old,replaced", [("登录后查看全文", True), ("和页面无关的一段话，是我自己写的。", False)])
async def test_an_entry_from_before_the_sha_is_replaced_only_when_nothing_of_it_is_lost(tmp_path, old, replaced):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    entry = app.library.get(first)
    entry.body, entry.fetched = old, None
    app.library.save(entry)
    await intake.save(page())
    await app.process_due_jobs()
    body = app.library.get(first).body
    assert (old in body) is not replaced and "长期主义的投资者相信复利" in body


async def test_a_partial_entry_whose_summary_failed_is_filled_too(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", title="抓到的标题", text="抓到的正文" * 100)
    for p in app.providers.values():
        p.error = "down"
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    assert app.library.get(first).status == "partial"
    for p in app.providers.values():
        p.error = None
    await intake.save(page())
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert entry.status == "ok" and entry.summary == "这是摘要。"


# m-1: the popup says "filled" only when it happened

async def test_supplemented_meanwhile_is_not_filled(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page(telegram=True))
    await app.pipeline.merge(first, [], "我补充的文字")  # ok now: the page is not used
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] == first and "filled" not in item
    assert FILLED not in app.tg.sent[-1][1] and SHORT_POST in app.library.get(first).body


async def test_deleted_meanwhile_is_a_new_entry_not_a_fill(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page(telegram=True))
    await app.pipeline.delete(first)
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != first and "filled" not in item and FILLED not in app.tg.sent[-1][1]


# m-3: files only the replaced text linked go to the trash

async def test_files_of_the_replaced_text_go_to_the_trash(tmp_path):
    from octopus.pipeline import body_sha

    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    folder = app.cfg.attachments_dir / first
    folder.mkdir(parents=True)
    (folder / "old.jpg").write_bytes(b"jpg")
    entry = app.library.get(first)
    entry.body = f"{entry.body}\n\n{t('note.attach_line')}attachments/{first}/old.jpg"
    entry.fetched = body_sha(entry.body)
    app.library.save(entry)
    await intake.save(page())
    await app.process_due_jobs()
    assert "old.jpg" not in app.library.get(first).body and not (folder / "old.jpg").exists()
    assert (app.library.trash / f"{first}-replaced" / "old.jpg").read_bytes() == b"jpg"


# m-4: the chat's window on the entry ends with the fill

async def test_the_fill_ends_the_chat_window_on_the_entry(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    assert app.pending.active("tg", "me") == first
    await intake.save(page())
    await app.process_due_jobs()
    assert app.pending.active("tg", "me") is None


# m-5: only a link's reply gets the line

async def test_a_supplement_that_stays_partial_gets_no_line(tmp_path, monkeypatch):
    from octopus.media import MediaError
    from octopus.models import Attachment, attachment_to_dict

    app, _ = build(tmp_path)
    reddit = "https://www.reddit.com/r/x/comments/abc/t/"
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=reddit))
    await app.process_due_jobs()
    assert INSTALL in app.chat.sent[-1][1]
    eid = app.index.find_by_url(reddit)

    def broken(path):
        raise MediaError("看不清")

    monkeypatch.setattr(app.pipeline.media, "ocr", broken)
    shot = app.media.incoming_dir / "tg-1" / "shot.png"  # staged as a channel stages it
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG")
    app.jobs.enqueue("merge", {"entry_id": eid, "text": "截图里是帖子", "fallback_id": app.pipeline.reserve_entry_id(),
                               "files": [attachment_to_dict(Attachment("image", shot, "shot.png", "image/png"))]},
                     "tg", "me")
    await app.process_due_jobs()
    assert app.library.get(eid).status == "partial"
    assert INSTALL not in app.chat.sent[-1][1] and HINT not in app.chat.sent[-1][1]


# m-6: short and alternate domains

@pytest.mark.parametrize("url", ["https://lnkd.in/abc", "https://fb.watch/x", "https://fb.com/x/posts/1",
                                 "https://instagr.am/p/x", "https://t.cn/A6abc", "https://www.reddit.com./r/x"])
def test_short_domains_need_the_browser_too(url):
    assert needs_browser(url)


# I-2: the phone's share link and the browser tab's link are one entry

XHS_ID = "66b1a0b2000000001e01d0d0"


def test_one_post_one_key():
    from octopus.urls import same_page

    xhs = f"https://www.xiaohongshu.com/explore/{XHS_ID}"
    assert same_page(f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?xsec_token=A") == xhs
    assert same_page(f"https://xiaohongshu.com/explore/{XHS_ID}?xsec_token=B&xsec_source=pc_feed") == xhs
    post = "https://www.reddit.com/comments/abc12"
    assert same_page("https://www.reddit.com/r/x/comments/abc12/a_title/") == post
    assert same_page("https://old.reddit.com/comments/ABC12") == post == same_page("https://redd.it/abc12")
    assert same_page("https://www.threads.net/@a/post/C1") == same_page("https://www.threads.com/@a/post/C1/")
    assert same_page("https://weibo.com/1669879400/O1aBcDeFg") == same_page("https://m.weibo.cn/status/O1aBcDeFg")
    assert same_page("https://m.weibo.cn/detail/4900000000000000") == "weibo:4900000000000000"
    for other in ("https://www.reddit.com/r/x/s/AbCd", "https://a.com/explore/x", "https://www.xiaohongshu.com/",
                  "https://weibo.com/123456/profile", "https://weibo.com/123456/abcdefghij", "https://weibo.com/1/12"):
        assert same_page(other) is None


def test_page_aliases_keep_only_other_pages():
    from octopus.urls import page_aliases

    assert page_aliases("https://a.com/p/1", [
        "https://a.com/p/1", "https://a.com/p/1?utm_source=x", "https://a.com/", "https://www.zhihu.com/signin?next=/",
        "https://passport.weibo.com/visitor/visitor?url=x", "https://www.xiaohongshu.com/404?redirectPath=x",
        "https://a.com/p/one", "https://a.com/p/one", "not a link", None]) == ["https://a.com/p/one"]


async def test_an_entry_is_found_by_its_aliases_and_their_keys_until_deleted(tmp_path):
    app, _ = build(tmp_path)
    out = await app.pipeline.store(FetchResult(url="http://xhslink.com/a/abc", source="web", title="t", text="x",
                                               aliases=[f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?x=1"]),
                                   "tg")
    eid = out.entry.id
    assert app.index.find_by_url(f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?x=1") == eid
    assert app.index.find_by_url(f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B") == eid
    assert app.library.get(eid).url_aliases == [f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?x=1"]
    await app.pipeline.delete(eid)
    assert app.index.find_by_url(f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B") is None


async def test_an_entry_saved_before_aliases_is_found_by_its_posts_key(tmp_path):
    app, _ = build(tmp_path)
    out = await app.pipeline.store(FetchResult(url=f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=A",
                                               source="web", title="t", text="x"), "tg")
    assert app.index.find_by_url(f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B") == out.entry.id
    assert app.index.find_by_url("https://www.xiaohongshu.com/explore/" + "0" * 24) is None


async def test_the_phones_share_link_is_filled_from_the_browser_tab(tmp_path):
    import httpx
    import respx

    from octopus.fetchers.web import WebFetcher

    share = "http://xhslink.com/a/abc"
    item_url = f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?app_platform=ios&xsec_token=A"
    footer = "<html><head><title>小红书</title></head><body><p>© 2014-2026 行吟信息科技</p></body></html>"
    app, intake = build(tmp_path, [WebFetcher()])
    with respx.mock:
        respx.get(share).mock(return_value=httpx.Response(302, headers={"Location": item_url}))
        respx.get(url__startswith="https://www.xiaohongshu.com/discovery/").mock(return_value=httpx.Response(
            302, headers={"Location": "https://www.xiaohongshu.com/404?redirectPath=x"}))
        respx.get(url__startswith="https://www.xiaohongshu.com/404").mock(return_value=httpx.Response(
            200, html=footer))
        await app.handle(IncomingMessage(channel="tg", user_id="me", text=share))
        await app.process_due_jobs()
    first = app.index.find_by_url(share)
    entry = app.library.get(first)
    assert entry.status == "partial" and entry.url_aliases == [
        f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?app_platform=ios&xsec_token=A"]
    tab = f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B&xsec_source=pc_feed"
    await intake.save(page(url=tab))
    await app.process_due_jobs()
    [item] = await items(intake)
    entry = app.library.get(first)
    assert (item["entry_id"], item["filled"]) == (first, t("extension.filled")) and entry.status == "ok"
    assert f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B" in entry.url_aliases
    assert len(app.index.recent(10)) == 1


async def test_the_pages_canonical_finds_the_entry_saved_under_it(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    amp = ARTICLE_HTML.replace("<head>", f'<head><link rel="canonical" href="{URL}">')
    await intake.save(page(url="https://a.com/amp/x", html=amp))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert (item["entry_id"], item["filled"]) == (first, t("extension.filled"))
    assert app.index.find_by_url("https://a.com/amp/x") == first


async def test_a_reddit_app_share_link_is_not_matched(tmp_path):
    # documented limit: the /s/ link carries no post id and Reddit is never fetched, so nothing resolves it
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="https://www.reddit.com/r/x/s/AbCd"))
    await app.process_due_jobs()
    await intake.save(page(url="https://www.reddit.com/r/x/comments/abc12/t/"))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert "filled" not in item and len(app.index.recent(10)) == 2


# ---- fix round 2 ----

def article(title, text, canonical):
    return (f'<html><head><title>{title}</title><link rel="canonical" href="{canonical}"></head>'
            f'<body><article><h1>{title}</h1><p>{text}</p></article></body></html>')


A_TEXT = "第一篇文章讲的是复利和长期主义。" * 30
B_TEXT = "完全不同的第二篇文章内容，讲的是另一件事。" * 30


async def test_pages_sharing_a_section_canonical_are_two_entries(tmp_path):
    # R-I1: a canonical never answers "already saved"
    app, intake = build(tmp_path)
    section = "https://news.example.com/world/"
    await intake.save(page(url="https://news.example.com/world/a1", html=article("文章一", A_TEXT, section)))
    await app.process_due_jobs()
    await intake.save(page(url="https://news.example.com/world/b2", html=article("文章二", B_TEXT, section)))
    await app.process_due_jobs()
    b, a = await items(intake)
    assert a["entry_id"] != b["entry_id"] and len(app.index.recent(10)) == 2
    assert "完全不同的第二篇" in app.library.get(b["entry_id"]).body and "filled" not in b


async def test_a_canonical_naming_an_ok_entry_is_saved_as_its_own(tmp_path):
    app, intake = build(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    await intake.save(page(url="https://b.com/copy", html=article("转载", B_TEXT, URL)))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != first and "filled" not in item and len(app.index.recent(10)) == 2


async def test_a_shared_canonical_never_fills_another_partial_entry(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)  # https://a.com/x
    await intake.save(page(url=URL, html=article("A", A_TEXT, "https://a.com/section")))
    await app.process_due_jobs()
    await intake.save(page(url="https://a.com/x2", html=article("B", B_TEXT, "https://a.com/section")))
    await app.process_due_jobs()
    b, a = await items(intake)
    assert a["entry_id"] == first and b["entry_id"] != first
    assert "完全不同的第二篇" not in app.library.get(first).body


async def test_a_canonical_that_is_a_parent_of_the_page_is_ignored(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url="https://a.com/blog", source="web", title="t", text="短", status="partial")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="https://a.com/blog"))
    await app.process_due_jobs()
    blog = app.index.find_by_url("https://a.com/blog")
    await intake.save(page(url="https://a.com/blog/post-1", html=article("文章", B_TEXT, "https://a.com/blog")))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != blog and app.library.get(blog).status == "partial"


async def test_a_canonical_entry_completed_meanwhile_gets_the_page_as_a_new_entry(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page(url="https://a.com/amp/x", html=article("A", A_TEXT, URL)))
    await app.pipeline.merge(first, [], "我补充的文字")  # ok now
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != first and "filled" not in item and len(app.index.recent(10)) == 2


async def test_a_title_edited_in_obsidian_is_kept_by_the_fill(tmp_path):
    # R-m1: the title a partial entry was saved with is recorded; another one now is the owner's
    from octopus.pipeline import body_sha

    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    entry = app.library.get(first)
    assert entry.fetched_title == body_sha(entry.title)
    entry.title = "我在 Obsidian 里改的标题"
    app.library.save(entry)
    await intake.save(page())
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert (entry.title, entry.status) == ("我在 Obsidian 里改的标题", "ok") and entry.fetched_title is None


async def test_an_untouched_title_is_replaced_by_the_pages(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    await intake.save(page())
    await app.process_due_jobs()
    assert app.library.get(first).title == "页面标题"


async def test_the_platform_is_picked_by_the_keys_site(tmp_path):
    # R-m2: a Threads user named like another platform still finds the Threads entry
    app, _ = build(tmp_path)
    out = await app.pipeline.store(FetchResult(url="https://www.threads.net/@xiaohongshu_fan/post/Cx1", source="web",
                                               title="t", text="x"), "tg")
    assert app.index.find_by_url("https://www.threads.com/@xiaohongshu_fan/post/Cx1") == out.entry.id


async def test_an_owners_title_stays_theirs_through_a_fill_that_is_still_partial(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    entry = app.library.get(first)
    entry.title = "我改的标题"
    app.library.save(entry)
    longer = "<html><head><title>帖子</title></head><body><article><p>" + SHORT_POST * 2 + "</p></article></body></html>"
    app.web.result = FetchResult(url=URL, source="web", text="", status="needs_manual", note="要登录")
    await intake.save(page(html=longer))
    await app.process_due_jobs()
    assert (app.library.get(first).title, app.library.get(first).status) == ("我改的标题", "partial")
    await intake.save(page())
    await app.process_due_jobs()
    assert (app.library.get(first).title, app.library.get(first).status) == ("我改的标题", "ok")


# ---- fix round 3 ----

async def test_a_canonical_entry_completed_during_the_fetch_leaves_the_page_its_own_entry(tmp_path):
    # R2-I1: the merge lands while the page is being read; the page is stored as read, not fetched again
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    read = app.pipeline._fetch

    async def fetch_then_merge(*args, **kwargs):
        r = await read(*args, **kwargs)
        await app.pipeline.merge(first, [], "主人在这时补充了文字")
        return r

    app.pipeline._fetch = fetch_then_merge
    text = "另一个地址上的同一篇文章的全文。" * 30
    await intake.save(page(url="https://m.a.com/x", html=article("移动版", text, URL), title="移动版"))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert len(app.index.recent(10)) == 2 and item["entry_id"] != first and "filled" not in item
    assert "另一个地址上的同一篇" in app.library.get(item["entry_id"]).body
    assert "另一个地址上的同一篇" not in app.library.get(first).body
    assert app.web.calls == [URL]  # the chat link's own fetch only: the page was not read twice


async def test_a_canonical_entry_edited_during_the_summary_leaves_the_page_its_own_entry(tmp_path):
    app, intake = build(tmp_path)
    first = await partial_from_chat(app)
    summarize = app.pipeline._summarize_into

    async def edit_then_summarize(entry, title_given):
        if entry.id == first:
            mine = app.library.get(first)
            mine.body += "\n\n我在 Obsidian 里加的一句。"
            app.library.save(mine)
        return await summarize(entry, title_given)

    app.pipeline._summarize_into = edit_then_summarize
    text = "另一个地址上的同一篇文章的全文。" * 30
    await intake.save(page(url="https://m.a.com/x", html=article("移动版", text, URL), title="移动版"))
    await app.process_due_jobs()
    [item] = await items(intake)
    assert item["entry_id"] != first and "filled" not in item
    assert "另一个地址上的同一篇" in app.library.get(item["entry_id"]).body
    assert "我在 Obsidian 里加的一句。" in app.library.get(first).body


async def test_a_title_the_model_gave_in_a_merge_is_still_replaced_by_the_fill(tmp_path):
    # R2-m3: the record follows a title Octopus itself changes
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=URL, source="web", text=SHORT_POST, status="partial", note="短")
    for p in app.providers.values():
        p.error = "down"
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=URL))
    await app.process_due_jobs()
    first = app.index.find_by_url(URL)
    assert app.library.get(first).title == URL
    for p in app.providers.values():
        p.error = None
    await app.pipeline.merge(first, [], "补充的文字")
    entry = app.library.get(first)
    assert entry.title == "摘要标题"
    entry.status = "partial"  # say a screenshot in the merge could not be read
    app.library.save(entry)
    await app.index.upsert(entry, app.library.find(first))
    await intake.save(page())
    await app.process_due_jobs()
    assert app.library.get(first).title == "页面标题"


# ---- P37: the 小红书 app's xhslink.cn share links ----

XHS_CN = "https://xhslink.cn/o/9Zx8Yw7Vu6T"
FOOTER = "© 2014-2026 行吟信息科技"


def test_xhslink_cn_needs_the_browser():
    assert needs_browser(XHS_CN) and needs_browser("http://xhslink.cn/a/b")


async def test_a_partial_xhslink_cn_link_gets_the_line(tmp_path):
    footer = FetchResult(url=XHS_CN, source="web", title="小红书", text=FOOTER, status="partial",
                         note=t("fetch.short"))
    app, ch, _ = make_app(tmp_path, StubFetcher(footer))
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, XHS_CN)
    assert text.startswith("⚠️ 已保存（内容不完整）") and text.endswith("\n" + HINT)


@pytest.mark.parametrize("aliases,line", [([f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?xsec_token=A"], True),
                                          ([], False)])
async def test_an_unknown_short_link_redirecting_to_a_browser_platform_gets_the_line(tmp_path, aliases, line):
    short = "https://xhs.example/o/AbCd"  # a short host not in the table: its recorded redirect decides
    footer = FetchResult(url=short, source="web", title="小红书", text=FOOTER, status="partial",
                         note=t("fetch.short"), aliases=aliases)
    app, ch, _ = make_app(tmp_path, StubFetcher(footer))
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, short)
    assert app.library.get(app.index.find_by_url(short)).url_aliases == aliases
    assert text.startswith("⚠️ 已保存（内容不完整）")
    assert text.endswith("\n" + HINT) == line and (HINT in text) == line


async def test_a_read_xhslink_cn_note_gets_no_line(tmp_path):
    note = FetchResult(url=XHS_CN, source="web", title="笔记", text="南瓜和山药都是好东西。" * 30,
                       aliases=[f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?xsec_token=A"])
    app, ch, _ = make_app(tmp_path, StubFetcher(note))
    app.cfg.extension.enabled = True
    text = await saved_reply(app, ch, XHS_CN)
    assert text.startswith("✅ 已保存") and HINT not in text and INSTALL not in text


async def test_the_phones_xhslink_cn_share_text_is_filled_from_the_browser_tab(tmp_path):
    import httpx
    import respx

    from octopus.fetchers.web import WebFetcher

    item_url = f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?app_platform=ios&xsec_token=A&track_code=T"
    footer = f"<html><head><title>小红书</title></head><body><p>{FOOTER}</p></body></html>"
    app, intake = build(tmp_path, [WebFetcher()])
    with respx.mock:
        respx.get(XHS_CN).mock(return_value=httpx.Response(302, headers={"Location": item_url}))
        respx.get(url__startswith="https://www.xiaohongshu.com/discovery/").mock(return_value=httpx.Response(
            302, headers={"Location": "https://www.xiaohongshu.com/404?redirectPath=x"}))
        respx.get(url__startswith="https://www.xiaohongshu.com/404").mock(return_value=httpx.Response(
            200, html=footer))
        text = f"周末 南瓜、山药…都是厨房... {XHS_CN} 先复制这段内容，再进入【小红书】即可阅读笔记。"  # the app's share text
        await app.handle(IncomingMessage(channel="tg", user_id="me", text=text))
        await app.process_due_jobs()
    first = app.index.find_by_url(XHS_CN)
    entry = app.library.get(first)
    assert entry.status == "partial" and entry.url == XHS_CN
    assert entry.url_aliases == [f"https://www.xiaohongshu.com/discovery/item/{XHS_ID}?app_platform=ios&xsec_token=A"
                                 "&track_code=T"]
    tab = f"https://www.xiaohongshu.com/explore/{XHS_ID}?xsec_token=B&xsec_source=pc_feed"
    await intake.save(page(url=tab))
    await app.process_due_jobs()
    [item] = await items(intake)
    entry = app.library.get(first)
    assert (item["entry_id"], item["filled"]) == (first, t("extension.filled")) and entry.status == "ok"
    assert len(app.index.recent(10)) == 1
