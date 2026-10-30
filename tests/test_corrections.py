import asyncio
import time

import pytest

from fakes import FakeChannel, FakeEmbedder, FakeProvider, summary_json

import octopus.app as app_mod
from octopus.app import App, FIX_TO_QUESTION, NO_CORRECTION, NOTE_CHANGED, SAVE_CHAT, UNREADABLE
from octopus.channels.base import SendResult
from octopus.commands import SAVE_USAGE
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.models import IncomingMessage
from octopus.settings import Settings


def make_app(tmp_path, channel=None, enabled=True):
    cfg = make_test_config(tmp_path)
    cfg.channels["tg"] = ChannelConfig(enabled=enabled, allowed_users=["me"])
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    providers = {"claude:haiku": FakeProvider("claude:haiku", summary_json(title="复利笔记")),
                 "claude:sonnet": FakeProvider("claude:sonnet", "回答 [1]"),
                 "local": FakeProvider("local", "本地")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    ch = channel or FakeChannel("tg")
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir), index=Index(conn, FakeEmbedder()),
              llm=llm, registry=FetcherRegistry([]), channels={ch.name: ch})
    return app, ch


def m(text="", callback=None, user="me"):
    return IncomingMessage(channel="tg", user_id=user, text=text, callback=callback)


async def test_saved_note_offers_fix_to_question(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    text, buttons = ch.sent_full[-1]
    assert text.startswith("✅ 已保存")
    assert [b.label for b in buttons] == [FIX_TO_QUESTION]
    assert buttons[0].fallback == "回复“问”改成提问"
    assert app.index.count() == 1


async def test_button_turns_note_into_question(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    await app.handle(m(callback=data))
    assert app.index.count() == 0  # the note was removed
    assert ch.sent[-1][1].startswith("回答 [1]")  # the note itself is gone, so nothing to cite
    await app.handle(m(callback=data))  # second press: already used
    assert ch.sent[-1][1] == NO_CORRECTION


async def test_answer_offers_save_chat_and_bare_word_works(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("什么是复利？"))
    assert [b.label for b in ch.sent_full[-1][1]] == [SAVE_CHAT]
    await app.handle(m("存"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    assert app.index.count() == 1


async def test_plain_text_is_chat_with_save_chat_button(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("复利需要时间"))
    assert app.jobs.claim_due(10) == []  # nothing queued: not saved
    text, buttons = ch.sent_full[-1]
    assert text.startswith("回答 [1]")
    assert [b.label for b in buttons] == [SAVE_CHAT] == ["存下来"]
    assert buttons[0].fallback == "回复“存”存下来"


async def test_save_chat_button_saves_question_and_answer(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利让财富增长"))  # something to answer from, so the model answers
    await app.process_due_jobs()
    await app.handle(m("复利需要时间吗"))
    assert ch.sent[-1][1].startswith("回答 [1]")
    await app.handle(m(callback=ch.sent_full[-1][1][0].data))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    [body] = [e.body for _, e in app.library.iter_entries() if "AI 回答" in e.body]
    assert body.index("复利需要时间吗") < body.index("AI 回答：\n回答 [1]") < body.index("来源：\n[1] 复利笔记")


async def test_slash_s_alone_shows_usage(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s"))
    assert ch.sent[-1][1] == SAVE_USAGE
    assert app.jobs.claim_due(10) == []


async def test_bare_word_without_target(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("问"))
    assert ch.sent[-1][1] == NO_CORRECTION


async def test_disabled_channel_ignored(tmp_path):
    app, ch = make_app(tmp_path, enabled=False)
    await app.handle(m("复利需要时间"))
    assert ch.sent == []


async def test_owner_is_allowed_without_allowlist(tmp_path):
    class OwnerChannel(FakeChannel):
        def is_owner(self, user_id):
            return user_id == "wx-owner"

    app, ch = make_app(tmp_path, channel=OwnerChannel("tg"))
    app.cfg.channels["tg"].allowed_users = []
    await app.handle(m("/help", user="wx-owner"))
    assert ch.sent and ch.sent[-1][0] == "wx-owner"
    await app.handle(m("/help", user="stranger"))
    assert all(u == "wx-owner" for u, _ in ch.sent)


async def test_failed_send_goes_to_outbox_and_flushes_on_next_message(tmp_path):
    class FlakyChannel(FakeChannel):
        fail = True

        async def send(self, user_id, reply):
            if self.fail:
                return SendResult(False)
            return await super().send(user_id, reply)

    app, ch = make_app(tmp_path, channel=FlakyChannel("tg"))
    await app.handle(m("/help"))
    await app.handle(m("/model"))
    assert app.outbox.count() == 2
    ch.fail = False
    await app.handle(m("/status"))
    texts = [t for _, t in ch.sent]
    # the two missed replies, one by one and oldest first, then the new reply (P4: no more joining)
    assert len(texts) == 3
    assert texts[0].startswith("你好！我是 Octopus") and texts[1].startswith("当前模型") and texts[2].startswith("已保存：")
    assert app.outbox.count() == 0
    assert not hasattr(app_mod, "OUTBOX_JOINER")


async def test_outbox_flush_stops_at_first_failure_and_keeps_the_rest_queued(tmp_path):
    class OneThenDown(FakeChannel):
        budget = 1

        async def send(self, user_id, reply):
            if self.budget <= 0:
                return SendResult(False)
            self.budget -= 1
            return await super().send(user_id, reply)

    app, ch = make_app(tmp_path, channel=OneThenDown("tg"))
    base = time.time() - 3600
    for i, text in enumerate(["第一条", "第二条", "第三条"]):
        app.outbox.add("tg", "me", text, created_at=base + i)
    await app.handle(m("/help"))
    assert [t for _, t in ch.sent] == ["第一条"]  # delivered alone, never joined with the others
    rows = [tuple(r) for r in app.conn.execute("SELECT text, created_at FROM outbox ORDER BY id")]
    assert rows[:2] == [("第二条", base + 1), ("第三条", base + 2)]  # untouched: order and age kept
    assert rows[2][0].startswith("你好！我是 Octopus") and len(rows) == 3


async def test_messages_from_one_user_are_handled_in_order(tmp_path):
    app, ch = make_app(tmp_path)
    order = []
    gate = asyncio.Event()

    async def slow_dispatch(msg):
        order.append(("start", msg.text))
        if msg.text == "第一条":
            await gate.wait()
        order.append(("end", msg.text))

    app._dispatch = slow_dispatch
    t1 = asyncio.create_task(app.handle(m("第一条")))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(app.handle(m("第二条")))
    await asyncio.sleep(0.01)
    assert order == [("start", "第一条")]
    gate.set()
    await asyncio.gather(t1, t2)
    assert order == [("start", "第一条"), ("end", "第一条"), ("start", "第二条"), ("end", "第二条")]


async def test_notify_owner_uses_images_when_supported(tmp_path):
    class ImageChannel(FakeChannel):
        images = []

        async def send_image(self, user_id, path, caption=""):
            self.images.append((user_id, path, caption))
            return SendResult(True, "img1")

    app, ch = make_app(tmp_path, channel=ImageChannel("tg"))
    png = tmp_path / "qr.png"
    png.write_bytes(b"png")
    assert await app.notify_owner("请扫码", png) == 1
    assert ch.images == [("me", png, "请扫码")]


async def test_failed_link_reply_has_no_button_and_records_prompt_message_id(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    text, buttons = ch.sent_full[-1]
    assert text.startswith("⚠️ 没能获取内容")
    assert buttons == []  # only plain notes offer 改成提问
    prompt_id = f"m{len(ch.sent)}"  # FakeChannel numbers its sends m1, m2, …
    assert app.pending.for_reply("tg", "me", prompt_id) == app.pending.active("tg", "me") is not None


@pytest.mark.parametrize("press", ["button", "word"])
async def test_save_chat_never_merges_into_pending_entry(tmp_path, press):
    app, ch = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    pending = app.pending.active("tg", "me")
    path = app.library.find(pending)
    before = path.read_text()
    await app.handle(m("什么是复利？"))  # inside the merge window
    data = ch.sent_full[-1][1][0].data
    await app.handle(m(callback=data) if press == "button" else m("存"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    assert app.index.count() == 2  # a new entry
    assert path.read_text() == before  # the link entry still waits for its screenshots, untouched
    assert app.pending.active("tg", "me") == pending


async def test_notify_owner_skips_the_triggering_channel(tmp_path):
    app, tg = make_app(tmp_path)
    wx = FakeChannel("clawbot")
    app.channels["clawbot"] = wx
    app.cfg.channels["clawbot"] = ChannelConfig(enabled=True, allowed_users=["wx-me"])
    assert await app.notify_owner("ClawBot 需要重新登录", exclude_channel="clawbot") == 1
    assert tg.sent == [("me", "ClawBot 需要重新登录")]
    assert wx.sent == []


async def test_bare_wen_flips_last_note(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    assert app.index.count() == 1
    await app.handle(m("问"))
    assert app.index.count() == 0
    assert ch.sent[-1][1].startswith("回答 [1]")
    assert [b.label for b in ch.sent_full[-1][1]] == [SAVE_CHAT]  # and it can be flipped back


async def test_other_users_callback_is_refused(tmp_path):
    app, ch = make_app(tmp_path)
    app.cfg.channels["tg"].allowed_users = ["me", "friend"]
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    await app.handle(m(callback=data, user="friend"))
    assert ch.sent[-1] == ("friend", NO_CORRECTION)
    assert app.index.count() == 1


async def test_fix_to_question_when_note_already_deleted(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    assert await app.pipeline.delete(app.actions.get(int(data[4:]), "tg", "me")["entry_id"])  # e.g. /delete
    await app.handle(m(callback=data))
    assert ch.sent[-1][1].startswith("回答 [1]")
    assert app.actions.get(int(data[4:]), "tg", "me") is None


async def test_fix_to_question_keeps_note_when_models_are_down(tmp_path):
    app, ch = make_app(tmp_path)
    await app.handle(m("/s 复利让财富增长"))  # something else to answer from, so the models are asked
    await app.handle(m("/s 复利需要时间"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    for p in app.llm.providers.values():
        p.error = "down"
    await app.handle(m(callback=data))
    assert ch.sent[-1][1].startswith("现在没法回答") and ch.sent[-1][1].endswith("笔记还在")
    assert app.index.count() == 2
    assert app.actions.get(int(data[4:]), "tg", "me") is not None  # can be pressed again later


async def test_failed_outbox_flush_keeps_original_age(tmp_path):
    class DownChannel(FakeChannel):
        async def send(self, user_id, reply):
            return SendResult(False)

    app, ch = make_app(tmp_path, channel=DownChannel("tg"))
    queued_at = time.time() - 3600
    app.outbox.add("tg", "me", "旧回复", created_at=queued_at)
    await app.handle(m("/help"))
    rows = [tuple(r) for r in app.conn.execute("SELECT text, created_at FROM outbox ORDER BY id")]
    assert rows[0] == ("旧回复", queued_at)  # re-queued with its original age: the 7-day TTL doesn't restart
    assert rows[1][0].startswith("你好！我是 Octopus") and len(rows) == 2


# ---- final review fixes ----

async def saved_note(app, ch, text="复利需要时间"):
    await app.handle(m(f"/s {text}"))
    await app.process_due_jobs()
    data = ch.sent_full[-1][1][0].data
    return app.actions.get(int(data[4:]), "tg", "me")["entry_id"], data


@pytest.mark.parametrize("press", ["button", "word"])
async def test_fix_to_question_refused_after_a_note_was_added(tmp_path, press):
    app, ch = make_app(tmp_path)
    eid, data = await saved_note(app, ch)
    await app.pipeline.annotate(eid, "下周复习")
    await app.handle(m(callback=data) if press == "button" else m("问"))
    assert ch.sent[-1][1] == NOTE_CHANGED
    assert app.library.get(eid) is not None and app.index.count() == 1


async def test_fix_to_question_refused_after_a_retitle(tmp_path):
    app, ch = make_app(tmp_path)
    eid, data = await saved_note(app, ch)
    await app.pipeline.retitle(eid, "时间的朋友")
    await app.handle(m(callback=data))
    assert ch.sent[-1][1] == NOTE_CHANGED
    assert app.library.get(eid) is not None


async def test_fix_to_question_refused_when_the_note_is_unreadable(tmp_path):
    app, ch = make_app(tmp_path)
    eid, data = await saved_note(app, ch)
    app.library.find(eid).write_text("被改坏了", encoding="utf-8")
    await app.handle(m(callback=data))
    assert ch.sent[-1][1] == f"{UNREADABLE}{eid}"
    assert app.library.find(eid) is not None


async def test_action_remembers_the_saved_title(tmp_path):
    app, ch = make_app(tmp_path)
    eid, data = await saved_note(app, ch)
    assert app.actions.get(int(data[4:]), "tg", "me")["title"] == "复利笔记"


@pytest.mark.parametrize("word", ["存。", "存！", "存～", "存吧"])
async def test_bare_save_with_punctuation(tmp_path, word):
    app, ch = make_app(tmp_path)
    await app.handle(m("什么是复利？"))
    await app.handle(m(word))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "note" and "AI 回答" in job.payload["text"]


async def test_clawbot_voice_save_word(tmp_path):
    from octopus.models import Attachment

    app, ch = make_app(tmp_path)
    await app.handle(m("什么是复利？"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="", attachments=[
        Attachment(kind="audio", filename="voice.silk", voice=True, text="存。")]))  # WeChat's own transcript
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ 已保存")
    assert app.index.count() == 1
