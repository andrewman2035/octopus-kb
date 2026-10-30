import json
import re
import time
from datetime import date, datetime, timedelta, timezone

from fakes import FakeChannel, FakeEmbedder, FakeProvider, summary_json

from octopus.app import (ANSWER_PLEASE, App, CANCEL_WORDS, CONFIRM_WORDS, FIND_FALLBACK, FIND_NONE, NO_CORRECTION,
                         NOT_FOUND, OP_CANCELLED, OP_EXPIRED, OP_REASK, SAVE_CHAT, UNREADABLE)
from octopus.commands import AMBIGUOUS_ID
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.index import Index
from octopus.library import Entry, Library
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.models import IncomingMessage
from octopus.ops import OP_TTL_SECONDS
from octopus.app import MISSING_ID
from octopus.qa import PINNED_MARK
from octopus.settings import Settings

CST = timezone(timedelta(hours=8))


def make_app(tmp_path):
    """haiku answers intent prompts with state["intent"] and summary prompts with a title from the text."""
    cfg = make_test_config(tmp_path)
    cfg.channels["tg"] = ChannelConfig(enabled=True, allowed_users=["me", "other"])
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    state = {"intent": {"intent": "answer"}, "intent_calls": 0}

    def haiku(prompt):
        if state.get("summary_down") and "用户说：" not in prompt:
            raise LLMError("haiku down")
        if "用户说：" in prompt:
            state["intent_calls"] += 1
            if state["intent"] is None:
                raise LLMError("haiku down")
            return json.dumps(state["intent"], ensure_ascii=False)
        return summary_json(title="苹果笔记" if "苹果" in prompt else "复利笔记")

    providers = {"claude:haiku": FakeProvider("claude:haiku", haiku),
                 "claude:sonnet": FakeProvider("claude:sonnet", "回答 [1]"),
                 "local": FakeProvider("local", "本地")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    ch = FakeChannel("tg")
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir), index=Index(conn, FakeEmbedder()),
              llm=llm, registry=FetcherRegistry([]), channels={ch.name: ch})
    app.today = lambda: date(2026, 10, 13)
    return app, ch, state


def m(text="", callback=None, user="me"):
    return IncomingMessage(channel="tg", user_id=user, text=text, callback=callback)


async def save(app, text):
    await app.handle(m(f"/s {text}"))
    await app.process_due_jobs()
    return app.focus.get("tg", "me")


def last(ch):
    return ch.sent_full[-1]


def test_word_sets_leave_the_bare_corrections_alone():
    assert not ({"存", "问"} & (CONFIRM_WORDS | CANCEL_WORDS))
    assert "确认" in CONFIRM_WORDS and "取消" in CANCEL_WORDS


async def test_delete_focus_with_button(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "ids": [], "target": "focus"}
    await app.handle(m("删掉上面这条"))
    text, buttons = last(ch)
    assert text == f"确定删除下面 1 条吗？\n「复利笔记」 {eid}"
    assert [(b.label, b.fallback) for b in buttons] == [("确认", "回复“确认”执行"), ("取消", "回复“取消”放弃")]
    assert buttons[0].data.startswith("op:") and buttons[0].data.endswith(":yes")
    assert buttons[1].data == buttons[0].data[:-3] + "no"
    assert app.library.get(eid) is not None  # nothing happens before 确认
    await app.handle(m(callback=buttons[0].data))
    assert app.library.get(eid) is None and app.index.count() == 0
    assert ch.sent[-1][1] == "已删除：「复利笔记」（文件已移到回收目录）"
    assert app.focus.get("tg", "me") is None
    await app.handle(m(callback=buttons[0].data))  # pressed again
    assert ch.sent[-1][1] == OP_EXPIRED


