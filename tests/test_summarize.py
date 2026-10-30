import pytest
from fakes import FakeProvider

from octopus.config import TaskModels
from octopus.db import connect
from octopus.llm.base import LLMError
from octopus.llm.router import LLMRouter
from octopus.settings import Settings
from octopus.summarize import MAX_CHARS, parse_json_object, summarize


def router(tmp_path, provider):
    s = Settings(connect(tmp_path / "s.db", embed_dim=8))
    tm = TaskModels(provider.name, None)
    return LLMRouter({provider.name: provider}, {"summarize": tm, "answer": tm}, s)


GOOD = '```json\n{"title": "复利", "summary": "讲复利。", "points": ["长期", "坚持"], "tags": ["投资"], "language": "zh"}\n```'


async def test_summarize_parses_fenced_json(tmp_path):
    p = FakeProvider("claude:haiku", GOOD)
    s = await summarize(router(tmp_path, p), "正文" * 10, title_hint="原标题")
    assert (s.title, s.summary, s.points, s.tags, s.language) == ("复利", "讲复利。", ["长期", "坚持"], ["投资"], "zh")
    assert (s.provider, s.fell_back) == ("claude:haiku", False)
    prompt, system = p.calls[0]
    assert "原标题" in prompt and "正文正文" in prompt
    assert "JSON" in system


async def test_summarize_truncates_long_text(tmp_path):
    p = FakeProvider("local", GOOD)
    await summarize(router(tmp_path, p), "长" * (MAX_CHARS + 500))
    assert p.calls[0][0].count("长") == MAX_CHARS


async def test_summarize_bad_output(tmp_path):
    with pytest.raises(LLMError):
        await summarize(router(tmp_path, FakeProvider("local", "抱歉我不能")), "正文")


def test_parse_json_object_tolerates_prose():
    assert parse_json_object('好的：\n{"a": 1}\n以上') == {"a": 1}
    with pytest.raises(LLMError):
        parse_json_object("[1, 2]")


def router2(tmp_path, primary, fallback=None):
    s = Settings(connect(tmp_path / "s2.db", embed_dim=8))
    tm = TaskModels(primary.name, fallback.name if fallback else None)
    provs = {p.name: p for p in (primary, fallback) if p}
    return LLMRouter(provs, {"summarize": tm, "answer": tm}, s)


async def test_summarize_string_points_and_tags(tmp_path):
    out = '{"title": "t", "summary": "s", "points": "一\\n二", "tags": "#投资, AI、基金"}'
    s = await summarize(router(tmp_path, FakeProvider("local", out)), "正文")
    assert s.points == ["一", "二"]
    assert s.tags == ["投资", "AI", "基金"]


async def test_summarize_ignores_other_types(tmp_path):
    out = '{"title": "t", "summary": "s", "points": 5, "tags": null}'
    s = await summarize(router(tmp_path, FakeProvider("local", out)), "正文")
    assert s.points == [] and s.tags == []


async def test_summarize_falls_back_on_bad_json(tmp_path):
    primary = FakeProvider("claude:haiku", "抱歉，我没法总结")
    fallback = FakeProvider("local", GOOD)
    s = await summarize(router2(tmp_path, primary, fallback), "正文")
    assert (s.title, s.provider, s.fell_back) == ("复利", "local", True)


async def test_summarize_requires_summary_field(tmp_path):
    with pytest.raises(LLMError):
        await summarize(router(tmp_path, FakeProvider("local", '{"foo": 1}')), "正文")


async def test_summarize_flattens_newlines_inside_points(tmp_path):
    out = '{"title": "t", "summary": "s", "points": ["第一行\\n第二行", "  单行  "], "tags": ["a\\nb"]}'
    s = await summarize(router(tmp_path, FakeProvider("local", out)), "正文")
    assert s.points == ["第一行 第二行", "单行"]
    assert s.tags == ["a b"]
