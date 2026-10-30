"""P14: a pasted article or a /s note that contains a link is saved as text; its links are not read."""
import itertools

import pytest
from fakes import StubFetcher
from test_app_media import m, make_app, staged

from octopus.app import LINKS_UNREAD, PASTE_MIN_CHARS
from octopus.fetchers.base import FetchResult
from octopus.models import IncomingMessage

LINK = "https://github.com/example-org/sample-repo"
ARTICLE = ("短视频七步法：第一步，找到你的定位，想清楚你要服务谁、解决什么问题。第二步，拆解对标账号，"
           "看他们的选题、开头三秒、节奏和结尾的引导。第三步，建立自己的选题库，每天记录十个灵感。") * 4
PASTE = f"{ARTICLE}\n项目地址：{LINK}’\n第四步，开拍。"
EXCERPT = ("作者的意思是：先想清楚你要服务谁，再去拆解三到五个对标账号，看他们的选题、开头三秒、节奏和结尾的引导，"
           "最后每天记十个灵感，攒够一百个再开拍。这一段是原文里最关键的部分，我先贴过来，截图稍后再发。")  # 80-299 chars
DOUYIN_LONG = ("4.20 复制打开抖音，看看【小王的作品】短视频七步法第二集：怎么拆解对标账号，从选题到开头三秒到结尾引导，"
               "一步一步讲清楚 # 短视频 # 运营 # 干货 https://v.douyin.com/iRNBabc2/ Kqm:/ 07/25 T@p.Rw")
DOUYIN = ("7.43 复制打开抖音，看看【小王的作品】短视频七步法，从零开始做账号 # 短视频 # 运营 "
          "https://v.douyin.com/iRNBxyz1/ Kqm:/ 07/25 T@p.Rw")
XHS = ("小王 发布了一篇小红书笔记，快来看吧！😆 7wGcKq1Ab2Cd3Ef 😆 http://xhslink.com/a/AbCdEf123 "
       "复制本条信息，打开【小红书】App查看精彩内容！")


def kinds(app) -> list[str]:
    return [r[0] for r in app.conn.execute("SELECT kind FROM jobs ORDER BY id")]


def quote(text, reply_to):
    return IncomingMessage(channel="tg", user_id="me", text=text, reply_to_id=reply_to)


async def _failed_link(app, ch) -> str:
    """The link saved as needs_manual (no fetcher): its window is open; returns the entry id."""
    await app.handle(m(LINK))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("⚠️ 没能获取内容")
    return app.pending.active("tg", "me")


# ---- R1: /s and 存：with text are a note ----

async def test_save_prefix_with_text_and_a_link_saves_the_whole_text(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(f"/s 这个项目回头试试 {LINK}"))
    assert kinds(app) == ["note"]
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    entry = app.library.get(row["id"])
    assert f"这个项目回头试试 {LINK}" in entry.body and entry.url is None
    assert ch.sent[-1][1].endswith(f"id：{row['id']}\n{LINKS_UNREAD}")


async def test_save_prefix_with_only_a_link_still_reads_it(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(f"存：{LINK}"))
    assert kinds(app) == ["url"]
    await app.process_due_jobs()
    assert LINKS_UNREAD not in ch.sent[-1][1]


async def test_save_prefix_with_text_a_link_and_a_photo(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(f"/s 看这个 {LINK}", [staged(tmp_path, "a.png")]))
    assert kinds(app) == ["attachments"]
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    assert LINK in app.library.get(row["id"]).body and ch.sent[-1][1].endswith(LINKS_UNREAD)


async def test_a_note_without_links_gets_no_extra_line(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 明天给老王回电话"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存") and LINKS_UNREAD not in ch.sent[-1][1]


# ---- R2: a long paste with a link is a note; share blurbs are still links ----

async def test_a_pasted_article_with_a_link_is_saved_as_text(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(PASTE))
    assert kinds(app) == ["note"]
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    body = app.library.get(row["id"]).body
    assert ARTICLE[:40] in body and "第四步，开拍。" in body and LINK in body
    assert ch.sent[-1][1].endswith(LINKS_UNREAD)


@pytest.mark.parametrize("text", [DOUYIN, XHS, f"看看这个 {LINK}"])
async def test_share_blurbs_are_still_read_as_links(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(text))
    assert kinds(app) == ["url"]
    await app.process_due_jobs()
    assert LINKS_UNREAD not in ch.sent[-1][1]


