"""P25 Part B fix round 1 (review p25b): the live Chinese user's everyday traffic stays Chinese, English words never
reach into it, and a note stays in one language."""
from datetime import date

import pytest
from fakes import FakeEmbedder, FakeProvider
from test_app_manage import last, m, save
from test_app_manage import make_app as manage_app

from octopus import i18n, qa
from octopus.app import typed_answer
from octopus.config import load_config
from octopus.db import connect
from octopus.index import Index
from octopus.library import parse, render
from octopus.llm.router import LLMRouter
from octopus.manage import build_prompt, wants_manage
from octopus.models import IncomingMessage
from octopus.router import RouteKind, bare_word, classify
from octopus.settings import Settings
from octopus.summarize import INSTRUCTIONS, SYSTEM, summarize, summary_language
from octopus.text import text_language

MIXED_ZH = ["LangChain 和 LlamaIndex 有什么区别", "Claude Code hooks 怎么用", "Python asyncio 的 event loop 原理",
            "React useEffect cleanup 什么时候执行", "用 Docker compose 部署 Postgres 和 Redis", "hi, what is 复利"]
ARTICLE_WITH_CODE = ("Go 的并发模型基于 goroutine 和 channel。下面是一个例子：\n\n```go\npackage main\n\nimport \"fmt\"\n\n"
                     "func main() {\n\tch := make(chan int)\n\tgo func() { ch <- 42 }()\n\tfmt.Println(<-ch)\n}\n```\n\n"
                     "调用 `make(chan int)` 创建一个无缓冲的 channel，发送和接收会互相等待。")


def route(text):
    return classify(IncomingMessage(channel="tg", user_id="me", text=text))


# ---- I1: detection counts Chinese characters against Latin words, without code, biased to the configured language ----

@pytest.mark.parametrize("text", [*MIXED_ZH, ARTICLE_WITH_CODE, "Claude Code 发布了 hooks 功能，可以在 PreToolUse 和 "
                                  "PostToolUse 时运行 shell command。"])
def test_mixed_chinese_stays_chinese_with_a_chinese_setup(text):
    assert text_language(text, "zh") == "zh"


@pytest.mark.parametrize("text,configured,lang", [
    ("what is RAG", "zh", "en"), ("Compounding takes time.", "zh", "en"), ("12345", "zh", "zh"), ("12345", "en", "en"),
    ("what is 复利", "en", "en"), ("复利是什么", "en", "zh"), ("复 is it", "en", "en"),
    ("这是一篇讲复利的文章，`compound()` 只是例子", "en", "zh"), ("`print('x')`", "zh", "zh")])
def test_detection_leaves_the_configured_language_only_when_the_other_clearly_dominates(text, configured, lang):
    assert text_language(text, configured) == lang


@pytest.mark.parametrize("question", MIXED_ZH)
async def test_mixed_chinese_questions_get_the_old_chinese_prompt(tmp_path, question):
    p = FakeProvider("claude:sonnet", "答")
    conn = connect(tmp_path / "db.sqlite", embed_dim=FakeEmbedder.dim)
    from octopus.config import TaskModels

    llm = LLMRouter({"claude:sonnet": p}, {"summarize": TaskModels("claude:sonnet", None),
                                           "answer": TaskModels("claude:sonnet", None)}, Settings(conn))
    await qa.answer(question, Index(conn, FakeEmbedder()), llm, [])
    prompt, system = p.calls[-1]
    assert prompt.startswith(f"最近的对话：\n（无）\n\n资料：\n（无）\n\n问题：{question}\n\n要求：优先根据资料用中文回答")
    assert system == qa.SYSTEM
    assert build_prompt(question, [], None, [], date(2026, 10, 13)).endswith(f"用户说：{question}\n\n{qa_manage_instructions()}")


def qa_manage_instructions():
    from octopus.manage import INSTRUCTIONS as MANAGE

    return MANAGE


# ---- I2: summaries follow `language` unless summary_language says otherwise ----

def test_summary_language_follows_language_unless_set(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path).summary_language == "zh"
    path.write_text('language = "en"\n', encoding="utf-8")
    assert load_config(path).summary_language == "en"
    path.write_text('language = "en"\nsummary_language = "auto"\n', encoding="utf-8")
    assert load_config(path).summary_language == "auto"


async def test_the_live_user_gets_chinese_summaries_of_english_content(tmp_path):
    from test_app import make_app

    app, ch, providers = make_app(tmp_path)  # no summary_language in its config
    await app.pipeline.save_note("Long-term investors believe in compounding and patience.", "tg")
    assert providers["claude:haiku"].calls[-1] == (
        f"{INSTRUCTIONS}\n原标题：（无）\n\n内容：\nLong-term investors believe in compounding and patience.", SYSTEM)


@pytest.mark.parametrize("setting,text,lang", [("auto", ARTICLE_WITH_CODE, "zh"), ("auto", "English content here", "en"),
                                               ("zh", "English content", "zh"), ("en", "中文内容", "en"),
                                               ("", "English content", "zh")])
def test_summary_language_settings(setting, text, lang):
    assert summary_language(setting, text) == lang


