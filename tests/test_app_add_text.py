"""P15: after /add <id> the first text is added too; "done" (好了) / /done ends a window."""
import itertools
import time

import pytest
from test_app_media import m, make_app, staged, voice
from test_app_paste import LINK, PASTE, _failed_link, kinds, quote

from octopus.app import (ADD_OPEN, ADD_USAGE, FILES_SAVED, FILES_SAVING, FIX_TO_QUESTION, LINKS_UNREAD, NEW_DONE,
                         NO_WINDOW, NOTE_GREW, WINDOW_DONE)
from octopus.channels.telegram import COMMAND_MENU
from octopus.commands import HELP
from octopus.models import IncomingMessage
from octopus.pending import PendingManual
from fakes import fake_media

URL = "https://www.reddit.com/r/x/comments/1"
TEXT = "视频里老师实际只讲了5个skill：第一个是写作，第二个是剪辑"


@pytest.fixture(autouse=True)
def digit_ids(monkeypatch):
    n = itertools.count(1)
    monkeypatch.setattr("octopus.pipeline.new_entry_id", lambda now: f"{now:%Y%m%d}-{next(n):06d}")


async def _note(tmp_path):
    """An existing note; nothing open."""
    app, ch, media = make_app(tmp_path)
    await app.handle(m("/s 一条已有的笔记"))
    await app.process_due_jobs()
    return app, ch, app.focus.get("tg", "me")


def merges(app):
    return [j for j in app.jobs.claim_due(10) if j.kind == "merge"]


# ---- R1: the first text in an /add window is added ----

async def test_first_text_after_add_is_added_and_the_window_stays(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(TEXT))
    assert ch.sent[-1][1] == "收到，处理中…"
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到") and LINKS_UNREAD not in ch.sent[-1][1]
    assert TEXT in app.library.get(x).body and app.convos.recent("tg", "me") == []
    assert app.pending.active("tg", "me") == x and app.pending.origin("tg", "me") == "add"
    await app.handle(m("", [staged(tmp_path, "a.png")]))  # files still join it
    await app.process_due_jobs()
    assert "a.png" in app.library.get(x).body and app.pending.active("tg", "me") == x
    await app.handle(m("这是什么意思"))  # the second text: chat, and it ends the window
    assert app.convos.recent("tg", "me", 1)[0][1] == "这是什么意思"
    assert app.pending.opened_at("tg", "me") is None and app.index.count() == 1


async def test_first_text_merge_is_a_window_merge_with_the_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x[-4:]}"))
    await app.handle(m("短的"))  # any length
    [job] = merges(app)
    assert job.payload["entry_id"] == x and job.payload["text"] == "短的" and job.payload["via"] == "window"


async def test_a_second_text_is_chat_also_after_a_restart(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(TEXT))
    app.pending = PendingManual(app.conn)  # the service restarted: only the row is left
    await app.handle(m("再补一段"))
    assert [j.payload["text"] for j in merges(app)] == [TEXT]
    assert app.pending.opened_at("tg", "me") is None


async def test_add_with_text_in_one_message_then_the_next_text_is_chat(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x} 一段补充"))
    await app.handle(m("这是什么意思"))
    assert [j.payload["text"] for j in merges(app)] == ["一段补充"]
    assert app.pending.opened_at("tg", "me") is None


async def test_files_first_then_the_first_text_is_still_added(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m(TEXT))
    assert [j.payload["text"] for j in merges(app)] == ["", TEXT]
    assert app.pending.active("tg", "me") == x


async def test_a_voice_transcript_is_a_first_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    app.media = fake_media(tmp_path, transcript="老师只讲了五个")
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [voice(tmp_path)]))
    [job] = merges(app)
    assert job.payload["text"] == "老师只讲了五个" and app.pending.active("tg", "me") == x


async def test_a_long_paste_with_a_link_is_added_as_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(PASTE))
    await app.process_due_jobs()
    assert kinds(app) == ["note", "merge"] and ch.sent[-1][1].endswith(LINKS_UNREAD)
    assert LINK in app.library.get(x).body and app.pending.active("tg", "me") == x


@pytest.mark.parametrize("text", ["/recent", "/s 一条新笔记", "存：一条新笔记", "问：这是什么", "存", "你好",
                                  "好的", "新的"])
async def test_not_added_and_ends_the_window_as_before(tmp_path, text):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(text))
    assert merges(app) == []
    if text != "新的":  # /new opens a fresh window of its own
        assert app.pending.opened_at("tg", "me") is None


