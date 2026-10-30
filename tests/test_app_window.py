from test_app_media import make_app, m, staged

import pytest

from octopus.app import NEW_DONE
from octopus.models import IncomingMessage

URL = "https://www.reddit.com/r/x/comments/1"


def quote(text, reply_to, attachments=None):
    return IncomingMessage(channel="tg", user_id="me", text=text, attachments=attachments or [], reply_to_id=reply_to)


async def _merged(tmp_path):
    """A needs_manual link entry with one screenshot merged in; returns the merge reply's message id."""
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    return app, ch, eid, f"m{ch._n}"


async def test_quoted_question_on_a_merge_reply_is_answered(tmp_path):
    app, ch, eid, merge_reply = await _merged(tmp_path)
    assert app.pending.active("tg", "me") == eid  # the window is still restarted
    await app.handle(quote("这篇讲了什么？", merge_reply))
    assert app.jobs.claim_due(10) == []
    assert app.convos.recent("tg", "me", 1)[0][1] == "这篇讲了什么？"  # a chat turn: it was answered


async def test_quoted_text_on_a_split_reply_is_not_merged(tmp_path):
    app, ch, eid, _ = await _merged(tmp_path)
    await app.handle(m("单独存"))
    assert ch.sent[-1][1].startswith("✅ 已另存为新的一条")
    await app.handle(quote("删掉这条", f"m{ch._n}"))
    assert [j for j in app.jobs.claim_due(10) if j.kind == "merge"] == []