async def test_a_chinese_article_with_code_gets_a_chinese_summary_even_with_auto(tmp_path):
    from test_english_prompts import SUMMARY, router

    p = FakeProvider("claude:haiku", SUMMARY)
    llm, _ = router(tmp_path, p)
    await summarize(llm, ARTICLE_WITH_CODE, language="auto")
    assert p.calls[-1] == (f"{INSTRUCTIONS}\n原标题：（无）\n\n内容：\n{ARTICLE_WITH_CODE}", SYSTEM)


# ---- I3: English management words only for English messages, as a request ----

@pytest.mark.parametrize("text", ["Python list 和 tuple 的区别", "SQL delete 语句怎么写", "HTML title 标签", "DNS record 是什么",
                                  "Redis list 怎么用"])
def test_chinese_tech_questions_make_no_intent_call(text):
    assert not wants_manage(text)


@pytest.mark.parametrize("text", ["what is a linked list", "how do I delete a git branch", "is the note on C sharp?",
                                  "the saved game is gone", "what does Python's list do?"])
def test_english_questions_with_those_nouns_are_questions(text):
    assert not wants_manage(text)


@pytest.mark.parametrize("text", ["delete the one above", "please remove that entry", "list my notes about AI",
                                  "show me what I saved last week", "what did I save recently?", "summarize this one again",
                                  "can you find my notes about Reddit", "rename this entry to Compounding"])
def test_english_requests_still_ask_the_intent_model(text):
    assert wants_manage(text)


async def test_a_chinese_question_with_delete_in_it_is_answered_without_an_intent_call(tmp_path):
    app, ch, state = manage_app(tmp_path)
    await app.handle(m("SQL delete 语句怎么写"))
    assert state["intent_calls"] == 0 and last(ch)[0].startswith("回答")


# ---- I4: a /command is never a typed yes or no ----

@pytest.mark.parametrize("text", ["/delete", "/delete ", "/Delete", "／delete", "/ok", "/取消"])
def test_slash_commands_are_not_typed_answers(text):
    assert typed_answer(text) is None


async def test_slash_delete_with_a_delete_confirmation_open_shows_the_picker(tmp_path):
    app, ch, state = manage_app(tmp_path)
    eid = await save(app, "复利需要时间")
    state["intent"] = {"intent": "delete", "ids": [], "target": "focus"}
    await app.handle(m("删掉上面这条"))
    assert last(ch)[0].startswith("确定删除下面 1 条吗？")
    await app.handle(m("/delete"))
    assert last(ch)[0].startswith("选择要删除的一条：") and app.library.find(eid)


# ---- I5: English save needs save: or save, ----

@pytest.mark.parametrize("text", ["save the world from climate change?", "save me a seat", "Save money on flights?",
                                  "save vs invest — which is better"])
def test_save_followed_by_words_is_a_question(text):
    assert route(text).kind is RouteKind.QUESTION


@pytest.mark.parametrize("text,content", [("save: buy milk", "buy milk"), ("Save, buy milk.", "buy milk."),
                                          ("存 买牛奶", "买牛奶"), ("保存 买牛奶", "买牛奶"), ("存：买牛奶", "买牛奶")])
def test_save_prefixes(text, content):
    r = route(text)
    assert r.kind is RouteKind.SAVE_NOTE and r.text == content


async def test_save_the_world_gets_an_answer(tmp_path):
    from test_app import make_app

    app, ch, providers = make_app(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="save the world from climate change?"))
    await app.process_due_jobs()
    assert app.index.count() == 0 and ch.sent[-1][1].startswith("复利需要时间")


# ---- m1 / m2: a note's language is its first heading's; added lines follow it ----

EN_HEAD = ("---\nid: 20261013-abc123\ntitle: Paper\nsource: manual\nsaved_at: '2026-10-13T09:30:00+08:00'\n"
           "channel: tg\ntags: []\nstatus: ok\n---\n\n## Summary\n\ns\n\n## Key points\n\n- p\n\n## Original text\n\n")


def test_a_chinese_heading_in_an_english_notes_body_does_not_switch_it():
    text = EN_HEAD + "Intro\n\n## 摘要\n\n论文的中文摘要\n\n## 原文或转写稿\n\nmore\n"
    e = parse(text)
    assert e.heading_lang == "en" and "## 摘要" in e.body
    assert render(e) == text


async def test_lines_added_to_a_note_follow_its_own_language(tmp_path):
    from test_app_media import make_app as media_app
    from test_app_media import m as mm
    from test_app_media import staged

    app, ch, _ = media_app(tmp_path)
    await app.handle(mm("/s 第一部分"))  # a Chinese note
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    i18n.set_language("en")  # the setup switches to English later
    await app.handle(mm(f"/add {eid}"))
    await app.handle(mm("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    body = app.library.get(eid).body
    assert "\n## 补充内容（" in body and "### 图片 1（b.png）" in body and "附件：attachments/" in body
    assert "## Added" not in body and "Attachment:" not in body
    await app.pipeline.annotate(eid, "重读")
    assert app.library.get(eid).notes.startswith("（")


# ---- m3: /存 and /问 are the corrections again ----

def test_slash_chinese_corrections():
    assert bare_word("/存") == "存" and bare_word("/问") == "问"
    assert bare_word("/save") is None and bare_word("/ask") is None
