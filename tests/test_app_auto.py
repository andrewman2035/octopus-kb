"""P8: images sent one after another group by themselves; a "save the images above" text makes no entry."""
from test_app_media import make_app, m, staged
from test_app_window import URL, _with, quote

import pytest

from octopus.app import FILES_SAVED, FILES_SAVING
from octopus.models import IncomingMessage
from octopus.pending import AUTO, FILES_WINDOW_SECONDS


def idle(app, seconds):
    """The window was last restarted this many seconds earlier."""
    app.conn.execute("UPDATE pending_manual SET prompted_at = prompted_at - ?", (seconds,))


def pdf(tmp_path, name="doc.pdf"):
    return staged(tmp_path, name, kind="file")


async def test_images_sent_one_after_another_are_one_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.cfg.max_concurrent_jobs = 2
    for i in range(4):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
    assert app.pending.origin("tg", "me") == AUTO
    await app.process_due_jobs()
    assert app.index.count() == 1
    [row] = app.index.recent(10)
    assert all(f"s{i}.png" in app.library.get(row["id"]).body for i in range(4))
    assert app.pending.active("tg", "me") == row["id"] and app.pending.origin("tg", "me") == AUTO


async def test_an_image_with_a_caption_is_still_an_image(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("第二页", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 1 and len(_with(app, "a.png", "b.png")) == 1


async def test_chat_ends_an_auto_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.handle(m("这是什么意思"))
    assert app.convos.recent("tg", "me", 1)[0][1] == "这是什么意思"  # answered as chat
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "c.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 2
    [first] = _with(app, "a.png")
    assert "b.png" in first and "c.png" not in first


@pytest.mark.parametrize("text", ["你好", "/recent", "/s 一条笔记", "存：一条笔记", URL, "/new"])
async def test_any_other_message_ends_an_auto_window(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    eid = app.pending.active("tg", "me")
    assert app.pending.origin("tg", "me") == AUTO
    await app.handle(m(text))
    assert app.pending.active("tg", "me") != eid and app.pending.origin("tg", "me") != AUTO


async def test_a_file_with_no_window_is_its_own_entry_and_opens_none(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [pdf(tmp_path)]))
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 2 and _with(app, "doc.pdf", "a.png") == []


async def test_a_file_ends_an_auto_window_and_is_its_own_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [pdf(tmp_path)]))
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 3
    assert [len(_with(app, n)) for n in ("a.png", "doc.pdf", "b.png")] == [1, 1, 1]
    assert app.pending.origin("tg", "me") == AUTO  # the image after the file starts a window of its own


async def test_new_and_add_windows_still_collect_files(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [pdf(tmp_path)]))
    await app.process_due_jobs()
    assert app.index.count() == 1 and len(_with(app, "a.png", "doc.pdf")) == 1


