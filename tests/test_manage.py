import json
from datetime import date

import pytest
from fakes import FakeProvider

from octopus import manage
from octopus.config import TaskModels
from octopus.db import connect
from octopus.llm.router import LLMRouter
from octopus.manage import Intent, detect, parse_intent, wants_manage
from octopus.settings import Settings

TODAY = date(2026, 10, 13)
FOCUS = ("20261013-aaaaaa", "复利笔记")
RECENT = [("20261013-aaaaaa", "复利笔记"), ("20261012-bbbbbb", "播客转写")]


@pytest.mark.parametrize("text", ["删掉上面这条", "20261013-905eeb 改个标题", "我上周存了什么", "帮我记一下明天开会"])
def test_gate_lets_management_words_through(text):
    assert wants_manage(text)


@pytest.mark.parametrize("text", ["什么是复利", "你好"])
def test_gate_keeps_ordinary_questions_out(text):
    assert not wants_manage(text)


def test_parse_full_intent():
    it = parse_intent(json.dumps({"intent": "retitle", "ids": ["20261013-aaaaaa"], "target": "focus",
                                  "query": "", "title": "新标题", "note": "", "content": "",
                                  "since": None, "until": None}, ensure_ascii=False))
    assert it == Intent("retitle", ["20261013-aaaaaa"], "focus", "", "新标题", "", "", None, None)


def test_parse_fenced_and_missing_keys():
    it = parse_intent('```json\n{"intent": "find", "query": "AI", "since": "2026-10-05"}\n```')
    assert it.kind == "find" and it.query == "AI" and it.since == "2026-10-05"
    assert it.ids == [] and it.target == "" and it.title == "" and it.until is None


def test_parse_bad_values_are_dropped():
    it = parse_intent(json.dumps({"intent": "delete", "ids": ["20261013-aaaaaa", "abc", 3], "target": "那条",
                                  "since": "上周", "until": "2026-13-40", "title": None}))
    assert it.ids == ["20261013-aaaaaa"]
    assert it.target == "" and it.since is None and it.until is None and it.title == ""


@pytest.mark.parametrize("text", ["不是 JSON", "[1, 2]", '"answer"', '{"intent": "explode"}', '{"ids": []}'])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        parse_intent(text)


def make_llm(tmp_path, primary, local="本地"):
    conn = connect(tmp_path / "m.db", embed_dim=8)
    providers = {"claude:haiku": FakeProvider("claude:haiku", primary), "local": FakeProvider("local", local)}
    return LLMRouter(providers, {"summarize": TaskModels("claude:haiku", "local")}, Settings(conn)), providers


def js(**kw):
    return json.dumps(kw, ensure_ascii=False)


async def test_detect_builds_prompt_and_returns_intent(tmp_path):
    llm, p = make_llm(tmp_path, js(intent="delete", ids=[], target="focus"))
    it = await detect(llm, "删掉上面这条", [("user", "复利是什么"), ("assistant", "复利是利滚利")], FOCUS, RECENT, TODAY)
    assert it.kind == "delete" and it.target == "focus"
    [(prompt, system)] = p["claude:haiku"].calls
    assert system == manage.SYSTEM
    assert "今天是 2026-10-13。" in prompt
    assert "当前条目：20261013-aaaaaa 「复利笔记」" in prompt
    assert "20261012-bbbbbb「播客转写」" in prompt
    assert "用户：复利是什么\n助手：复利是利滚利" in prompt
    assert "用户说：删掉上面这条" in prompt


async def test_detect_prompt_without_context(tmp_path):
    llm, p = make_llm(tmp_path, js(intent="answer"))
    it = await detect(llm, "找一下", [], None, [], TODAY)
    assert it.kind == "answer"
    prompt = p["claude:haiku"].calls[0][0]
    assert "当前条目：（无）" in prompt and "最近保存：\n（无）" in prompt and "最近的对话：\n（无）" in prompt


async def test_detect_fenced(tmp_path):
    llm, _ = make_llm(tmp_path, "```json\n" + js(intent="find", query="AI") + "\n```")
    assert (await detect(llm, "我存过哪些 AI 的", [], None, [], TODAY)).query == "AI"


async def test_detect_garbage_falls_back_to_local(tmp_path):
    llm, p = make_llm(tmp_path, "我觉得是删除", local=js(intent="delete", target="focus"))
    it = await detect(llm, "删掉这条", [], FOCUS, RECENT, TODAY)
    assert it.kind == "delete" and len(p["local"].calls) == 1


async def test_detect_all_models_fail(tmp_path):
    llm, _ = make_llm(tmp_path, "乱码", local="也是乱码")
    assert await detect(llm, "删掉这条", [], FOCUS, RECENT, TODAY) is None


async def test_explicit_ids_override_model_ids(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=["20261013-aaaaaa"]))
    it = await detect(llm, "删除 20261001-cccccc 和 20261002-dddddd", [], FOCUS, RECENT, TODAY)
    assert it.ids == ["20261001-cccccc", "20261002-dddddd"]


async def test_invented_ids_are_dropped(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=["20261013-aaaaaa", "20261012-bbbbbb", "20260101-ffffff"]))
    it = await detect(llm, "删掉这两条", [], FOCUS, RECENT[1:], TODAY)
    assert it.ids == ["20261013-aaaaaa", "20261012-bbbbbb"]  # focus and recent are known; the third is invented


async def test_gate_false_never_calls_the_model(tmp_path):
    llm, p = make_llm(tmp_path, js(intent="delete"))
    assert await detect(llm, "什么是复利", [], FOCUS, RECENT, TODAY) is None
    assert p["claude:haiku"].calls == [] and p["local"].calls == []


