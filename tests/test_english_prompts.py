"""P25 Part B: a summary is written in `language` unless summary_language says "zh", "en" or "auto" (the content's),
an answer in its question's language; Chinese content and questions get exactly the prompts they always did."""
import json

import pytest
from fakes import FakeEmbedder, FakeProvider

from octopus import i18n, manage, qa
from octopus.config import ConfigError, load_config
from octopus.db import connect
from octopus.index import Index
from octopus.llm.router import LLMRouter
from octopus.settings import Settings
from octopus.summarize import INSTRUCTIONS, SYSTEM, summarize, summary_language
from octopus.text import text_language

SUMMARY = json.dumps({"title": "t", "summary": "s", "points": ["p"], "tags": ["x"], "language": "en"})


def router(tmp_path, provider):
    cfg_conn = connect(tmp_path / "db.sqlite", embed_dim=FakeEmbedder.dim)
    from octopus.config import TaskModels

    tasks = {"summarize": TaskModels("claude:haiku", None), "answer": TaskModels("claude:haiku", None)}
    return LLMRouter({"claude:haiku": provider}, tasks, Settings(cfg_conn)), cfg_conn


@pytest.mark.parametrize("text,lang", [("复利需要时间，越早开始越好。", "zh"), ("Compounding takes time.", "en"),
                                       ("Python 是什么", "zh"), ("@b305ad 讲了什么", "zh"), ("b305ad what is it about", "en"),
                                       ("12345 ...", "zh"), ("https://a.com/x", "zh")])
def test_text_language(text, lang):
    assert text_language(text, "zh") == lang


@pytest.mark.parametrize("setting,text,lang", [("auto", "中文内容", "zh"), ("auto", "English content", "en"),
                                               ("zh", "English content", "zh"), ("en", "中文内容", "en")])
def test_summary_language(setting, text, lang):
    assert summary_language(setting, text) == lang


def test_auto_with_neither_follows_the_configured_language():
    assert summary_language("auto", "12345") == "zh"
    i18n.set_language("en")
    assert summary_language("auto", "12345") == "en"


async def test_chinese_content_gets_the_old_prompt_byte_for_byte(tmp_path):
    p = FakeProvider("claude:haiku", SUMMARY)
    llm, _ = router(tmp_path, p)
    await summarize(llm, "正文" * 10, title_hint="原标题")
    assert p.calls == [(f"{INSTRUCTIONS}\n原标题：原标题\n\n内容：\n{'正文' * 10}", SYSTEM)]
    await summarize(llm, "正文" * 10)
    assert p.calls[-1][0] == f"{INSTRUCTIONS}\n原标题：（无）\n\n内容：\n{'正文' * 10}"


async def test_english_content_gets_an_english_prompt(tmp_path):
    p = FakeProvider("claude:haiku", SUMMARY)
    llm, _ = router(tmp_path, p)
    await summarize(llm, "Long-term investors believe in compounding.", title_hint="Compounding", language="auto")
    prompt, system = p.calls[-1]
    assert '"summary": a summary in English' in prompt and "Original title: Compounding" in prompt
    assert system.startswith("You help the user organize")
    await summarize(llm, "Long-term investors believe in compounding.", language="zh")  # summary_language = "zh"
    assert p.calls[-1] == (f"{INSTRUCTIONS}\n原标题：（无）\n\n内容：\nLong-term investors believe in compounding.", SYSTEM)


def test_summary_language_in_the_config(tmp_path):
    path = tmp_path / "config.toml"
    assert load_config(path).summary_language == "zh"  # unset: follows language (review p25b I2)
    path.write_text('summary_language = "zh"\n', encoding="utf-8")
    assert load_config(path).summary_language == "zh"
    path.write_text('summary_language = "fr"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="summary_language"):
        load_config(path)


async def test_the_pipeline_uses_the_configured_summary_language(tmp_path):
    from test_app import make_app

    app, ch, providers = make_app(tmp_path)
    app.pipeline.summary_language = "en"
    await app.pipeline.save_note("复利需要时间", "tg")
    assert '"summary": a summary in English' in providers["claude:haiku"].calls[-1][0]


# ---- answers ----

async def _answer(tmp_path, question, reply="Patience pays."):
    p = FakeProvider("claude:haiku", reply)
    llm, conn = router(tmp_path, p)
    ans = await qa.answer(question, Index(conn, FakeEmbedder()), llm, [("user", "hi"), ("assistant", "hello")])
    return ans, p.calls[-1]


async def test_an_english_question_gets_an_english_prompt(tmp_path):
    ans, (prompt, system) = await _answer(tmp_path, "Why does patience matter?")
    assert prompt.startswith("Recent conversation:\nUser: hi\nAssistant: hello\n\nMaterial:\n(none)\n\n"
                             "Question: Why does patience matter?\n\nRules: answer in English")
    assert system.startswith("You are Octopus") and "never claim" in system


async def test_a_chinese_question_gets_the_old_prompt(tmp_path):
    ans, (prompt, system) = await _answer(tmp_path, "复利有什么用？")
    assert prompt.startswith("最近的对话：\n用户：hi\n助手：hello\n\n资料：\n（无）\n\n问题：复利有什么用？\n\n要求：优先根据资料用中文回答")
    assert system == qa.SYSTEM


async def test_a_general_answer_in_english_cites_nothing(tmp_path):
    note = i18n.text_in("en", "prompt.qa.general_note")
    ans, _ = await _answer(tmp_path, "What is the capital of France?", f"{note}\nParis [1].")
    assert ans.text.startswith(note) and ans.sources == []


# ---- management requests ----

def test_an_english_request_gets_an_english_intent_prompt():
    from datetime import date

    prompt = manage.build_prompt("delete the one above", [], ("20261013-abc123", "Compounding"),
                                 [("20261013-abc123", "Compounding")], date(2026, 10, 13))
    assert prompt.startswith('Today is 2026-10-13.\nCurrent entry: 20261013-abc123 "Compounding"\n')
    assert "User says: delete the one above" in prompt and "- intent: answer" in prompt
    zh = manage.build_prompt("删掉上面这条", [], None, [], date(2026, 10, 13))
    assert zh == f"今天是 2026-10-13。\n当前条目：（无）\n最近保存：\n（无）\n最近的对话：\n（无）\n\n用户说：删掉上面这条\n\n{manage.INSTRUCTIONS}"