async def test_delete_with_typed_confirm_then_confirm_is_chat_again(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    await app.handle(m("确认"))
    assert app.library.get(eid) is None
    assert ch.sent[-1][1].startswith("已删除")
    await app.handle(m("确认"))  # no op left: an ordinary chat turn
    assert ch.sent[-1][1].startswith("回答") and [b.label for b in last(ch)[1]] == [SAVE_CHAT]


async def test_cancel(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    await app.handle(m("取消"))
    assert ch.sent[-1][1] == OP_CANCELLED
    assert app.library.get(eid) is not None
    assert app.ops.latest("tg", "me") is None


async def test_confirm_after_ttl_expired(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    data = last(ch)[1][0].data
    later = time.time() + OP_TTL_SECONDS + 1
    app.ops.clock = lambda: later
    await app.handle(m(callback=data))
    assert ch.sent[-1][1] == OP_EXPIRED
    assert app.library.get(eid) is not None


async def test_delete_two_explicit_ids(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m(f"删除 {a} 和 {b}"))
    text, buttons = last(ch)
    assert text == f"确定删除下面 2 条吗？\n「复利笔记」 {a}\n「苹果笔记」 {b}"
    await app.handle(m(callback=buttons[0].data))
    assert app.library.get(a) is None and app.library.get(b) is None
    assert ch.sent[-1][1] == "已删除：「复利笔记」「苹果笔记」（文件已移到回收目录）"


async def test_delete_reports_entries_gone_meanwhile(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    state["intent"] = {"intent": "delete"}
    await app.handle(m(f"删除 {a} 和 {b}"))
    data = last(ch)[1][0].data
    await app.pipeline.delete(b)
    await app.handle(m(callback=data))
    assert ch.sent[-1][1] == f"已删除：「复利笔记」（文件已移到回收目录）\n找不到：{b}"


async def test_retitle_focus(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "retitle", "target": "focus", "title": "时间的朋友"}
    await app.handle(m("把上面这条标题改成 时间的朋友"))
    assert last(ch)[0] == "确定把「复利笔记」的标题改成「时间的朋友」吗？"
    assert app.library.get(eid).title == "复利笔记"
    await app.handle(m("确认"))
    assert ch.sent[-1][1] == f"✅ 已改标题：时间的朋友\nid：{eid}"
    assert app.library.get(eid).title == "时间的朋友"
    assert "时间的朋友" in app.library.find(eid).name
    assert app.focus.get("tg", "me") == eid


async def test_retitle_without_title_asks(tmp_path):
    app, ch, state = make_app(tmp_path)
    await save(app, "复利需要时间")
    state["intent"] = {"intent": "retitle", "target": "focus"}
    await app.handle(m("改一下这条的标题"))
    assert last(ch) == ("要改成什么标题？", [])
    assert app.ops.latest("tg", "me") is None


async def test_annotate_explicit_id(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    await save(app, "苹果很好吃")  # the focus is now another entry
    state["intent"] = {"intent": "annotate", "note": "下周复习"}
    await app.handle(m(f"给 {eid} 加个备注：下周复习"))
    assert last(ch)[0] == "确定给「复利笔记」加备注：下周复习"
    await app.handle(m(callback=last(ch)[1][0].data))
    assert ch.sent[-1][1] == f"✅ 已加备注：复利笔记\nid：{eid}"
    assert "下周复习" in app.library.get(eid).notes
    assert app.focus.get("tg", "me") == eid


async def test_resummarize_focus(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "resummarize", "target": "focus"}
    await app.handle(m("那条重新总结一下"))
    assert last(ch)[0] == "确定重新总结「复利笔记」吗？"
    await app.handle(m("好的"))
    assert ch.sent[-1][1].startswith("✅ 已保存：复利笔记") and f"id：{eid}" in ch.sent[-1][1]


async def test_resummarize_failure_is_not_reported_as_saved(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "resummarize", "target": "focus"}
    await app.handle(m("那条重新总结一下"))
    state["summary_down"] = True  # haiku fails; local answers "本地", which is not JSON
    await app.handle(m("确认"))
    assert ch.sent[-1][1].startswith("⚠️ 重新生成摘要失败：复利笔记") and ch.sent[-1][1].endswith(f"id：{eid}")


async def test_search_target_with_two_matches_asks_for_an_id(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "复利让财富增长")
    state["intent"] = {"intent": "delete", "target": "search", "query": "复利"}
    await app.handle(m("删掉关于复利的那条"))
    text, buttons = last(ch)
    assert text.startswith("找到好几条，请用 id 说清楚是哪一条：\n")
    assert a in text and b in text and buttons == []
    assert app.ops.latest("tg", "me") is None


async def test_search_target_with_one_match(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    await save(app, "苹果很好吃")
    state["intent"] = {"intent": "delete", "target": "search", "query": "复利"}
    await app.handle(m("删掉关于复利的那条"))
    assert last(ch)[0] == f"确定删除下面 1 条吗？\n「复利笔记」 {a}"


async def test_target_not_found(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "search", "query": "天气"}
    await app.handle(m("删掉关于天气的那条"))
    assert ch.sent[-1][1] == NOT_FOUND
    await app.pipeline.delete(eid)  # the focus still names it, but it is gone
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    assert ch.sent[-1][1] == NOT_FOUND
    assert app.ops.latest("tg", "me") is None


async def test_find_by_query_lists_ids_and_one_match_sets_focus(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    assert app.focus.get("tg", "me") == b
    state["intent"] = {"intent": "find", "query": "复利"}
    await app.handle(m("我存过哪些关于复利的"))
    assert ch.sent[-1][1] == f"找到 1 条：\n{a}  复利笔记"
    assert app.focus.get("tg", "me") == a
    assert app.convos.recent("tg", "me")[-2:] == [("user", "我存过哪些关于复利的"),
                                                  ("assistant", f"找到 1 条：\n{a}  复利笔记")]


async def test_find_by_date(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    day = app.library.get(a).saved_at.date().isoformat()
    state["intent"] = {"intent": "find", "since": day, "until": day}
    await app.handle(m("我今天存了什么"))
    text = ch.sent[-1][1]
    assert text.startswith("找到 2 条：\n") and a in text and b in text
    state["intent"] = {"intent": "find", "since": day, "until": day, "query": "苹果"}
    await app.handle(m("我今天存了哪些苹果的"))
    assert ch.sent[-1][1] == f"找到 1 条：\n{b}  苹果笔记"
    state["intent"] = {"intent": "find", "since": "2020-01-01", "until": "2020-01-31"}
    await app.handle(m("我 2020 年 1 月存了什么"))
    # nothing in that range: the latest ones instead of a bare 没找到 (P6)
    head, *rows = ch.sent[-1][1].split("\n")
    assert head == FIND_FALLBACK and sorted(rows) == sorted([f"{a}  复利笔记", f"{b}  苹果笔记"])


async def test_find_with_a_vague_query_falls_back_to_the_latest(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    state["intent"] = {"intent": "find", "query": "没有这个词"}  # a topic nothing matches
    await app.handle(m("最近存了哪些？"))
    text = ch.sent[-1][1]
    assert text.startswith(FIND_FALLBACK + "\n") and a in text and b in text
    state["intent"] = {"intent": "find", "query": "最近"}  # "最近" alone is no topic: the plain latest list
    await app.handle(m("最近存了哪些？"))
    assert ch.sent[-1][1].startswith("找到 2 条：")
    assert app.focus.get("tg", "me") == b  # a fallback list is not a match: focus stays


async def test_find_query_of_only_filler_words_is_no_topic(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    for query in ("最近存的东西", "最近存了什么", " 最近 存过的内容 ", "存了哪些"):
        state["intent"] = {"intent": "find", "query": query}
        await app.handle(m("最近存了哪些？"))
        text = ch.sent[-1][1]
        assert text.startswith("找到 2 条：") and a in text and b in text, query


async def test_find_fallback_of_one_entry_does_not_set_focus(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    app.focus.clear("tg", "me")
    state["intent"] = {"intent": "find", "query": "没有这个词"}
    await app.handle(m("我存过哪些没有这个词的"))
    assert ch.sent[-1][1] == f"{FIND_FALLBACK}\n{a}  复利笔记"
    assert app.focus.get("tg", "me") is None  # one entry listed, but not one the user asked for


async def test_find_on_an_empty_library_says_none(tmp_path):
    app, ch, state = make_app(tmp_path)
    for intent in ({"intent": "find", "query": "最近"}, {"intent": "find", "since": "2020-01-01", "until": "2020-01-31"},
                   {"intent": "find"}):
        state["intent"] = intent
        await app.handle(m("最近存了哪些？"))
        assert ch.sent[-1][1] == FIND_NONE


async def test_find_by_missing_id_does_not_fall_back(tmp_path):
    app, ch, state = make_app(tmp_path)
    await save(app, "复利需要时间")
    state["intent"] = {"intent": "find", "ids": ["20200101-abcdef"]}
    await app.handle(m("20200101-abcdef 是哪条"))
    assert FIND_FALLBACK not in ch.sent[-1][1]


def test_intent_prompt_leaves_query_empty_for_recent():
    from octopus.manage import INSTRUCTIONS
    assert "用户只是想看最近存了什么、没有具体主题时，query 留空，since/until 为 null。" in INSTRUCTIONS


async def test_save_intent_queues_a_note(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = {"intent": "save", "content": "明天给老王回电话"}
    await app.handle(m("帮我记一下：明天给老王回电话"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "note" and job.payload["text"] == "明天给老王回电话"


async def test_intent_model_failure_answers_normally(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = None  # haiku raises; local returns non-JSON
    await app.handle(m("删掉上面这条"))
    assert state["intent_calls"] == 1
    assert ch.sent[-1][1].startswith("回答") and [b.label for b in last(ch)[1]] == [SAVE_CHAT]


async def test_ordinary_question_skips_the_intent_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("什么是复利"))
    assert state["intent_calls"] == 0
    assert ch.sent[-1][1].startswith("回答")


async def test_ask_prefix_skips_the_intent_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("问：删掉一个文件夹要什么权限"))
    assert state["intent_calls"] == 0
    assert ch.sent[-1][1].startswith("回答")


async def test_button_from_another_user(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    data = last(ch)[1][0].data
    await app.handle(m(callback=data, user="other"))
    assert ch.sent[-1] == ("other", OP_EXPIRED)
    assert app.library.get(eid) is not None
    await app.handle(m("确认", user="other"))  # no op of theirs: chat
    assert app.library.get(eid) is not None


async def test_malformed_op_callbacks_are_ignored(tmp_path):
    app, ch, state = make_app(tmp_path)
    await save(app, "复利需要时间")
    n = len(ch.sent)
    for data in ("op:", "op:1", "op:1:maybe", "op:x:yes", "op:１:yes", "op:1:yes:no"):
        await app.handle(m(callback=data))
    assert len(ch.sent) == n


# ---- fix round 1 ----

async def ask_delete(app, ch, state):
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    return eid


async def test_delete_word_does_not_confirm_another_kind(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "resummarize", "target": "focus"}
    await app.handle(m("那条重新总结一下"))
    summaries = sum("用户说：" not in p for p, _ in app.llm.providers["claude:haiku"].calls)
    state["intent"] = {"intent": "delete", "target": "focus"}  # "删" now asks for a delete instead
    await app.handle(m("删"))
    assert sum("用户说：" not in p for p, _ in app.llm.providers["claude:haiku"].calls) == summaries  # no resummarize
    assert last(ch)[0] == f"确定删除下面 1 条吗？\n「复利笔记」 {eid}"
    await app.handle(m("删吧"))  # for a delete it is a yes
    assert app.library.get(eid) is None


async def test_typed_words_with_punctuation(tmp_path):
    for word, gone in (("确认。", True), ("好的！", True), ("确认～", True), (" 确认，删吧 ", True), ("好吧", True),
                       ("取消。", False), ("算了吧", False)):
        (tmp_path / word.strip()).mkdir()
        app, ch, state = make_app(tmp_path / word.strip())
        eid = await ask_delete(app, ch, state)
        await app.handle(m(word))
        assert (app.library.get(eid) is None) is gone, word
        assert ch.sent[-1][1].startswith("已删除") if gone else ch.sent[-1][1] == OP_CANCELLED, word


async def test_voice_transcript_confirms(tmp_path):
    from octopus.models import Attachment
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="", attachments=[
        Attachment(kind="audio", filename="voice.silk", voice=True, text="确认。")]))
    assert app.library.get(eid) is None and ch.sent[-1][1].startswith("已删除")


async def test_typed_confirm_after_expiry_says_expired(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    later = time.time() + OP_TTL_SECONDS + 1
    app.ops.clock = lambda: later
    sonnet = len(app.llm.providers["claude:sonnet"].calls)
    await app.handle(m("确认。"))
    assert ch.sent[-1][1] == OP_EXPIRED
    assert len(app.llm.providers["claude:sonnet"].calls) == sonnet  # never handed to the QA model
    assert app.library.get(eid) is not None
    await app.handle(m("确认"))  # told once; now it is just chat
    assert ch.sent[-1][1].startswith("回答")


async def test_confirm_turn_is_recorded(tmp_path):
    app, ch, state = make_app(tmp_path)
    await ask_delete(app, ch, state)
    await app.handle(m("确认"))
    assert app.convos.recent("tg", "me")[-2:] == [("user", "确认"), ("assistant", ch.sent[-1][1])]
    await ask_delete(app, ch, state)
    await app.handle(m(callback=last(ch)[1][1].data))  # the 取消 button
    assert app.convos.recent("tg", "me")[-2:] == [("user", "取消"), ("assistant", OP_CANCELLED)]


async def test_save_intent_never_stores_a_paraphrase(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = {"intent": "save", "content": "周四给老王打电话"}  # not what the user said
    await app.handle(m("帮我记一下：明天给老王回电话"))
    [job] = app.jobs.claim_due(10)
    assert job.payload["text"] == "帮我记一下：明天给老王回电话"


async def test_delete_confirmation_names_missing_ids(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete"}
    await app.handle(m(f"删除 {a} 和 20261013-ffffff"))
    assert last(ch)[0] == f"确定删除下面 1 条吗？\n「复利笔记」 {a}\n找不到：20261013-ffffff"
    await app.handle(m("取消"))
    await app.handle(m("删除 20261013-eeeeee 和 20261013-ffffff"))
    assert ch.sent[-1][1] == f"{NOT_FOUND}\n找不到：20261013-eeeeee\n找不到：20261013-ffffff"
    assert app.ops.latest("tg", "me") is None


async def test_oversized_op_id_is_ignored(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(m(callback="op:" + "9" * 30 + ":yes"))
    assert ch.sent == []


def test_typed_answer_normalisation():
    from octopus.app import typed_answer
    assert typed_answer("确认") == (True, False)
    assert typed_answer(" 好的！👍") == (True, False)
    assert typed_answer("删吧。") == (True, True)
    assert typed_answer("确认，删吧") == (True, True)  # mentions 删: a yes only to a delete
    assert typed_answer("不用了吧") == (False, False)
    assert typed_answer("算了吧～") == (False, False)
    assert typed_answer("好，不要") is None  # mixed: not an answer
    for text in ("", "。", "确认一下这个", "存", "问", "好的我知道复利"):
        assert typed_answer(text) is None, text


# ---- greeting guide and tappable commands (Task 5) ----

GUIDE_DATA = ["cmd:recent", "cmd:list", "cmd:status", "cmd:model", "cmd:model local", "cmd:model default", "cmd:delete"]


def provider_calls(app):
    return sum(len(p.calls) for p in app.llm.providers.values())


async def test_greeting_gets_the_guide_without_a_model_call(tmp_path):
    from octopus.commands import HELP

    app, ch, state = make_app(tmp_path)
    for text in ("hi oct", "你好！", "/help"):
        await app.handle(m(text))
        text_sent, buttons = last(ch)
        assert text_sent == HELP
        assert [b.data for b in buttons] == GUIDE_DATA and all(b.fallback == "" for b in buttons)
    assert provider_calls(app) == 0 and state["intent_calls"] == 0
    assert app.jobs.counts() == {} and app.index.count() == 0
    assert app.convos.recent("tg", "me") == []  # not a conversation turn


async def test_tapped_commands_run_like_typed_ones(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    await app.handle(m(callback="cmd:recent"))
    assert ch.sent[-1][1] == await app.commands.run("recent", []) and eid in ch.sent[-1][1]
    app.today = lambda: date.fromisoformat(app.index.recent(1)[0]["saved_at"][:10])
    await app.handle(m(callback="cmd:list"))  # today's, by the app's clock
    assert ch.sent[-1][1] == await app.commands.run("list", []) and eid in ch.sent[-1][1]
    await app.handle(m(callback="cmd:model local"))
    assert ch.sent[-1][1].startswith("已切换到本地模型")
    await app.handle(m(callback="cmd:model default"))
    assert ch.sent[-1][1].startswith("已恢复默认模型")
    await app.handle(m(callback="cmd:help"))
    assert [b.data for b in last(ch)[1]] == GUIDE_DATA


async def test_unknown_tapped_commands_are_ignored(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    n = len(ch.sent)
    for data in ("cmd:rm -rf", "cmd:bogus", "cmd:clawbot", "cmd:resummarize " + eid, "cmd:delete " + eid,
                 "cmd:model x", "cmd:recent 5", "cmd:", "cmd:model  local", "del:../x", "del:", "del:" + eid + "0"):
        await app.handle(m(callback=data))
    assert len(ch.sent) == n and app.library.get(eid) is not None and app.ops.latest("tg", "me") is None


async def test_delete_picker_buttons_ask_before_deleting(tmp_path):
    app, ch, _ = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    for typed in (m("/delete"), m("/del"), m(callback="cmd:delete")):
        await app.handle(typed)
        text, buttons = last(ch)
        assert text.startswith("选择要删除的一条：\n")
        assert {x.data for x in buttons} == {f"del:{a}", f"del:{b}"}
        assert {x.label for x in buttons} == {"删除 复利笔记", "删除 苹果笔记"}
    await app.handle(m(callback=f"del:{a}"))
    text, buttons = last(ch)
    assert text == f"确定删除下面 1 条吗？\n「复利笔记」 {a}"
    assert app.library.get(a) is not None and app.ops.latest("tg", "me")["entry_ids"] == [a]
    await app.handle(m("确认"))  # typed works too: the confirmation was the last thing said
    assert app.library.get(a) is None and app.library.get(b) is not None


async def test_delete_picker_button_for_a_gone_entry(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m(callback="del:20261013-ffffff"))
    assert ch.sent[-1][1] == f"{NOT_FOUND}\n找不到：20261013-ffffff"
    assert app.ops.latest("tg", "me") is None


async def test_typed_delete_with_ids_deletes_directly(tmp_path):
    app, ch, _ = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    await app.handle(m(f"/del {a}，{b}"))
    assert ch.sent[-1][1] == f"已删除：「复利笔记」 {a}\n已删除：「苹果笔记」 {b}\n（文件已移到回收目录）"
    assert app.index.count() == 0 and app.ops.latest("tg", "me") is None


# ---- final review fixes ----

def corrupt(app, eid):
    """A hand edit in Obsidian that breaks the YAML (the README invites editing notes there)."""
    path = app.library.find(eid)
    path.write_text(re.sub(r"(?m)^title: .*$", "title: AI: 未来", path.read_text(encoding="utf-8")), encoding="utf-8")


def intent_prompts(app):
    return [p for p, _ in app.llm.providers["claude:haiku"].calls if "用户说：" in p]


async def test_unreadable_focus_does_not_break_chat_and_can_be_deleted(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    corrupt(app, eid)
    await app.handle(m("帮我查一下天气"))  # a hint word: the intent call reads the focus
    assert ch.sent[-1][1].startswith("回答")
    assert "当前条目：（无）" in intent_prompts(app)[-1]  # unreadable focus = no current entry
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    assert ch.sent[-1][1] == NOT_FOUND
    state["intent"] = {"intent": "delete"}
    await app.handle(m(f"删除 {eid}"))
    assert last(ch)[0] == f"确定删除下面 1 条吗？\n{eid}"  # the id instead of a title
    await app.handle(m("确认"))
    assert ch.sent[-1][1] == f"已删除：{eid}（文件已移到回收目录）"
    assert app.library.find(eid) is None


async def test_unreadable_entry_cannot_be_edited(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    corrupt(app, eid)
    for intent in ({"intent": "retitle", "title": "新标题"}, {"intent": "annotate", "note": "备注"},
                   {"intent": "resummarize"}):
        state["intent"] = intent
        await app.handle(m(f"{eid} 改一下"))
        assert ch.sent[-1][1] == f"{UNREADABLE}{eid}"
    assert app.ops.latest("tg", "me") is None


async def test_unreadable_entry_from_picker_find_and_mid_delete(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    await app.handle(m(callback=f"del:{b}"))  # the picker's button
    data = last(ch)[1][0].data
    corrupt(app, b)  # broken between the question and the 确认
    await app.handle(m(callback=data))
    assert ch.sent[-1][1] == f"已删除：{b}（文件已移到回收目录）"
    corrupt(app, a)
    state["intent"] = {"intent": "find", "ids": [a]}
    await app.handle(m(f"找一下 {a}"))
    assert ch.sent[-1][1] == f"找到 1 条：\n{a}  （文件读不出来）"
    state["intent"] = {"intent": "retitle", "title": "新标题"}
    await app.handle(m(f"把 {a} 标题改成新标题"))
    assert ch.sent[-1][1] == f"{UNREADABLE}{a}"


async def test_edit_of_entry_broken_after_confirmation(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "retitle", "target": "focus", "title": "新标题"}
    await app.handle(m("把上面这条标题改成新标题"))
    corrupt(app, eid)
    await app.handle(m("确认"))
    assert ch.sent[-1][1] == f"{UNREADABLE}{eid}"


def test_more_yes_and_no_words():
    from octopus.app import typed_answer
    for word in ("行", "行的", "嗯", "嗯嗯", "好滴", "没问题", "可以的", "确定的", "ok", "OK", "Okay", "yes", "Y",
                 "確認", "確定", "嗯。", "OK!"):
        assert typed_answer(word) == (True, False), word
    for word in ("别", "不了", "不用了", "no", "No", "n", "不用了吧"):
        assert typed_answer(word) == (False, False), word


async def test_new_yes_words_confirm(tmp_path):
    for word in ("行", "嗯嗯", "OK", "没问题"):
        (tmp_path / word).mkdir()
        app, ch, state = make_app(tmp_path / word)
        eid = await ask_delete(app, ch, state)
        await app.handle(m(word))
        assert app.library.get(eid) is None, word


async def test_short_unrecognised_reply_to_a_confirmation_asks_again(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    data = last(ch)[1][0].data
    calls = provider_calls(app)
    for text in ("嗯哼", "?", "👍", "咋整啊"):
        await app.handle(m(text))
        reply, buttons = last(ch)
        assert reply == ANSWER_PLEASE
        assert [b.data for b in buttons] == [data, data[:-3] + "no"]
        assert provider_calls(app) == calls  # never handed to a model
    assert app.library.get(eid) is not None
    await app.handle(m("确认"))  # re-armed: the typed answer works
    assert app.library.get(eid) is None


async def test_long_reply_to_a_confirmation_is_chat(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    await app.handle(m("复利和单利有什么区别"))
    assert ch.sent[-1][1].startswith("回答")
    assert app.library.get(eid) is not None


async def test_interrupted_confirmation_is_asked_again(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    question, buttons = last(ch)
    app.jobs.enqueue("note", {"text": "苹果很好吃"}, "tg", "me")  # a background save finishes meanwhile
    await app.process_due_jobs()
    calls = provider_calls(app)
    await app.handle(m("好的。"))
    assert app.library.get(eid) is not None
    assert last(ch) == (OP_REASK + question, buttons)  # same op, same buttons
    assert provider_calls(app) == calls  # never handed to a model that might claim it was done
    assert app.convos.recent("tg", "me")[-2:] == [("user", "好的。"), ("assistant", OP_REASK + question)]
    app.jobs.enqueue("note", {"text": "复利让财富增长"}, "tg", "me")  # interrupted once more
    await app.process_due_jobs()
    await app.handle(m("确认"))
    assert last(ch)[0] == OP_REASK + question
    await app.handle(m("确认"))
    assert app.library.get(eid) is None and ch.sent[-1][1].startswith("已删除")
    await app.handle(m("好"))  # no live op left: ordinary chat
    assert ch.sent[-1][1].startswith("回答")


async def test_interrupted_cancel_just_cancels(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    app.jobs.enqueue("note", {"text": "苹果很好吃"}, "tg", "me")
    await app.process_due_jobs()
    await app.handle(m("取消"))
    assert ch.sent[-1][1] == OP_CANCELLED
    assert app.ops.latest("tg", "me") is None and app.library.get(eid) is not None


async def test_yes_after_a_chat_answer_is_chat_even_with_an_op_pending(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    data = last(ch)[1][0].data
    await app.handle(m("复利和单利有什么区别"))  # the model now asked something itself
    await app.handle(m("好"))
    assert ch.sent[-1][1].startswith("回答")  # answers the model's question, not the old confirmation
    assert app.library.get(eid) is not None
    await app.handle(m(callback=data))  # the button still works
    assert app.library.get(eid) is None


async def test_picker_confirmation_can_be_asked_again(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    await app.handle(m(callback=f"del:{eid}"))
    question = last(ch)[0]
    app.jobs.enqueue("note", {"text": "苹果很好吃"}, "tg", "me")
    await app.process_due_jobs()
    await app.handle(m("确认"))
    assert last(ch)[0] == OP_REASK + question


async def test_undelivered_confirmation_keeps_its_hints_in_the_outbox(tmp_path):
    from octopus.channels.base import SendResult

    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    down = {"on": True}
    real_send = ch.send

    async def send(user_id, reply):
        return SendResult(False) if down["on"] else await real_send(user_id, reply)

    ch.send = send
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))
    [(_, text, _)] = app.outbox.queued("tg", "me")
    assert text == f"确定删除下面 1 条吗？\n「复利笔记」 {eid}\n回复“确认”执行\n回复“取消”放弃"


async def test_bare_save_only_right_after_the_answer(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(m("什么是复利"))
    state["intent"] = {"intent": "find", "query": "天气"}
    await app.handle(m("我存过哪些关于天气的"))  # the latest reply is a listing, not that answer
    await app.handle(m("存"))
    assert ch.sent[-1][1] == NO_CORRECTION
    assert app.jobs.claim_due(10) == []
    await app.handle(m("什么是复利"))
    await app.handle(m("存。"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "note" and "AI 回答" in job.payload["text"]


async def test_pasted_text_skips_the_intent_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    state["intent"] = {"intent": "delete", "target": "focus"}
    paste = "这篇文章讲了怎么改进代码\n第二段：总结一下要点\n" + "很长的文字" * 60
    assert len(paste.strip()) >= 300
    await app.handle(m(paste))
    await app.handle(m("记录" + "很长的文字" * 60))
    assert state["intent_calls"] == 0
    assert ch.sent[-1][1].startswith("回答")


async def test_long_single_line_annotate_still_reaches_the_intent_model(tmp_path):
    app, ch, state = make_app(tmp_path)
    await save(app, "复利需要时间")
    note = "下周复习" * 28
    text = "给这条加个备注：" + note
    assert len(text) == 120 and "\n" not in text
    state["intent"] = {"intent": "annotate", "target": "focus", "note": note}
    await app.handle(m(text))
    assert state["intent_calls"] == 1
    assert last(ch)[0] == f"确定给「复利笔记」加备注：{note}"


async def test_multi_line_request_still_reaches_the_intent_model(tmp_path):
    app, ch, state = make_app(tmp_path)
    await save(app, "复利需要时间")
    state["intent"] = {"intent": "retitle", "target": "focus", "title": "时间的朋友"}
    await app.handle(m("把上面这条\n标题改成时间的朋友"))
    assert state["intent_calls"] == 1
    assert last(ch)[0] == "确定把「复利笔记」的标题改成「时间的朋友」吗？"


async def test_short_quote_reply_to_the_prompt_merges_after_the_window(tmp_path):
    from octopus.pending import MERGE_WINDOW_SECONDS

    app, ch, state = make_app(tmp_path)
    await app.handle(m("https://www.reddit.com/r/x/comments/1"))
    await app.process_due_jobs()
    entry_id = app.pending.active("tg", "me")
    prompt_id = f"m{len(ch.sent)}"
    later = time.time() + MERGE_WINDOW_SECONDS + 1
    app.pending.clock = lambda: later
    assert app.pending.active("tg", "me") is None
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="作者是张三，2024 年发的",
                                     reply_to_id=prompt_id))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == entry_id
    assert state["intent_calls"] == 0


async def test_note_from_save_chat_has_no_fix_button(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(m("什么是复利"))
    await app.handle(m(callback=last(ch)[1][0].data))
    await app.process_due_jobs()
    text, buttons = last(ch)
    assert text.startswith("✅ 已保存") and buttons == []


async def test_confirmation_delivered_from_outbox_is_not_answered_by_the_same_message(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    real_send = ch.send
    from octopus.channels.base import SendResult

    async def down(user_id, reply):
        return SendResult(False)

    ch.send = down
    await app.handle(m("删掉上面这条"))  # the confirmation cannot be delivered: it waits in the outbox
    ch.send = real_send
    await app.handle(m("好"))  # flushes the confirmation first; this 好 was not an answer to it
    assert app.library.get(eid) is not None
    assert any(t.startswith("确定删除") for _, t in ch.sent)
    assert ch.sent[-1][1].startswith(OP_REASK)  # asked again instead
    await app.handle(m("确认"))  # an answer to the question the user has now seen
    assert app.library.get(eid) is None


async def test_more_delete_words_confirm_a_delete(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await ask_delete(app, ch, state)
    await app.handle(m("确认删除"))
    assert app.library.get(eid) is None


def test_answer_prompt_forbids_claiming_actions():
    from octopus.qa import SYSTEM
    assert "不要声称已经删除" in SYSTEM


def refuse_first(ch, n=1):
    """ch refuses its next n sends (e.g. ClawBot's send budget used up), then delivers again."""
    from octopus.channels.base import SendResult
    real_send = ch.send
    left = [n]

    async def send(user_id, reply):
        if left[0] > 0:
            left[0] -= 1
            return SendResult(False)
        return await real_send(user_id, reply)

    ch.send = send


async def test_outbox_sweep_delivers_a_held_reply_without_a_user_message(tmp_path):
    app, ch, state = make_app(tmp_path)
    refuse_first(ch)
    await app.handle(m("/model"))
    assert ch.sent == [] and app.outbox.pending_users() == [("tg", "me")]
    await app.sweep_outbox()
    assert len(ch.sent) == 1 and ch.sent[0][1].startswith("当前模型")
    assert app.outbox.count() == 0 and app.outbox.pending_users() == []


async def test_outbox_sweep_that_cannot_deliver_keeps_the_rows(tmp_path):
    app, ch, state = make_app(tmp_path)
    refuse_first(ch, 4)  # /model, /status's flush of it, /status, and the sweep's first try
    await app.handle(m("/model"))
    await app.handle(m("/status"))  # its flush of /model fails too: both stay queued
    before = [tuple(r) for r in app.conn.execute("SELECT text, created_at FROM outbox ORDER BY id")]
    assert len(before) == 2
    await app.sweep_outbox()  # still refused
    assert [tuple(r) for r in app.conn.execute("SELECT text, created_at FROM outbox ORDER BY id")] == before
    assert ch.sent == []

    async def boom(user_id, reply):
        raise OSError("network down")

    ch.send = boom
    await app.sweep_outbox()  # an exception is a failed delivery, nothing more
    assert [tuple(r) for r in app.conn.execute("SELECT text, created_at FROM outbox ORDER BY id")] == before


async def test_outbox_sweep_skips_a_channel_that_is_gone(tmp_path, caplog):
    import logging
    app, ch, state = make_app(tmp_path)
    app.outbox.add("feishu", "me", "旧回复")
    with caplog.at_level(logging.INFO):
        await app.sweep_outbox()
    assert app.outbox.count() == 1 and not caplog.records


async def test_confirmation_delivered_by_the_sweep_is_answered_by_the_next_confirm(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    refuse_first(ch)
    await app.handle(m("删掉上面这条"))  # the confirmation is held in the outbox
    assert app.outbox.count() == 1
    await app.sweep_outbox()  # the user now sees the question, with its typed hints
    assert ch.sent[-1][1].startswith("确定删除") and "回复“确认”执行" in ch.sent[-1][1]
    await app.handle(m("确认"))  # an answer to the question just seen: runs it, no re-ask
    assert app.library.get(eid) is None
    assert not any(t.startswith(OP_REASK) for _, t in ch.sent)


async def test_outbox_sweep_waits_for_the_user_lock(tmp_path):
    import asyncio
    app, ch, state = make_app(tmp_path)
    app.outbox.add("tg", "me", "旧回复")
    lock = app._user_locks.setdefault(("tg", "me"), asyncio.Lock())
    await lock.acquire()  # a message of this user is being handled
    sweep = asyncio.create_task(app.sweep_outbox())
    await asyncio.sleep(0.05)
    assert ch.sent == [] and not sweep.done()  # never interleaves with handle
    lock.release()
    await asyncio.wait_for(sweep, 2)
    assert ch.sent == [("me", "旧回复")]


async def test_outbox_sweep_and_handle_never_interleave(tmp_path):
    import asyncio
    app, ch, state = make_app(tmp_path)
    app.outbox.add("tg", "me", "旧回复")
    real_send = ch.send
    order = []

    async def slow_send(user_id, reply):
        order.append(("start", reply.text[:3]))
        await asyncio.sleep(0.02)
        result = await real_send(user_id, reply)
        order.append(("end", reply.text[:3]))
        return result

    ch.send = slow_send
    await asyncio.gather(app.handle(m("/model")), app.sweep_outbox())
    texts = [t for _, t in ch.sent]
    assert texts[0] == "旧回复" and texts[1].startswith("当前模型") and len(texts) == 2  # delivered once, in order
    assert [k for k, _ in order] == ["start", "end", "start", "end"]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def refusing(ch):
    """ch delivers only while ch.up; every attempt is counted in ch.tries."""
    from octopus.channels.base import SendResult
    real_send = ch.send
    ch.up, ch.tries = False, 0
    ch.sent.clear()  # from here on: only what this channel delivers

    async def send(user_id, reply):
        ch.tries += 1
        return await real_send(user_id, reply) if ch.up else SendResult(False)

    ch.send = send
    return ch


async def test_unseen_confirmation_is_never_run_by_a_typed_confirm(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    refusing(ch)
    await app.handle(m("删掉上面这条"))  # the question is held: the user never saw it
    await app.handle(m("确认"))  # its flush fails too: this 确认 answers nothing the user has seen
    assert app.library.get(eid) is not None
    [(_, text, op_id)] = app.outbox.queued("tg", "me")  # one question, re-asked, not two
    assert text.startswith(OP_REASK) and op_id == app.ops.latest("tg", "me")["id"]
    ch.up = True
    await app.sweep_outbox()
    assert ch.sent[-1][1].startswith(OP_REASK)
    await app.handle(m("确认"))  # now the user has seen it
    assert app.library.get(eid) is None


async def test_unseen_confirmation_answered_while_flush_fails_by_a_button_word(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "target": "focus"}
    refusing(ch)
    await app.handle(m("删掉上面这条"))
    await app.handle(m("删吧"))
    assert app.library.get(eid) is not None
    assert app.ops.latest("tg", "me") is not None  # still waiting for an answer to what the user will see


async def test_a_stale_confirmation_is_not_delivered_after_a_newer_one(tmp_path):
    app, ch, state = make_app(tmp_path)
    a = await save(app, "复利需要时间")
    b = await save(app, "苹果很好吃")
    refusing(ch)
    state["intent"] = {"intent": "delete", "ids": [a]}
    await app.handle(m(f"删掉 {a}"))
    state["intent"] = {"intent": "retitle", "ids": [b], "title": "水果"}
    await app.handle(m(f"把 {b} 的标题改成水果"))
    assert len(app.outbox.queued("tg", "me")) == 2
    ch.up = True
    await app.sweep_outbox()
    texts = [t for _, t in ch.sent]
    assert len(texts) == 1 and "水果" in texts[0] and not any(t.startswith("确定删除") for t in texts)
    assert app.outbox.count() == 0
    await app.handle(m("确认"))
    assert app.library.get(b).title == "水果" and app.library.get(a) is not None


async def test_an_expired_confirmation_is_dropped_not_delivered(tmp_path):
    from octopus.ops import Ops
    app, ch, state = make_app(tmp_path)
    clock = Clock()
    app.ops = Ops(app.conn, clock)
    a = await save(app, "复利需要时间")
    refusing(ch)
    state["intent"] = {"intent": "delete", "ids": [a]}
    await app.handle(m(f"删掉 {a}"))
    assert app.outbox.count() == 1
    clock.now += OP_TTL_SECONDS + 1
    ch.up = True
    await app.sweep_outbox()
    assert ch.sent == [] and app.outbox.count() == 0
    await app.handle(m("/model"))  # nothing held any more for the next message either
    assert len(ch.sent) == 1 and ch.sent[0][1].startswith("当前模型")


async def test_sweep_backs_off_a_user_it_cannot_reach(tmp_path):
    app, ch, state = make_app(tmp_path)
    clock = Clock()
    app.monotonic = clock
    refusing(ch)
    app.outbox.add("tg", "me", "旧回复")
    tries = []
    for minute in range(16):
        before = ch.tries
        await app.sweep_outbox()
        if ch.tries > before:
            tries.append(minute)
        clock.now += 60
    assert tries == [0, 1, 3, 7, 15]  # then waits 1, 2, 4, 8 minutes
    before = ch.tries
    for _ in range(8):
        await app.sweep_outbox()
        clock.now += 60
    assert ch.tries - before == 1  # capped at 8 minutes: still tried every 8
    ch.up = True
    clock.now += 8 * 60
    await app.sweep_outbox()
    assert ch.sent == [("me", "旧回复")]
    ch.up = False
    app.outbox.add("tg", "me", "新回复")  # delivered once, so the backoff starts over
    before = ch.tries
    await app.sweep_outbox()
    clock.now += 60
    await app.sweep_outbox()
    assert ch.tries - before == 2


async def test_an_incoming_message_resets_the_sweep_backoff(tmp_path):
    app, ch, state = make_app(tmp_path)
    clock = Clock()
    app.monotonic = clock
    refusing(ch)
    app.outbox.add("tg", "me", "旧回复")
    for _ in range(2):
        await app.sweep_outbox()  # tried at minute 0 and 1: next at minute 3
        clock.now += 60
    before = ch.tries
    await app.sweep_outbox()  # minute 2: backed off
    assert ch.tries == before
    await app.handle(m("/model"))  # the user writes: they can be reached again
    before = ch.tries
    await app.sweep_outbox()
    assert ch.tries == before + 1 and ch.sent == []


async def test_run_worker_sweeps_the_outbox(tmp_path):
    import asyncio
    app, ch, state = make_app(tmp_path)
    app.outbox_sweep_interval = 0.01
    app.tick = 0.01
    refuse_first(ch)
    await app.handle(m("/model"))
    assert app.outbox.count() == 1
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    for _ in range(200):
        if ch.sent:
            break
        await asyncio.sleep(0.01)
    assert len(ch.sent) == 1 and ch.sent[0][1].startswith("当前模型")  # no user message needed
    stop.set()
    await asyncio.wait_for(worker, 2)


async def test_run_worker_cancels_a_running_sweep_when_it_stops(tmp_path):
    import asyncio
    app, ch, state = make_app(tmp_path)
    started, cancelled = asyncio.Event(), []

    async def stuck():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    app.sweep_outbox = stuck
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await asyncio.wait_for(started.wait(), 2)
    stop.set()
    await asyncio.wait_for(worker, 2)
    assert cancelled == [True]


async def test_run_worker_survives_a_failing_sweep(tmp_path):
    import asyncio
    app, ch, state = make_app(tmp_path)
    app.outbox_sweep_interval = 0
    app.tick = 0
    calls, again = [], asyncio.Event()

    async def boom():
        calls.append(1)
        if len(calls) >= 3:
            again.set()
        raise RuntimeError("db locked")

    app.sweep_outbox = boom
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    await asyncio.wait_for(again.wait(), 2)  # it failed, and the worker swept again on schedule
    assert not worker.done()
    stop.set()
    await asyncio.wait_for(worker, 2)


async def _put(app, eid, title):
    app.library.save(Entry(id=eid, title=title, source="note", saved_at=datetime(2026, 10, 13, tzinfo=CST),
                           channel="cli", summary="摘要"))


async def test_delete_by_short_id_asks_confirmation_with_full_id(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-6e43c1", "甲")
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 43c1"))
    text, buttons = last(ch)
    assert text == "确定删除下面 1 条吗？\n「甲」 20261013-6e43c1"
    assert app.library.get("20261013-6e43c1") is not None
    await app.handle(m(callback=buttons[0].data))
    assert app.library.get("20261013-6e43c1") is None


async def test_ambiguous_short_id_lists_and_records_no_op(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-9eb123", "甲")
    await _put(app, "20261012-aaa123", "乙")
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 123"))
    assert ch.sent[-1][1] == AMBIGUOUS_ID.format(token="123") + "\n20261013-9eb123  甲\n20261012-aaa123  乙"
    assert app.ops.latest("tg", "me") is None


async def test_word_without_digit_is_not_a_short_id(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-aaaadd", "甲")
    state["intent"] = {"intent": "answer"}
    await app.handle(m("add 一下这个"))
    assert state["intent_calls"] == 0  # no management word, no id: straight to the answer


async def test_year_in_a_question_costs_no_intent_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-aaaadd", "甲")
    await app.handle(m("2026年AI有什么趋势"))
    assert state["intent_calls"] == 0
    await _put(app, "20261012-ab2026", "乙")
    await app.handle(m("2026年AI有什么趋势"))
    assert state["intent_calls"] == 0  # a token naming an entry, but only asked about: the answer path
    await app.handle(m("2026年AI趋势"))
    assert state["intent_calls"] == 1  # an entry ending ...2026 makes the token a candidate


async def test_resolved_and_ambiguous_tokens_together_ask_nothing_yet(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-6e43c1", "甲")
    await _put(app, "20261013-9eb123", "乙")
    await _put(app, "20261012-aaa123", "丙")
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 43c1 和 123"))
    assert ch.sent[-1][1] == AMBIGUOUS_ID.format(token="123") + "\n20261013-9eb123  乙\n20261012-aaa123  丙"
    assert app.ops.latest("tg", "me") is None


async def _indexed(app, eid, title, body):
    entry = Entry(id=eid, title=title, source="web", saved_at=datetime(2026, 10, 15, tzinfo=CST), channel="cli",
                  summary=f"{title}的摘要", body=body)
    await app.index.upsert(entry, app.library.save(entry))


def answer_prompts(app):
    return [prompt for prompt, _ in app.llm.providers["claude:sonnet"].calls]


async def two_articles(app):
    await _indexed(app, "20261015-b305ad", "Acme宣布全面迁移Rust", "开头说了原因。\n\n" + "细节。" * 900 + "\n\n结尾的话。")
    await _indexed(app, "20261013-aaaaaa", "家庭烘焙配方", "每天走路讲了什么。")


async def test_an_id_in_a_question_pins_that_entry_in_full(tmp_path):
    for n, question in enumerate(["b305ad 讲了什么", "id：20261015-b305ad 这篇文章讲了😧", "b305ad 的要点", "305ad 说了啥？"]):
        (tmp_path / str(n)).mkdir()
        app, ch, state = make_app(tmp_path / str(n))
        await two_articles(app)
        await app.handle(m(question))
        assert state["intent_calls"] == 0, question  # a question, not a find
        [prompt] = answer_prompts(app)
        head = f"[1] 《Acme宣布全面迁移Rust》 id：20261015-b305ad\n{PINNED_MARK}\n"
        assert head in prompt and "开头说了原因。" in prompt and "结尾的话。" in prompt, question
        assert "[2] 《家庭烘焙配方》 id：20261013-aaaaaa" in prompt
        assert ch.sent[-1][1].startswith("回答 [1]")
        assert "[1] Acme宣布全面迁移Rust（id：20261015-b305ad）" in ch.sent[-1][1]
        assert app.focus.get("tg", "me") == "20261015-b305ad"


async def test_an_ambiguous_short_id_in_a_question_lists_the_entries_without_a_model_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-9eb123", "甲")
    await _put(app, "20261012-aaa123", "乙")
    await app.handle(m("编号 123 讲了什么"))  # digits only: marked as an id
    assert ch.sent[-1][1] == AMBIGUOUS_ID.format(token="123") + "\n20261013-9eb123  甲\n20261012-aaa123  乙"
    assert answer_prompts(app) == [] and state["intent_calls"] == 0
    await _put(app, "20261011-bbbc12", "丙")
    await _put(app, "20261010-cccc12", "丁")
    state["intent"] = {"intent": "answer"}
    await app.handle(m("上面 id c12 讲了什么"))  # the intent model says answer: the same list
    assert ch.sent[-1][1].startswith(AMBIGUOUS_ID.format(token="c12")) and answer_prompts(app) == []


async def test_a_number_in_a_question_is_not_an_id(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-aaa100", "甲文", "正文甲。")
    await _indexed(app, "20261012-bbb100", "乙文", "正文乙。")
    await _indexed(app, "20261012-ab2026", "丙文", "正文丙。")
    for question in ("我有100块钱怎么理财", "2026年AI有什么趋势"):
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答 [1]"), question  # answered: no list
        assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question
    await _indexed(app, "20261012-731542", "丁文", "正文丁。")
    for question in ("id 731542 讲了什么", "ID：731542 讲了什么", "记录号731542的要点"):
        await app.handle(m(question))
        assert f"《丁文》 id：20261012-731542\n{PINNED_MARK}\n" in answer_prompts(app)[-1], question
    await app.handle(m("20261012-ab2026 讲了什么"))  # a full id always counts
    assert f"《丙文》 id：20261012-ab2026\n{PINNED_MARK}\n" in answer_prompts(app)[-1]


async def test_a_management_request_with_an_id_is_still_one(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 b305ad"))
    assert ch.sent[-1][1] == "确定删除下面 1 条吗？\n「Acme宣布全面迁移Rust」 20261015-b305ad"
    assert answer_prompts(app) == []


async def test_a_title_in_a_question_pins_that_entry(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await _indexed(app, "20261013-bbbbbb", "家庭烘焙配方（续）", "第二部分。")
    await app.handle(m("《迁移Rust》讲了什么"))
    assert f"[1] 《Acme宣布全面迁移Rust》 id：20261015-b305ad\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
    await app.handle(m("家庭烘焙配方（续） 的要点"))  # the whole title, typed as it is
    assert f"[1] 《家庭烘焙配方（续）》 id：20261013-bbbbbb\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
    await app.handle(m("《家庭烘焙》讲了什么"))  # two titles have it: plain search, no question back
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]


async def test_this_one_pins_the_entry_just_saved(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("这篇文章讲了什么"))  # nothing current yet: plain search
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]
    eid = await save(app, "复利需要时间")  # the save makes it the current entry
    await app.handle(m("这篇文章讲了什么"))
    assert f"[1] 《复利笔记》 id：{eid}\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
    assert state["intent_calls"] == 0


async def test_an_answer_makes_its_entry_the_current_one(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("b305ad 讲了什么"))
    await save(app, "复利需要时间")
    await app.handle(m("家庭烘焙配方 讲了什么"))
    assert app.focus.get("tg", "me") == "20261013-aaaaaa"  # pinned
    await app.handle(m("这篇说了什么"))
    assert f"[1] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
    state["intent"] = {"intent": "delete", "target": "focus"}
    await app.handle(m("删掉上面这条"))  # the entry the answer was about, asked 确认 by title
    assert ch.sent[-1][1] == "确定删除下面 1 条吗？\n「家庭烘焙配方」 20261013-aaaaaa"


async def test_an_answer_from_search_makes_its_first_cited_source_current(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-aaaaaa", "家庭烘焙配方", "每天走路。")
    await app.handle(m("走路有什么好处"))  # sonnet answers "回答 [1]"
    assert app.focus.get("tg", "me") == "20261013-aaaaaa"


async def test_a_note_turned_into_a_question_does_not_pin_itself(tmp_path):
    app, ch, state = make_app(tmp_path)
    eid = await save(app, "这篇讲了什么")
    await app.handle(m("问"))
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]
    assert app.library.get(eid) is None


async def test_tech_words_in_a_question_are_not_short_ids(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-aaab2b", "甲", "正文甲")
    await _indexed(app, "20261012-cccb2b", "乙", "正文乙")
    await _indexed(app, "20261011-aaae2e", "家庭烘焙配方", "每天走路")
    await _indexed(app, "20261010-0a100a", "丙", "正文丙")
    for question in ("B2B 模式有什么优缺点", "e2e 测试怎么写", "A100 显卡多少钱"):
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答 [1]"), question  # answered, no list
        assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question
    await app.handle(m("id b2b 讲了什么"))  # marked: the list, as for other short ids
    assert ch.sent[-1][1].startswith(AMBIGUOUS_ID.format(token="b2b"))


async def test_an_unmarked_token_naming_several_entries_is_ignored(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-ab305a", "甲", "正文甲")
    await _indexed(app, "20261012-bb305a", "乙", "正文乙")
    await app.handle(m("b305a 讲了什么"))
    assert ch.sent[-1][1].startswith("回答 [1]") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]


async def test_a_typed_id_that_is_not_there_never_pins_the_current_entry(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await save(app, "复利需要时间")  # the current entry
    calls = len(answer_prompts(app))
    await app.handle(m("20261015-b305ae 这篇讲了什么"))  # a typo
    assert ch.sent[-1][1] == MISSING_ID.format(id="20261015-b305ae") and len(answer_prompts(app)) == calls
    await app.handle(m("b305ae 这篇讲了什么"))  # a short one: plain search
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]
    await app.handle(m("120261015-b305ad7 讲了什么"))  # not an id: digits glued on
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]


async def test_a_quote_reply_does_not_pin_the_current_entry(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await save(app, "复利需要时间")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="这篇讲了什么", reply_to_id="old-message"))
    assert f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]  # the quoted message may be about another entry
    app.pending.set("tg", "me", "20261013-aaaaaa", prompt_message_id="prompt-1")  # a prompt asking for material
    app.pending.clear("tg", "me")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="问：这篇讲了什么", reply_to_id="prompt-1"))
    assert f"[1] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in answer_prompts(app)[-1]


async def test_an_unreadable_pinned_entry_is_left_out(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    app.library.find("20261015-b305ad").write_text("---\nid: [broken\n---\n", encoding="utf-8")
    await app.handle(m("b305ad 讲了什么"))
    assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]


async def test_this_one_after_the_current_entry_was_deleted_is_a_plain_search(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    eid = await save(app, "复利需要时间")
    app.library.delete(eid)
    await app.handle(m("这篇讲了什么"))
    assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]


async def test_retitle_by_short_id_still_asks_to_confirm(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    state["intent"] = {"intent": "retitle", "ids": [], "title": "X"}
    await app.handle(m("b305ad 改标题为 X"))
    assert ch.sent[-1][1] == "确定把「Acme宣布全面迁移Rust」的标题改成「X」吗？" and answer_prompts(app) == []


async def test_an_ambiguous_id_keeps_a_note_being_turned_into_a_question(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-9eb123", "甲")
    await _put(app, "20261012-aaa123", "乙")
    eid = await save(app, "编号 123 讲了什么")
    await app.handle(m("问"))
    assert ch.sent[-1][1].startswith(AMBIGUOUS_ID.format(token="123")) and answer_prompts(app) == []
    assert app.library.get(eid) is not None


async def test_a_short_id_with_this_one_pins_the_entry_named(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await _indexed(app, "20261012-aaac12", "甲", "正文甲")
    await _indexed(app, "20261011-bbbc12", "乙", "正文乙")
    await save(app, "复利需要时间")  # the current entry
    for question in ("05ad 这篇讲了什么", "5ad 这篇讲了什么"):
        await app.handle(m(question))
        assert f"[1] 《Acme宣布全面迁移Rust》 id：20261015-b305ad\n{PINNED_MARK}\n" in answer_prompts(app)[-1], question
    for question in ("12ae 这篇讲了什么", "c12 这篇讲了什么"):  # names none / several: a plain answer, no list
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question


async def test_a_missing_full_id_beside_an_existing_one_is_noted_above_the_answer(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("20261013-abcdef 和 20261015-b305ad 有什么区别"))
    assert f"[1] 《Acme宣布全面迁移Rust》 id：20261015-b305ad\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
    assert ch.sent[-1][1].startswith(MISSING_ID.format(id="20261013-abcdef") + "\n回答 [1]")
    calls = len(answer_prompts(app))
    await app.handle(m("20261013-abcdef 和 20261013-fedcba 有什么区别"))  # none there: only the line
    assert ch.sent[-1][1] == MISSING_ID.format(id="20261013-abcdef、20261013-fedcba")
    assert len(answer_prompts(app)) == calls


async def test_a_number_beside_this_one_keeps_the_current_entry(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-aaa100", "甲文", "正文甲。")
    eid = await save(app, "复利需要时间")  # the current entry
    for question in ("这篇文章说的 100 个技巧有哪些", "这篇讲的 2026 年计划是什么"):
        await app.handle(m(question))
        assert f"[1] 《复利笔记》 id：{eid}\n{PINNED_MARK}\n" in answer_prompts(app)[-1], question
        assert f"《甲文》 id：20261013-aaa100\n{PINNED_MARK}" not in answer_prompts(app)[-1], question


async def test_a_missing_full_id_is_named_beside_an_ambiguous_list(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-9eb123", "甲")
    await _put(app, "20261012-aaa123", "乙")
    await app.handle(m("20261013-abcdef 和 编号 123 讲了什么"))
    assert ch.sent[-1][1] == (MISSING_ID.format(id="20261013-abcdef") + "\n" + AMBIGUOUS_ID.format(token="123")
                              + "\n20261013-9eb123  甲\n20261012-aaa123  乙")