async def test_late_link_job_leaves_a_window_opened_after_it(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条已有的笔记"))
    await app.process_due_jobs()
    x = app.focus.get("tg", "me")
    await app.handle(m(URL))  # its job finishes needs_manual only after the /add below
    await app.handle(m(f"/add {x}"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("⚠️ 没能获取内容")
    assert app.pending.active("tg", "me") == x
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    await app.process_due_jobs()
    assert "s0.png" in app.library.get(x).body


async def test_new_then_a_burst_of_images_is_one_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.cfg.max_concurrent_jobs = 2
    await app.handle(m("/new"))
    assert ch.sent[-1][1] == NEW_DONE
    for i in range(4):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 1
    [row] = app.index.recent(10)
    body = app.library.get(row["id"]).body
    assert all(f"s{i}.png" in body for i in range(4))
    assert sorted(p.name for p in (tmp_path / "attachments" / row["id"]).iterdir()) == [f"s{i}.png" for i in range(4)]
    assert app.pending.active("tg", "me") == row["id"]
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "t0.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 2


async def test_merge_waits_while_its_entry_is_still_being_saved(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    app.conn.execute("UPDATE jobs SET next_run_at = next_run_at + 100 WHERE kind = 'attachments'")  # e.g. a retry
    await app.process_due_jobs()
    [merge] = app.conn.execute("SELECT status, attempts FROM jobs WHERE kind = 'merge'").fetchall()
    assert tuple(merge) == ("queued", 0) and app.index.count() == 0  # waiting, not saved apart
    app.conn.execute("UPDATE jobs SET next_run_at = 0")
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    body = app.library.get(row["id"]).body
    assert "a.png" in body and "b.png" in body


async def test_merge_falls_back_when_its_entry_will_never_be_saved(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    app.conn.execute("UPDATE jobs SET status = 'failed' WHERE kind = 'attachments'")
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    assert "b.png" in app.library.get(row["id"]).body


async def test_new_with_nothing_sent_is_ended_by_a_note_or_a_link(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    assert not app.pending.fresh("tg", "me") and app.index.count() == 1  # the note is saved, the window ends
    await app.handle(m("/new"))
    await app.handle(m(URL))
    assert not app.pending.fresh("tg", "me")  # a link ends it


async def test_quote_reply_merge_is_not_a_window_merge(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    prompt = f"m{ch._n}"
    await app.handle(quote("", prompt, [staged(tmp_path, "q.png")]))
    await app.handle(m("", [staged(tmp_path, "w.png")]))
    quoted, window = sorted(app.jobs.claim_due(10), key=lambda j: j.id)
    assert "via" not in quoted.payload and window.payload["via"] == "window"


# ---- task 7: a /new or /add window collects only files sent one after another; /s is always a new entry ----

def _with(app, *names):
    """The bodies of the entries that contain all of names."""
    bodies = [app.library.get(r["id"]).body for r in app.index.recent(10)]
    return [b for b in bodies if all(n in b for n in names)]


async def test_chat_ends_a_new_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.handle(m("一段说明"))  # the window's one text (P15)
    await app.handle(m("这篇讲了什么？"))
    assert app.convos.recent("tg", "me", 1)[0][1] == "这篇讲了什么？"  # answered as chat
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "c.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 2
    [first] = _with(app, "a.png")
    assert "b.png" in first and "c.png" not in first


async def test_save_note_in_a_new_window_is_its_own_entry_and_ends_it(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.handle(m("/s 一条笔记"))
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert app.index.count() == 3
    assert [len(_with(app, n)) for n in ("a.png", "b.png", "一条笔记")] == [1, 1, 1]
    assert _with(app, "a.png", "b.png") == _with(app, "a.png", "一条笔记") == _with(app, "b.png", "一条笔记") == []


async def test_chat_ends_an_add_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 一条已有的笔记"))
    await app.process_due_jobs()
    x = app.focus.get("tg", "me")
    await app.handle(m(f"/add {x}"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    await app.handle(m("一段说明"))  # the window's one text (P15)
    await app.handle(m("这是什么意思"))
    assert app.pending.opened_at("tg", "me") is None
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    assert "a.png" in app.library.get(x).body and "b.png" not in app.library.get(x).body
    assert app.index.count() == 2


@pytest.mark.parametrize("text", ["你好", "/recent", "存：一条笔记", "存", "好的"])
async def test_any_other_message_ends_a_new_window(tmp_path, text):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    assert app.pending.origin("tg", "me") == "new"
    await app.handle(m(text))
    assert app.pending.opened_at("tg", "me") is None


async def test_failed_link_window_keeps_its_rules_but_save_note_is_new(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    assert app.pending.origin("tg", "me") == "link"
    await app.handle(m("这个是什么"))
    assert app.convos.recent("tg", "me", 1)[0][1] == "这个是什么"  # answered
    assert app.pending.active("tg", "me") == eid
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    await app.process_due_jobs()
    await app.handle(m("原文第一段\n原文第二段"))
    await app.process_due_jobs()
    body = app.library.get(eid).body
    assert "s0.png" in body and "原文第二段" in body
    await app.handle(m("/s 一条笔记"))
    await app.process_due_jobs()
    note = app.focus.get("tg", "me")
    assert note != eid and "一条笔记" in app.library.get(note).body and "一条笔记" not in app.library.get(eid).body
    assert app.pending.active("tg", "me") == eid and app.pending.origin("tg", "me") == "link"
    await app.handle(m("你好"))
    await app.handle(m("/recent"))
    await app.handle(m("", [staged(tmp_path, "s1.png")]))
    await app.process_due_jobs()
    assert "s1.png" in app.library.get(eid).body and app.index.count() == 2


async def test_answering_a_confirmation_keeps_a_new_window(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/s 要删的笔记"))
    await app.process_due_jobs()
    doomed = app.focus.get("tg", "me")
    await app.handle(m("/new"))
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(IncomingMessage(channel="tg", user_id="me", callback=f"del:{doomed}"))  # a tap: an answer
    assert ch.sent[-1][1].startswith("确定删除")
    await app.handle(m("确认"))
    assert ch.sent[-1][1].startswith("已删除")
    assert app.pending.active("tg", "me") == eid and app.pending.origin("tg", "me") == "new"
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid


async def test_old_window_rows_without_origin_are_link_windows(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    app.conn.execute("UPDATE pending_manual SET origin = NULL")  # written before the column existed
    assert app.pending.origin("tg", "me") == "link"
    await app.handle(m("这个是什么"))
    assert app.pending.active("tg", "me") == eid  # short chat leaves it open
    await app.handle(m("原文第一段\n原文第二段"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == eid


async def test_split_moves_the_window_as_a_new_window(tmp_path):
    app, ch, eid, _ = await _merged(tmp_path)
    await app.handle(m("单独存"))
    new = app.pending.active("tg", "me")
    assert new != eid and app.pending.origin("tg", "me") == "new"  # the user's own files, not the failed link
    await app.handle(m("这个是什么"))
    assert app.pending.opened_at("tg", "me") is None