async def test_a_failed_link_window_still_takes_the_images(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 1 and app.pending.origin("tg", "me") == "link"
    assert "a.png" in app.library.get(eid).body and "b.png" in app.library.get(eid).body


async def test_quote_reply_image_still_merges_into_the_quoted_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    link, prompt = app.pending.active("tg", "me"), f"m{ch._n}"
    app.pending.clear("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    auto = app.pending.active("tg", "me")
    await app.handle(quote("", prompt, [staged(tmp_path, "q.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert "q.png" in app.library.get(link).body and "q.png" not in app.library.get(auto).body
    assert "b.png" in app.library.get(auto).body and app.index.count() == 2


async def test_split_works_on_an_auto_window_merge(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    first = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    await app.handle(m("单独存"))
    assert ch.sent[-1][1].startswith("✅ 已另存为新的一条")
    new = app.pending.active("tg", "me")
    assert new != first and "b.png" in app.library.get(new).body and "b.png" not in app.library.get(first).body


async def test_auto_window_ends_after_five_idle_minutes_and_each_image_restarts_it(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    idle(app, FILES_WINDOW_SECONDS - 10)
    await app.handle(m("", [staged(tmp_path, "b.png")]))  # its merge has not run yet: arriving restarts it
    idle(app, FILES_WINDOW_SECONDS - 10)
    await app.handle(m("", [staged(tmp_path, "c.png")]))
    idle(app, FILES_WINDOW_SECONDS + 1)
    await app.handle(m("", [staged(tmp_path, "d.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 2
    assert len(_with(app, "a.png", "b.png", "c.png")) == 1 and "a.png" not in _with(app, "d.png")[0]


async def test_answering_a_confirmation_keeps_an_auto_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 要删的笔记"))
    await app.process_due_jobs()
    doomed = app.focus.get("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(IncomingMessage(channel="tg", user_id="me", callback=f"del:{doomed}"))
    await app.handle(m("确认"))
    assert ch.sent[-1][1].startswith("已删除")
    assert app.pending.active("tg", "me") == eid and app.pending.origin("tg", "me") == AUTO
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid


# ---- a short "save the images above" text: those images are already one entry ----

INSTRUCTIONS = ["上面这些图片的文字存成一篇文章", "把上面的图存成一篇", "合成一篇", "这些截图保存一下", "存一下上面的图",
                "上面这些图片的文字能存成一篇文章吗？", "帮我合成一篇文章", "帮我存成一篇", "这些保存一下", "存一下",
                "把这些图合成一篇", "这些图片整理成一篇文章"]


@pytest.mark.parametrize("text", INSTRUCTIONS)
async def test_save_these_images_makes_no_entry(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    for i in range(3):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
    await app.handle(m(text))
    assert ch.sent[-1][1] == FILES_SAVING == "这些图正在存成一条，好了会告诉你。"
    assert app.pending.opened_at("tg", "me") is None  # it still ends the window
    await app.process_due_jobs()
    assert app.index.count() == 1


async def test_save_these_images_names_the_saved_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("把上面的图存成一篇"))
    assert ch.sent[-1][1] == FILES_SAVED.format(title="标题", id=eid) == f"这些图已经存在「标题」里了\nid：{eid}"
    assert app.jobs.claim_due(10) == [] and app.index.count() == 1


async def test_save_these_images_in_a_new_or_add_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("合成一篇"))  # nothing sent in it yet: not about any images
    assert ch.sent[-1][1] not in (FILES_SAVING,) and not ch.sent[-1][1].startswith("这些图已经")
    await app.handle(m("/s 一条已有的笔记"))
    await app.process_due_jobs()
    x = app.focus.get("tg", "me")
    await app.handle(m(f"/add {x}"))
    await app.handle(m("把上面的图存成一篇"))
    assert not ch.sent[-1][1].startswith("这些图已经")  # /add with no files yet
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [pdf(tmp_path)]))
    await app.handle(m("把上面的图存成一篇"))
    assert ch.sent[-1][1] == FILES_SAVED.format(title="标题", id=x)
    assert app.pending.opened_at("tg", "me") is None


@pytest.mark.parametrize("text", ["/s 把上面的图存一下", "问：上面这些图讲了什么", "这些图里讲了什么",
                                  "总结一下上面这些截图", "把上面的图翻译成英文", "这些截图帮我翻译一下", "解释一下刚才的图",
                                  "上面这些图的要点列一下", "这些图很有用", "这些照片拍得真好", "刚才那张图有错别字",
                                  "把上面图片里的文字整理出来", "这张图整理一下要点", "这张图是哪一条记录里的？",
                                  "上面这些图片的文字存成一篇文章，然后再帮我整理一下要点，顺便总结一下这篇文章说了什么内容"])
async def test_other_texts_about_images_are_not_caught(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    await app.handle(m(text))
    assert not ch.sent[-1][1].startswith("这些图已经") and ch.sent[-1][1] != FILES_SAVING
    assert app.pending.opened_at("tg", "me") is None


async def test_save_these_images_with_no_window_is_not_caught(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("存一下上面的图"))
    assert not ch.sent[-1][1].startswith("这些图已经") and ch.sent[-1][1] != FILES_SAVING


async def test_screenshots_sent_as_files_group_like_photos(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png", kind="file")]))  # e.g. Telegram "send as file"
    assert app.pending.origin("tg", "me") == AUTO
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.handle(m("", [staged(tmp_path, "c.JPG", kind="file")]))
    assert app.pending.origin("tg", "me") == AUTO  # an image file does not end it
    await app.process_due_jobs()
    assert app.index.count() == 1 and len(_with(app, "a.png", "b.png", "c.JPG")) == 1


@pytest.mark.parametrize("gone", ["deleted", "never_saved"])
async def test_images_after_the_windows_entry_is_gone_start_a_new_auto_window(tmp_path, gone):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    if gone == "deleted":
        await app.process_due_jobs()
        await app.pipeline.delete(app.pending.active("tg", "me"))
    else:  # its attachments job failed for good
        app.conn.execute("UPDATE jobs SET status = 'failed' WHERE kind = 'attachments'")
    for n in ("b.png", "c.png", "d.png"):
        await app.handle(m("", [staged(tmp_path, n)]))
    await app.process_due_jobs()
    assert len(_with(app, "b.png", "c.png", "d.png")) == 1 and app.pending.origin("tg", "me") == AUTO


async def test_a_quote_reply_to_a_prompt_is_not_caught(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    link, prompt = app.pending.active("tg", "me"), f"m{ch._n}"
    app.pending.clear("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    await app.handle(quote("这些截图存到这条", prompt))
    assert not ch.sent[-1][1].startswith("这些图已经") and ch.sent[-1][1] != FILES_SAVING
    await app.process_due_jobs()
    assert "这些截图存到这条" in app.library.get(link).body  # material for the quoted entry, as in P7
