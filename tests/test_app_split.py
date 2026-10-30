from test_app_media import make_app, m, staged

import asyncio
import time

from octopus.app import NO_CORRECTION, SPLIT_LABEL, SPLIT_NONE
from octopus.models import IncomingMessage

URL = "https://www.reddit.com/r/x/comments/1"


def tap(data):
    return IncomingMessage(channel="tg", user_id="me", text="", callback=data)


async def _merges(tmp_path, gaps=(0, 0, 0)):
    """A needs_manual link entry, then one screenshot merged into it per gap (seconds after the previous)."""
    app, ch, _ = make_app(tmp_path)
    now = [time.time()]
    app.actions.clock = app.pending.clock = lambda: now[0]
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    buttons = []
    for i, gap in enumerate(gaps):
        now[0] += gap
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
        await app.process_due_jobs()
        text, bs = ch.sent_full[-1]
        assert text.startswith("✅ 已补充到")
        assert [(b.label, b.fallback) for b in bs] == [(SPLIT_LABEL, "回复“单独存”另存成新的一条")]
        buttons.append(bs[0].data)
    return app, ch, eid, buttons, now


def _new_id(app, eid):
    new_id = app.focus.get("tg", "me")
    assert new_id and new_id != eid
    return new_id


async def test_button_splits_that_merge_and_the_later_ones_of_its_batch(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path)
    assert buttons[1].startswith("split:")
    await app.handle(tap(buttons[1]))
    new_id = _new_id(app, eid)
    new, old = app.library.get(new_id), app.library.get(eid)
    assert ch.sent[-1][1] == (f"✅ 已另存为新的一条：{new.title}\n{new.summary}\nid：{new_id}\n"
                              f"原来那条「{old.title}」已恢复。")
    assert "s1.png" in new.body and "s2.png" in new.body and "s0.png" not in new.body
    assert "s0.png" in old.body and "s1.png" not in old.body and old.status == "ok"
    assert app.pending.active("tg", "me") == new_id
    await app.handle(tap(buttons[2]))  # consumed with the batch
    assert ch.sent[-1][1] == SPLIT_NONE


async def test_typed_split_takes_the_latest_batch_and_the_window_moves(tmp_path):
    app, ch, eid, _, _ = await _merges(tmp_path)
    await app.handle(m("单独存"))
    new_id = _new_id(app, eid)
    old = app.library.get(eid)
    assert (old.status, old.body, old.title) == ("needs_manual", "", URL)
    assert ch.sent[-1][1].endswith(f"id：{new_id}\n原来那条「{URL}」已恢复。")
    await app.handle(m("", [staged(tmp_path, "s3.png")]))  # the rest of that article joins the new entry
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已补充到")
    new = app.library.get(new_id)
    assert all(f"s{i}.png" in new.body for i in range(4)) and "### 图片 3（s2.png）" in new.body
    assert app.index.count() == 2


async def test_merges_more_than_two_minutes_apart_are_separate_batches(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0, 121, 120))
    await app.handle(m("另存"))
    new = app.library.get(_new_id(app, eid))
    assert "s1.png" in new.body and "s2.png" in new.body
    assert "s0.png" in app.library.get(eid).body
    await app.handle(tap(buttons[0]))  # its batch is itself alone
    second = app.library.get(app.focus.get("tg", "me"))
    assert second.id not in (eid, new.id) and "s0.png" in second.body and "s1.png" not in second.body
    assert app.library.get(eid).status == "needs_manual"


async def test_typed_split_after_the_window_or_with_nothing_merged(tmp_path):
    app, ch, eid, _, now = await _merges(tmp_path, gaps=(0,))
    now[0] += 1801
    await app.handle(m("单独存"))
    assert ch.sent[-1][1] == SPLIT_NONE
    assert "s0.png" in app.library.get(eid).body


async def test_typed_split_with_nothing_merged(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("单独存"))
    assert ch.sent[-1][1] == SPLIT_NONE