async def test_an_at_id_question_is_not_added(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(f"@{x[-6:]} 讲了什么"))
    assert merges(app) == [] and app.pending.opened_at("tg", "me") is None


async def test_a_short_text_with_a_link_is_its_own_entry(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(f"看看这个 {URL}"))
    assert kinds(app) == ["note", "url"] and app.pending.opened_at("tg", "me") is None


async def test_save_the_images_above_still_wins(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m("把上面的图存成一篇"))  # no files yet: not material either
    assert merges(app) == []
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    await app.handle(m("把上面的图存成一篇"))
    assert ch.sent[-1][1] == FILES_SAVED.format(title="标题", id=x)
    assert merges(app) == [] and app.pending.opened_at("tg", "me") is None


async def test_auto_windows_still_end_on_text(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    assert app.pending.origin("tg", "me") == "auto"
    await app.handle(m(TEXT))
    assert app.pending.opened_at("tg", "me") is None
    assert merges(app) == [] and app.convos.recent("tg", "me", 1)[0][1] == TEXT


# ---- R1b: a /new window takes the first text too ----

async def test_new_then_text_image_file_image_is_one_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m(TEXT))
    eid = app.pending.active("tg", "me")
    assert eid and app.pending.origin("tg", "me") == "new"
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "doc.pdf", kind="file")]))
    await app.handle(m("", [staged(tmp_path, "c.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 1 and app.pending.active("tg", "me") == eid
    body = app.library.get(eid).body
    assert TEXT in body and all(n in body for n in ("a.png", "doc.pdf", "c.png"))
    assert app.convos.recent("tg", "me") == []


async def test_new_then_image_text_file_is_one_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    eid = app.pending.active("tg", "me")
    await app.handle(m(TEXT))
    await app.handle(m("", [staged(tmp_path, "doc.pdf", kind="file")]))
    await app.process_due_jobs()
    assert app.index.count() == 1 and app.pending.active("tg", "me") == eid
    body = app.library.get(eid).body
    assert TEXT in body and "a.png" in body and "doc.pdf" in body


async def test_new_then_two_texts_the_second_is_chat(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m(TEXT))
    await app.handle(m("这是什么意思"))
    assert app.pending.opened_at("tg", "me") is None
    assert app.convos.recent("tg", "me", 1)[0][1] == "这是什么意思"
    await app.process_due_jobs()
    assert app.index.count() == 1 and "这是什么意思" not in app.library.get(app.focus.get("tg", "me")).body


@pytest.mark.parametrize("text", ["/recent", "/s 一条新笔记", "问：这是什么", "存", "你好", "好的"])
async def test_new_window_exclusions_end_it_as_before(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m(text))
    assert app.pending.opened_at("tg", "me") is None
    assert [j for j in app.jobs.claim_due(10) if j.kind == "attachments"] == []


async def test_new_window_long_paste_with_a_link_is_added(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    eid = app.pending.active("tg", "me")
    await app.handle(m(PASTE))
    await app.process_due_jobs()
    assert kinds(app) == ["attachments", "merge"] and ch.sent[-1][1].endswith(LINKS_UNREAD)
    assert LINK in app.library.get(eid).body


async def test_new_window_short_link_save_ends_it(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m(f"看看这个 {URL}"))
    assert kinds(app) == ["url"] and app.pending.opened_at("tg", "me") is None


async def test_split_after_a_first_text_takes_it_out(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(TEXT))
    await app.process_due_jobs()
    await app.handle(m("单独存"))
    new_id = app.focus.get("tg", "me")
    assert new_id != x and TEXT in app.library.get(new_id).body and TEXT not in app.library.get(x).body
    assert app.pending.active("tg", "me") == new_id and app.pending.origin("tg", "me") == "new"


# ---- R2: 好了 / /done ends a window ----

@pytest.mark.parametrize("word", ["好了", "好了。", "好啦", "完成", "/done", "好了吧", "好了！", "好了好了", "完成了",
                                  "好啦好啦", "结束了", "好了，好了"])
async def test_done_ends_an_add_window(tmp_path, word):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m(word))
    assert ch.sent[-1][1] == WINDOW_DONE == "好的，结束了。接下来发的文字是和我对话。"
    assert app.pending.opened_at("tg", "me") is None and merges(app) == []
    assert app.convos.recent("tg", "me") == []
    await app.handle(m(TEXT))  # the window is gone: chat
    assert merges(app) == [] and app.convos.recent("tg", "me", 1)[0][1] == TEXT


async def test_done_ends_new_auto_and_link_windows(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("好了"))
    assert ch.sent[-1][1] == WINDOW_DONE and app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    assert app.pending.origin("tg", "me") == "auto"
    await app.handle(m("/done"))
    assert ch.sent[-1][1] == WINDOW_DONE and app.pending.opened_at("tg", "me") is None
    await app.handle(m(URL))
    await app.process_due_jobs()
    assert app.pending.origin("tg", "me") == "link"
    await app.handle(m("完成"))
    assert ch.sent[-1][1] == WINDOW_DONE and app.pending.opened_at("tg", "me") is None


async def test_done_with_no_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/done"))
    assert ch.sent[-1][1] == NO_WINDOW == "现在没有在补充的记录。"
    await app.handle(m("好了"))  # plain chat as before
    assert app.convos.recent("tg", "me", 1)[0][1] == "好了"


async def test_done_by_voice(tmp_path):
    app, ch, x = await _note(tmp_path)
    app.media = fake_media(tmp_path, transcript="好了。")
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [voice(tmp_path)]))
    assert ch.sent[-1][1] == WINDOW_DONE and merges(app) == []


async def test_done_with_more_words_is_not_done(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m("好了，谢谢"))  # the first text: added
    assert [j.payload["text"] for j in merges(app)] == ["好了，谢谢"]


async def test_done_tapped_buttons_still_keep_the_window(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", callback="cmd:recent"))
    assert app.pending.active("tg", "me") == x


# ---- R3: wording, help, menus ----

def test_wording():
    assert ADD_OPEN == ("好的，接下来发的图片、文件，还有第一段文字，都会加到：{title}\nid：{id}\n"
                        "发“好了”、命令，或 5 分钟不发就结束。")
    assert "第一段文字" in ADD_USAGE and "/done" in ADD_USAGE
    assert "好了" in NEW_DONE
    assert "/done" in HELP and "第一段文字" in HELP
    assert "done" in {c for c, _ in COMMAND_MENU}


async def test_add_open_reply(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    assert ch.sent[-1][1] == ADD_OPEN.format(title=app.library.get(x).title, id=x)


# ---- fix round 1 ----

async def test_save_the_images_above_after_a_text_only_new_entry_is_not_about_files(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("", [staged(tmp_path, "a.png")]))  # an earlier auto entry with an image
    await app.process_due_jobs()
    await app.handle(m("/new"))
    await app.handle(m("一段说明文字"))
    await app.handle(m("把上面的图存成一篇"))  # its entry is still being saved, with no files
    assert ch.sent[-1][1] != FILES_SAVING and not ch.sent[-1][1].startswith("这些图已经")
    await app.handle(m("/new"))
    await app.handle(m("另一段说明"))
    await app.process_due_jobs()
    await app.handle(m("把上面的图存成一篇"))  # saved now, still no files
    assert not ch.sent[-1][1].startswith("这些图已经")


async def test_save_the_images_above_after_new_text_then_an_image_still_wins(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("一段说明文字"))
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("把上面的图存成一篇"))  # not saved yet: the image is on its way in
    assert ch.sent[-1][1] == FILES_SAVING
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    await app.handle(m("把上面的图存成一篇"))
    assert ch.sent[-1][1] == FILES_SAVED.format(title="标题", id=app.focus.get("tg", "me"))
    assert eid != app.focus.get("tg", "me")


async def test_save_the_images_above_in_a_split_window_with_only_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m("一段补充"))
    await app.process_due_jobs()
    await app.handle(m("单独存"))
    await app.handle(m("把上面的图存成一篇"))
    assert not ch.sent[-1][1].startswith("这些图已经")


async def test_a_new_entry_started_by_its_text_can_become_a_question(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("这篇讲了什么"))
    await app.process_due_jobs()
    text, buttons = ch.sent_full[-1]
    assert text.startswith("✅ 已保存") and [b.label for b in buttons] == [FIX_TO_QUESTION]
    await app.handle(m("问"))
    assert app.index.count() == 0 and app.convos.recent("tg", "me", 1)[0][1] == "这篇讲了什么"


async def test_a_new_entry_with_files_since_its_text_is_not_turned_into_a_question(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("这篇讲了什么"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    await app.handle(IncomingMessage(channel="tg", user_id="me", callback=data))
    assert ch.sent[-1][1] == NOTE_GREW == "这条后来又加了内容，所以保留了，没有删除。"
    assert app.index.count() == 1 and app.convos.recent("tg", "me") == []  # not answered either


async def test_quote_reply_done_to_a_link_prompt_closes_the_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await _failed_link(app, ch)
    [(pid,)] = app.conn.execute("SELECT prompt_message_id FROM prompts").fetchall()
    await app.handle(quote("好了", pid))
    assert ch.sent[-1][1] == WINDOW_DONE and kinds(app) == ["url"]


async def test_an_image_caption_does_not_use_up_the_windows_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"/add {x}"))
    await app.handle(m("图的说明", [staged(tmp_path, "a.png")]))
    await app.handle(m("又一段文字"))
    assert [j.payload["text"] for j in merges(app)] == ["图的说明", "又一段文字"]


async def test_first_text_and_new_start_restart_the_five_minutes(tmp_path):
    app, ch, x = await _note(tmp_path)
    now = [time.time()]
    app.pending.clock = lambda: now[0]
    await app.handle(m(f"/add {x}"))
    now[0] += 200
    await app.handle(m("一段补充"))
    now[0] += 250  # 450 s after /add, 250 after the text
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    assert [j.payload["entry_id"] for j in merges(app)] == [x, x]
    await app.handle(m("/new"))
    now[0] += 200
    await app.handle(m("一段说明"))
    now[0] += 250
    assert app.pending.active("tg", "me") is not None and app.pending.text_added("tg", "me")


async def test_quote_reply_to_a_prompt_in_an_add_window_goes_to_the_quoted_entry(tmp_path):
    app, ch, x = await _note(tmp_path)
    eid = await _failed_link(app, ch)
    [(pid,)] = app.conn.execute("SELECT prompt_message_id FROM prompts").fetchall()
    await app.handle(m(f"/add {x}"))
    await app.handle(quote("原文一句", pid))
    [job] = merges(app)
    assert job.payload["entry_id"] == eid and job.payload["text"] == "原文一句"


async def test_alias_with_text_takes_the_windows_text(tmp_path):
    app, ch, x = await _note(tmp_path)
    await app.handle(m(f"补充 {x[-4:]} 一段补充"))
    await app.handle(m("这是什么意思"))
    assert [j.payload["text"] for j in merges(app)] == ["一段补充"]
    assert app.pending.opened_at("tg", "me") is None


async def test_new_text_then_an_image_whose_merge_runs_first(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("一段说明"))
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    jobs = app.jobs.claim_due(10)
    [merge] = [j for j in jobs if j.kind == "merge"]
    [first] = [j for j in jobs if j.kind == "attachments"]
    await app._run_job(merge)  # its entry is not there yet: postponed
    assert app.jobs.get(merge.id).status == "queued"
    await app._run_job(first)
    app.conn.execute("UPDATE jobs SET next_run_at = 0 WHERE id = ?", (merge.id,))
    await app.process_due_jobs()
    body = app.library.get(eid).body
    assert app.index.count() == 1 and "a.png" in body and "一段说明" in body


async def test_a_note_typed_with_crlf_can_still_become_a_question(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 第一行\r\n第二行"))
    await app.process_due_jobs()
    await app.handle(m("问"))
    assert app.index.count() == 0


async def test_material_merged_while_the_answer_is_written_is_never_trashed(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("这篇讲了什么"))
    await app.process_due_jobs()
    eid, data = app.focus.get("tg", "me"), ch.sent_full[-1][1][0].data
    await app.handle(m("", [staged(tmp_path, "a.png")]))  # its merge is queued, not run yet
    answer = app._answer

    async def slow_answer(*args, **kwargs):  # the worker merges the screenshot while the answer is written
        done = await answer(*args, **kwargs)
        await app.process_due_jobs()
        return done

    app._answer = slow_answer
    await app.handle(IncomingMessage(channel="tg", user_id="me", callback=data))
    assert app.library.find(eid) and "a.png" in app.library.get(eid).body
    assert app.convos.recent("tg", "me", 1)[0][1] == "这篇讲了什么"  # answered
    assert ch.sent[-1][1] == NOTE_GREW
    assert not app.library.trash.exists() or not any(app.library.trash.iterdir())