async def test_ids_listed_in_the_history_are_known(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=["20260901-cccccc"]))
    history = [("user", "我九月存了什么"), ("assistant", "找到 2 条：\n20260901-dddddd  甲\n20260901-cccccc  乙"),
               ("user", "说说 20260101-eeeeee")]
    it = await detect(llm, "删掉第二条", history, FOCUS, RECENT, TODAY)
    assert it.ids == ["20260901-cccccc"]
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=["20260101-eeeeee"]))  # only the bot's own listings count
    assert (await detect(llm, "删掉那条", history, FOCUS, RECENT, TODAY)).ids == []


@pytest.mark.parametrize("text", ["存一下明天给老王回电话", "存下这句话", "保存一下这个想法", "记住我的车停在 B2"])
def test_gate_catches_spoken_save_words(text):
    assert wants_manage(text)


async def test_detect_caps_the_text_in_the_prompt(tmp_path):
    llm, p = make_llm(tmp_path, js(intent="answer"))
    await detect(llm, "删" + "很" * 1000, [], None, [], TODAY)
    [(prompt, _)] = p["claude:haiku"].calls
    assert "用户说：删" + "很" * (manage.MAX_INTENT_TEXT - 1) + "\n" in prompt
    assert manage.MAX_INTENT_TEXT == 300


@pytest.mark.parametrize("text,tokens", [
    ("删掉 6e43", ["6e43"]), ("删掉6E43吧", ["6E43"]), ("add 一下这个", []), ("cafe 改个标题", []),
    ("20261013-6e43c1 改个标题", []), ("2026 年的 6e43", ["2026", "6e43"]), ("删 6e43c1f", []),
    ("删 xx6e43", []), ("6e43 和 a1b2c3", ["6e43", "a1b2c3"])])
def test_short_id_tokens(text, tokens):
    assert manage.short_id_tokens(text) == tokens


@pytest.mark.parametrize("text,tokens", [
    ("b305ad 讲了什么", [("b305ad", False)]), ("7c62d9 的要点", [("7c62d9", False)]), ("305ad 说了啥", [("305ad", False)]),
    ("我有100块钱怎么理财", []), ("2026年AI有什么趋势", []), ("第 731542 条", []), ("20261013-731542 讲了什么", []),
    ("B2B 模式有什么优缺点", []), ("e2e 测试怎么写", []), ("A100 显卡多少钱", []), ("C919 首飞", []),
    ("AE86 是什么车", []), ("维生素b12", []), ("abcdef 是什么", []),  # unmarked: 5-6 chars with a digit and a letter
    ("id 731542 讲了什么", [("731542", True)]), ("ID：731542", [("731542", True)]), ("编号: 123 的要点", [("123", True)]),
    ("记录号731542", [("731542", True)]), ("id e2e 讲了什么", [("e2e", True)]),
    ("记录 100 讲了啥", []), ("我记录 365 天体重，怎么分析", []),  # bare 记录 is the verb, not a mark
    ("covid 2019 是怎么来的", []), ("I paid 100 dollars, 值吗", []), ("android 123", [])])
def test_question_id_tokens(text, tokens):
    assert manage.question_id_tokens(text) == tokens


def test_short_id_passes_the_gate_only_when_it_names_an_entry():
    table = {"6e43": ("20261013-6e43c1", []), "a1b2": (None, ["20261013-a1b2c3", "20261012-ffa1b2"])}

    def resolve(t):
        return table.get(t, (None, []))

    assert wants_manage("6e43 改个标题叫 X", resolve) and wants_manage("a1b2 不要了", resolve)
    assert not wants_manage("2026年AI有什么趋势", resolve) and not wants_manage("add the cafe", resolve)
    assert not wants_manage("6e43 怎么样")  # without a lookup only the words count


@pytest.mark.parametrize("text", ["6e43 讲了什么", "id：20261015-b305ad 这篇文章讲了😧", "6e43 的要点",
                                  "20261015-b305ad 说的是啥", "a1b2 怎么样", "6e43？"])
def test_a_question_about_an_entry_is_not_a_management_request(text):
    table = {"6e43": ("20261013-6e43c1", []), "a1b2": (None, ["20261013-a1b2c3", "20261012-ffa1b2"])}
    assert not wants_manage(text, lambda t: table.get(t, (None, [])))


@pytest.mark.parametrize("text", ["删掉 6e43", "6e43 改标题为 X", "20261015-b305ad", "6e43 不要了",
                                  "6e43 讲了什么，删掉吧"])
def test_an_id_with_a_management_word_or_alone_still_asks_the_intent(text):
    assert wants_manage(text, lambda t: ("20261013-6e43c1", []) if t == "6e43" else (None, []))


async def test_detect_resolves_short_ids_over_the_models_ids(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=["20261013-aaaaaa"]))
    table = {"6e43": ("20261013-6e43c1", []), "a1b2": (None, ["20261013-a1b2c3", "20261012-ffa1b2"])}
    it = await detect(llm, "删掉 6e43 和 a1b2", [], None, RECENT, TODAY, lambda t: table.get(t, (None, [])))
    assert it.ids == ["20261013-6e43c1"] and it.ambiguous == [("a1b2", table["a1b2"][1])]


async def test_detect_reports_ambiguous_tokens_when_nothing_resolved(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=[]))
    ids = ["20261013-a1b2c3", "20261012-ffa1b2"]
    it = await detect(llm, "删掉 a1b2", [], None, RECENT, TODAY, lambda t: (None, ids))
    assert it.ids == [] and it.ambiguous == [("a1b2", ids)]


async def test_detect_without_resolve_ignores_short_ids(tmp_path):
    llm, _ = make_llm(tmp_path, js(intent="delete", ids=[], target="focus"))
    it = await detect(llm, "删掉 6e43", [], FOCUS, RECENT, TODAY)
    assert it.ids == [] and it.ambiguous == []