async def test_a_long_at_question_with_a_link_is_still_a_question(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    await app.handle(m(f"@{eid} 和这篇比怎么样：{PASTE}"))
    assert kinds(app) == ["note"]  # only the first note: the question saved nothing
    assert app.convos.recent("tg", "me", 1)[0][0] == "user"


# ---- R3: adding text to an entry never refetches its link ----

async def test_a_long_paste_with_the_windows_link_is_merged_not_refetched(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await _failed_link(app, ch)
    await app.handle(m(PASTE))
    assert kinds(app) == ["url", "merge"]
    await app.process_due_jobs()
    assert ARTICLE[:40] in app.library.get(eid).body and app.index.count() == 1
    assert ch.sent[-1][1].startswith("✅ 已补充到") and ch.sent[-1][1].endswith(LINKS_UNREAD)


async def test_the_windows_link_alone_is_still_refetched(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await _failed_link(app, ch)
    await app.handle(m(LINK))
    assert kinds(app) == ["url", "refetch"] and app.pending.active("tg", "me") == eid


async def test_a_quote_reply_with_text_and_the_link_is_merged_not_refetched(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await _failed_link(app, ch)
    prompt = f"m{ch._n}"
    await app.handle(quote(f"原文在这：作者说先定位再拆解 {LINK}", prompt))
    assert kinds(app) == ["url", "merge"]
    await app.process_due_jobs()
    assert "作者说先定位再拆解" in app.library.get(eid).body and ch.sent[-1][1].endswith(LINKS_UNREAD)


async def test_a_quote_reply_with_only_the_link_is_refetched(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await _failed_link(app, ch)
    await app.handle(quote(LINK, f"m{ch._n}"))
    assert kinds(app) == ["url", "refetch"]


async def test_add_with_text_and_a_link_adds_the_text(tmp_path, monkeypatch):
    # an id ending in digits: "补充 <4 letters>" is chat, not the alias (ADD_ALIAS wants a digit)
    n = itertools.count(1)
    monkeypatch.setattr("octopus.pipeline.new_entry_id", lambda now: f"{now:%Y%m%d}-{next(n):06d}")
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    for text in (f"/add {eid[-4:]} 相关项目 {LINK}", f"补充 {eid[-4:]} 另一个 https://x.com/a/status/1"):
        await app.handle(m(text))
        await app.process_due_jobs()
        assert ch.sent[-1][1].startswith("✅ 已补充到") and ch.sent[-1][1].endswith(LINKS_UNREAD), text
    body = app.library.get(eid).body
    assert f"相关项目 {LINK}" in body and "另一个 https://x.com/a/status/1" in body
    assert kinds(app) == ["note", "merge", "merge"] and app.index.count() == 1


async def test_add_with_text_and_no_link_gets_no_extra_line(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    await app.handle(m(f"/add {app.focus.get('tg', 'me')} 一段补充"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到") and LINKS_UNREAD not in ch.sent[-1][1]


async def test_a_long_paste_with_a_link_is_an_add_windows_first_text(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    await app.handle(m(f"/add {eid}"))
    await app.handle(m(PASTE))  # P15: merged as text, the window stays
    assert kinds(app) == ["note", "merge"] and app.pending.active("tg", "me") == eid
    await app.handle(m(PASTE))  # a second text: ends it and is its own note (P14)
    assert kinds(app) == ["note", "merge", "note"] and app.pending.active("tg", "me") is None


async def test_a_long_paste_with_another_link_in_the_window_is_merged_too(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await _failed_link(app, ch)
    await app.handle(m(f"{ARTICLE}\n参考：https://x.com/a/status/1"))
    assert kinds(app) == ["url", "merge"]
    await app.process_due_jobs()
    assert "https://x.com/a/status/1" in app.library.get(eid).body and ch.sent[-1][1].endswith(LINKS_UNREAD)


# ---- fix round 1, F1: in the window, an 80+ char excerpt with the window's own link is merged ----

async def test_an_excerpt_with_the_windows_link_is_merged_not_refetched(tmp_path):
    assert PASTE_MIN_CHARS <= len(EXCERPT) < 300
    app, ch, _ = make_app(tmp_path)
    eid = await _failed_link(app, ch)
    await app.handle(m(f"{EXCERPT}\n{LINK}"))
    assert kinds(app) == ["url", "merge"]
    await app.process_due_jobs()
    assert EXCERPT[:30] in app.library.get(eid).body and app.index.count() == 1
    assert ch.sent[-1][1].startswith("✅ 已补充到") and ch.sent[-1][1].endswith(LINKS_UNREAD)


async def test_a_share_blurb_of_another_link_in_the_window_is_a_new_link(tmp_path):
    assert len(DOUYIN_LONG) - len("https://v.douyin.com/iRNBabc2/") >= PASTE_MIN_CHARS
    app, ch, _ = make_app(tmp_path)
    await _failed_link(app, ch)
    await app.handle(m(DOUYIN_LONG))
    assert kinds(app) == ["url", "url"]


@pytest.mark.parametrize("text", [f"再试一次 {LINK}", f"再试一次\n{LINK}"])
async def test_a_short_retry_with_the_windows_link_is_refetched(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await _failed_link(app, ch)
    await app.handle(m(text))
    assert kinds(app) == ["url", "refetch"]


async def test_an_excerpt_with_a_partly_read_links_url_is_merged(tmp_path):
    fetcher = StubFetcher(FetchResult(url=LINK, source="web", title="项目", text="很短", status="partial",
                                      note="只拿到摘要"), handles="github.com")
    app, ch, _ = make_app(tmp_path, fetcher)
    await app.handle(m(LINK))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    assert eid and app.library.get(eid).status == "partial"
    await app.handle(m(f"{EXCERPT} {LINK}"))
    assert kinds(app) == ["url", "merge"]
    await app.process_due_jobs()
    assert EXCERPT[:30] in app.library.get(eid).body and ch.sent[-1][1].startswith("✅ 已补充到")