async def test_split_when_the_section_was_edited_away(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0,))
    entry = app.library.get(eid)
    entry.body = "我手改过的内容"
    app.library.save(entry)
    await app.handle(tap(buttons[0]))
    assert ch.sent[-1][1] == SPLIT_NONE
    await app.handle(tap(buttons[0]))
    assert ch.sent[-1][1] == SPLIT_NONE  # consumed: not tried again


async def test_merge_into_deleted_target_gets_no_split_button(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    assert await app.pipeline.delete(eid)  # before the merge job runs: it becomes a new entry
    await app.process_due_jobs()
    text, buttons = ch.sent_full[-1]
    assert text.startswith("✅ 已保存") and buttons == []


async def test_merge_action_does_not_hide_the_answer_from_a_bare_save(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=())
    await app.handle(m("什么是复利"))
    assert ch.sent_full[-1][1]  # the answer, with 存下来
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    await app.process_due_jobs()
    await app.handle(m("存"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    assert app.index.count() == 2


async def test_fix_callback_never_runs_a_merge_action(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0,))
    await app.handle(tap("fix:" + buttons[0].removeprefix("split:")))
    assert ch.sent[-1][1] == NO_CORRECTION
    assert app.jobs.claim_due(10) == []


async def test_queued_and_running_merges_of_the_burst_follow_the_split(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    for i in range(3):  # a burst: all three are queued before the first is done
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
    [first] = app.jobs.claim_due(1)
    await app._run_job(first)
    button = ch.sent_full[-1][1][0].data
    [running] = app.jobs.claim_due(1)  # claimed by the worker, not merged yet
    await app.handle(tap(button))
    new_id = _new_id(app, eid)
    await app._run_job(running)
    await app.process_due_jobs()
    new, old = app.library.get(new_id), app.library.get(eid)
    assert all(f"s{i}.png" in new.body for i in range(3)) and old.status == "needs_manual" and old.body == ""
    assert all((tmp_path / "attachments" / new_id / f"s{i}.png").exists() for i in range(3))
    assert app.pending.active("tg", "me") == new_id


async def test_old_button_does_not_take_over_another_window(tmp_path):
    app, ch, eid, buttons, now = await _merges(tmp_path, gaps=(0,))
    now[0] += 3600
    app.pending.set("tg", "me", "20261013-0ther0")  # the user is busy with another article now
    await app.handle(tap(buttons[0]))
    new_id = _new_id(app, eid)
    assert "s0.png" in app.library.get(new_id).body
    assert app.pending.active("tg", "me") == "20261013-0ther0"


async def test_old_button_with_no_window_open_opens_one_on_the_new_entry(tmp_path):
    app, ch, eid, buttons, now = await _merges(tmp_path, gaps=(0,))
    now[0] += 3600
    assert app.pending.active("tg", "me") is None
    await app.handle(tap(buttons[0]))
    assert app.pending.active("tg", "me") == _new_id(app, eid)


async def test_reply_leaves_out_an_old_entry_deleted_meanwhile(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0, 0))
    provider = app.llm.providers["claude:haiku"]
    real = provider.respond

    def respond(prompt):
        if "s0.png" in prompt and "s1.png" not in prompt:  # the old entry's summary: deleted by hand right then
            app.library.find(eid).unlink()
        return real

    provider.respond = respond
    await app.handle(tap(buttons[1]))
    new_id = _new_id(app, eid)
    assert ch.sent[-1][1].endswith(f"id：{new_id}") and "原来那条" not in ch.sent[-1][1]


async def test_merge_running_when_split_is_tapped_goes_along(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(URL))
    await app.process_due_jobs()
    eid = app.pending.active("tg", "me")
    await app.handle(m("", [staged(tmp_path, "s0.png")]))
    await app.process_due_jobs()
    button = ch.sent_full[-1][1][0].data
    await app.handle(m("", [staged(tmp_path, "s1.png")]))
    gate, inside = asyncio.Event(), asyncio.Event()
    real = app.pipeline._summarize_into

    async def slow(entry, title_given):  # s1's merge holds the entry lock in its model call
        if entry.id == eid and "s1.png" in entry.body and not gate.is_set():
            inside.set()
            await gate.wait()
        return await real(entry, title_given)

    app.pipeline._summarize_into = slow
    [job] = app.jobs.claim_due(1)
    merging = asyncio.create_task(app._run_job(job))
    await inside.wait()
    splitting = asyncio.create_task(app.handle(tap(button)))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(merging, splitting)
    new_id = _new_id(app, eid)
    new, old = app.library.get(new_id), app.library.get(eid)
    assert "s0.png" in new.body and "s1.png" in new.body
    assert (old.status, old.body) == ("needs_manual", "")
    assert app.pending.active("tg", "me") == new_id


async def test_explicit_add_is_not_redirected_by_a_split(tmp_path):
    app, ch, eid, buttons, now = await _merges(tmp_path, gaps=(0,))
    now[0] += 86400
    await app.handle(m(f"/add {eid} 这是给原来那篇的补充"))  # an explicit target, still queued
    await app.handle(tap(buttons[0]))  # yesterday's button
    new_id = _new_id(app, eid)
    await app.process_due_jobs()
    assert "这是给原来那篇的补充" in app.library.get(eid).body
    assert "这是给原来那篇的补充" not in app.library.get(new_id).body


async def test_window_merges_are_not_redirected_when_the_window_stays(tmp_path):
    app, ch, eid, buttons, now = await _merges(tmp_path, gaps=(0,))
    await app.handle(m("", [staged(tmp_path, "s1.png")]))  # queued through the window on eid
    now[0] += 3600
    app.pending.set("tg", "me", "20261013-0ther0")  # long after: busy with another article
    await app.handle(tap(buttons[0]))
    new_id = _new_id(app, eid)
    await app.process_due_jobs()
    assert "s1.png" in app.library.get(eid).body and "s1.png" not in app.library.get(new_id).body


async def test_explicit_add_running_during_the_split_stays_in_the_old_entry(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0,))
    await app.handle(m(f"/add {eid} 明确给原来那篇"))
    gate, inside = asyncio.Event(), asyncio.Event()
    real = app.pipeline._summarize_into

    async def slow(entry, title_given):  # the /add merge holds the entry lock in its model call
        if entry.id == eid and "明确给原来那篇" in entry.body and not gate.is_set():
            inside.set()
            await gate.wait()
        return await real(entry, title_given)

    app.pipeline._summarize_into = slow
    [job] = app.jobs.claim_due(1)
    merging = asyncio.create_task(app._run_job(job))
    await inside.wait()
    splitting = asyncio.create_task(app.handle(tap(buttons[0])))
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(merging, splitting)
    new_id = next(line.removeprefix("id：") for text in ch.sent if text[1].startswith("✅ 已另存")
                  for line in text[1].splitlines() if line.startswith("id："))
    old, new = app.library.get(eid), app.library.get(new_id)
    assert "明确给原来那篇" in old.body and "s0.png" not in old.body
    assert "s0.png" in new.body and "明确给原来那篇" not in new.body


async def test_explicit_add_merge_is_a_batch_of_its_own(tmp_path):
    app, ch, eid, _, _ = await _merges(tmp_path, gaps=(0,))
    await app.handle(m(f"/add {eid} 明确给原来那篇"))
    await app.process_due_jobs()
    add_button = ch.sent_full[-1][1][0].data
    await app.handle(m("", [staged(tmp_path, "s1.png")]))  # the window is still open: a window merge
    await app.process_due_jobs()
    await app.handle(tap(add_button))
    new = app.library.get(_new_id(app, eid))
    assert "明确给原来那篇" in new.body and "s1.png" not in new.body
    old = app.library.get(eid)
    assert "s0.png" in old.body and "s1.png" in old.body


async def test_button_of_a_deleted_entry_is_used_up(tmp_path):
    app, ch, eid, buttons, _ = await _merges(tmp_path, gaps=(0,))
    assert await app.pipeline.delete(eid)
    await app.handle(tap(buttons[0]))
    assert ch.sent[-1][1] == SPLIT_NONE
    assert app.actions.get(int(buttons[0].removeprefix("split:")), "tg", "me") is None
