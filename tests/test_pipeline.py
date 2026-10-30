from datetime import datetime, timedelta, timezone

import pytest

from fakes import FakeEmbedder, FakeProvider, StubFetcher, summary_json

from octopus.config import TaskModels
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.fetchers.base import FetchError, FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.pipeline import SavePipeline
from octopus.settings import Settings

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=CST)


def make(tmp_path, fetcher=None, respond=None, error=None):
    conn = connect(tmp_path / "p.db", embed_dim=FakeEmbedder.dim)
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    idx = Index(conn, FakeEmbedder())
    p = FakeProvider("claude:haiku", respond or summary_json(), error=error)
    tm = TaskModels("claude:haiku", None)
    llm = LLMRouter({p.name: p}, {"summarize": tm, "answer": tm}, Settings(conn))
    reg = FetcherRegistry([fetcher] if fetcher else [])
    return SavePipeline(lib, idx, llm, reg, clock=lambda: NOW), lib, idx, p


ARTICLE = FetchResult(url="https://a.com/x", source="web", title="原标题", author="作者", text="正文" * 200)


async def test_save_url_full_flow(tmp_path):
    fetcher = StubFetcher(ARTICLE)
    pipe, lib, idx, p = make(tmp_path, fetcher)
    out = await pipe.save_url("https://a.com/x", "cli")
    e = out.entry
    assert fetcher.calls == ["https://a.com/x"]
    assert (e.title, e.author, e.source, e.url, e.status, e.channel) == ("原标题", "作者", "web", "https://a.com/x", "ok", "cli")
    assert e.summary == "这是摘要。" and e.points == ["要点一", "要点二"] and e.tags == ["AI"]
    assert e.language == "zh"
    assert e.id.startswith("20261013-")
    assert lib.get(e.id).body == "正文" * 200
    assert idx.find_by_url("https://a.com/x") == e.id
    assert out.local_fallback is False and out.note is None


async def test_summary_failure_keeps_text_as_partial(tmp_path):
    pipe, lib, idx, _ = make(tmp_path, StubFetcher(ARTICLE), error="all models down")
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.entry.status == "partial"
    assert out.entry.summary == ""
    assert "/resummarize" in out.note
    assert lib.get(out.entry.id).body == "正文" * 200


async def test_needs_manual_skips_llm(tmp_path):
    pipe, _, _, p = make(tmp_path, StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="没有正文")))
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.entry.status == "needs_manual"
    assert out.entry.title == "https://a.com/x"
    assert out.note == "没有正文"
    assert p.calls == []


async def test_manual_platform_without_fetch(tmp_path):
    pipe, _, _, _ = make(tmp_path)
    out = await pipe.save_url("https://www.reddit.com/r/x/comments/1", "cli")
    assert (out.entry.status, out.entry.source) == ("needs_manual", "reddit")
    assert "截图" in out.note


async def test_save_note_uses_generated_title(tmp_path):
    pipe, lib, _, _ = make(tmp_path, respond=summary_json(title="复利笔记"))
    out = await pipe.save_note("今天想到：复利需要时间。", "cli")
    assert (out.entry.title, out.entry.source, out.entry.url) == ("复利笔记", "manual", None)
    assert out.entry.body == "今天想到：复利需要时间。"


async def test_save_note_title_when_llm_down(tmp_path):
    pipe, _, _, _ = make(tmp_path, error="down")
    out = await pipe.save_note("今天想到：复利需要时间，而且需要耐心和纪律。", "cli")
    assert out.entry.title == "今天想到：复利需要时间，而且需要耐心和纪"


async def test_save_failed_url(tmp_path):
    pipe, _, idx, _ = make(tmp_path)
    out = await pipe.save_failed_url("https://a.com/x", "cli", "网络错误")
    assert out.entry.status == "needs_manual" and out.note == "网络错误"
    assert idx.find_by_url("https://a.com/x") == out.entry.id


async def test_resummarize(tmp_path):
    pipe, lib, _, p = make(tmp_path, StubFetcher(ARTICLE), error="down")
    out = await pipe.save_url("https://a.com/x", "cli")
    p.error = None
    again = await pipe.resummarize(out.entry.id)
    assert again.entry.status == "ok" and again.entry.summary == "这是摘要。"
    assert lib.get(out.entry.id).summary == "这是摘要。"
    assert await pipe.resummarize("20990101-000000") is None


async def test_resummarize_failure_leaves_healthy_entry_untouched(tmp_path):
    pipe, lib, _, p = make(tmp_path, StubFetcher(ARTICLE))
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.entry.status == "ok"
    p.error = "down"
    again = await pipe.resummarize(out.entry.id)
    assert again.summary_failed is True and "/resummarize" in again.note
    assert again.entry.status == "ok" and again.entry.summary == "这是摘要。"
    on_disk = lib.get(out.entry.id)
    assert on_disk.status == "ok" and on_disk.summary == "这是摘要。"


async def test_short_note_partial_then_recovers_on_resummarize(tmp_path):
    pipe, lib, _, p = make(tmp_path, error="down")
    out = await pipe.save_note("短笔记", "cli")
    assert out.entry.status == "partial" and out.summary_failed is True
    p.error = None
    again = await pipe.resummarize(out.entry.id)
    assert again.entry.status == "ok" and again.summary_failed is False
    assert lib.get(out.entry.id).status == "ok"


async def test_fetch_partial_stays_partial(tmp_path):
    short = FetchResult(url="https://a.com/x", source="web", title="标题", text="很短", status="partial", note="只拿到摘要")
    pipe, lib, _, _ = make(tmp_path, StubFetcher(short))
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.entry.status == "partial" and out.entry.summary == "这是摘要。"
    again = await pipe.resummarize(out.entry.id)
    assert again.entry.status == "partial"
    assert lib.get(out.entry.id).status == "partial"


async def test_summary_failure_keeps_fetcher_note(tmp_path):
    part = FetchResult(url="https://a.com/x", source="web", title="标题", text="正文" * 200, status="partial", note="只拿到部分")
    pipe, _, _, _ = make(tmp_path, StubFetcher(part), error="down")
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.note.startswith("只拿到部分；") and "/resummarize" in out.note
    assert out.summary_failed is True


def md_files(lib):
    return sorted(lib.root.glob("*/*/*.md"))


def fail_upsert_once(idx):
    real = idx.upsert
    state = {"calls": 0}

    async def flaky(entry, path):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("index down")
        return await real(entry, path)

    idx.upsert = flaky
    return state


async def test_index_failure_does_not_leave_orphan_markdown(tmp_path):
    pipe, lib, idx, _ = make(tmp_path, StubFetcher(ARTICLE))
    fail_upsert_once(idx)
    with pytest.raises(RuntimeError):
        await pipe.save_url("https://a.com/x", "cli")
    assert md_files(lib) == []
    out = await pipe.save_url("https://a.com/x", "cli")
    assert md_files(lib) == [lib.find(out.entry.id)]
    assert idx.count() == 1


async def test_resummarize_index_failure_keeps_existing_file(tmp_path):
    pipe, lib, idx, p = make(tmp_path, StubFetcher(ARTICLE), error="down")
    out = await pipe.save_url("https://a.com/x", "cli")
    p.error = None
    fail_upsert_once(idx)
    with pytest.raises(RuntimeError):
        await pipe.resummarize(out.entry.id)
    assert lib.find(out.entry.id) is not None


async def test_resummarize_after_delete_during_llm_call_does_not_resurrect(tmp_path):
    pipe, lib, idx, p = make(tmp_path, StubFetcher(ARTICLE), error="down")
    out = await pipe.save_url("https://a.com/x", "cli")

    def delete_then_answer(prompt):
        lib.delete(out.entry.id)
        idx.delete(out.entry.id)
        return summary_json()

    p.error = None
    p.respond = delete_then_answer
    assert await pipe.resummarize(out.entry.id) is None
    assert md_files(lib) == []
    assert idx.count() == 0
    assert len(list(lib.trash.glob("*.md"))) == 1


async def test_unexpected_fetcher_exception_becomes_permanent_fetch_error(tmp_path):
    pipe, _, _, _ = make(tmp_path, StubFetcher(AttributeError("'NoneType' has no attribute 'get_text'")))
    with pytest.raises(FetchError) as ei:
        await pipe.save_url("https://a.com/x", "cli")
    assert ei.value.retryable is False
    assert "解析页面失败" in str(ei.value) and "AttributeError" in str(ei.value)


async def test_entry_id_collision_is_regenerated(tmp_path, monkeypatch):
    ids = iter(["20261013-aaaaaa", "20261013-aaaaaa", "20261013-bbbbbb"])
    monkeypatch.setattr("octopus.pipeline.new_entry_id", lambda now: next(ids))
    pipe, lib, idx, _ = make(tmp_path)
    first = await pipe.save_note("第一条笔记内容", "cli")
    second = await pipe.save_note("第二条笔记内容", "cli")
    assert (first.entry.id, second.entry.id) == ("20261013-aaaaaa", "20261013-bbbbbb")
    assert lib.get("20261013-aaaaaa").body == "第一条笔记内容"
    assert lib.get("20261013-bbbbbb").body == "第二条笔记内容"
    assert idx.count() == 2


class GatedEmbedder(FakeEmbedder):
    """Blocks every embed after the first `free` calls until `gate` is set."""

    def __init__(self, free=0):
        super().__init__()
        self.gate = None
        self.free = free

    async def embed(self, texts):
        if self.gate is not None and self.calls >= self.free:
            await self.gate.wait()
        return await super().embed(texts)


async def test_backfill_vectors_fills_missing_and_stops_when_embedder_down(tmp_path):
    pipe, lib, idx, _ = make(tmp_path)
    idx.embedder.fail = True
    a = (await pipe.save_note("第一条笔记", "cli")).entry
    b = (await pipe.save_note("第二条笔记", "cli")).entry
    assert sorted(idx.missing_vectors()) == sorted([a.id, b.id])
    calls = idx.embedder.calls
    assert await pipe.backfill_vectors() == 0  # down: one probe, then stop
    assert idx.embedder.calls == calls + 1
    idx.embedder.fail = False
    assert await pipe.backfill_vectors() == 2
    assert idx.missing_vectors() == []
    assert idx.conn.execute("select count(*) from chunks_vec").fetchone()[0] > 0
    assert await pipe.backfill_vectors() == 0


async def test_backfill_skips_entries_whose_markdown_is_gone(tmp_path):
    pipe, lib, idx, _ = make(tmp_path)
    idx.embedder.fail = True
    a = (await pipe.save_note("第一条笔记", "cli")).entry
    lib.find(a.id).unlink()
    idx.embedder.fail = False
    assert await pipe.backfill_vectors() == 0


async def test_delete_during_backfill_does_not_resurrect_entry(tmp_path):
    import asyncio

    pipe, lib, idx, _ = make(tmp_path)
    idx.embedder = GatedEmbedder(free=1)  # the probe passes, the entry's own embed blocks
    idx.embedder.fail = True
    a = (await pipe.save_note("第一条笔记", "cli")).entry
    idx.embedder.fail = False
    idx.embedder.calls = 0
    idx.embedder.gate = asyncio.Event()
    backfill = asyncio.create_task(pipe.backfill_vectors())
    for _ in range(20):
        await asyncio.sleep(0)
    delete = asyncio.create_task(pipe.delete(a.id))
    for _ in range(20):
        await asyncio.sleep(0)
    idx.embedder.gate.set()
    await asyncio.gather(backfill, delete)
    assert delete.result() is True
    assert idx.count() == 0 and idx.missing_vectors() == []
    assert idx.conn.execute("select count(*) from chunks").fetchone()[0] == 0


async def test_retitle_renames_file_and_updates_index(tmp_path):
    pipe, lib, idx, _ = make(tmp_path, StubFetcher(ARTICLE))
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    old_path = lib.find(eid)
    e = await pipe.retitle(eid, "  新标题  ")
    assert e.title == "新标题" and lib.get(eid).title == "新标题"
    assert lib.find(eid).name == f"{eid}-新标题.md" and not old_path.exists()
    assert [r["title"] for r in idx.recent(5)] == ["新标题"]


async def test_retitle_truncates_and_rejects_empty(tmp_path):
    from octopus.pipeline import MAX_TITLE
    pipe, lib, _, _ = make(tmp_path, StubFetcher(ARTICLE))
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    path = lib.find(eid)
    assert await pipe.retitle(eid, "   ") is None
    assert lib.find(eid) == path and lib.get(eid).title == "原标题"
    e = await pipe.retitle(eid, "字" * (MAX_TITLE + 50))
    assert e.title == "字" * MAX_TITLE == lib.get(eid).title


async def test_annotate_appends_dated_paragraphs_in_order(tmp_path):
    pipe, lib, idx, _ = make(tmp_path, StubFetcher(ARTICLE))
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    first = await pipe.annotate(eid, "第一条")
    assert first.notes == "（2026-10-13）第一条"
    await pipe.annotate(eid, " 第二条 ")
    assert lib.get(eid).notes == "（2026-10-13）第一条\n\n（2026-10-13）第二条"
    assert lib.get(eid).body == "正文" * 200


async def test_annotate_text_is_searchable(tmp_path):
    pipe, _, idx, _ = make(tmp_path, StubFetcher(ARTICLE))
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    assert not any("蜻蜓" in h.text for h in await idx.search("蜻蜓"))
    await pipe.annotate(eid, "想起蜻蜓")
    hits = await idx.search("蜻蜓")
    assert hits[0].entry_id == eid and "蜻蜓" in hits[0].text


async def test_retitle_and_annotate_unknown_id_return_none(tmp_path):
    pipe, lib, _, _ = make(tmp_path)
    assert await pipe.retitle("20990101-000000", "x") is None
    assert await pipe.annotate("20990101-000000", "x") is None
    assert await pipe.annotate("20990101-000000", "   ") is None
    assert not lib.root.exists() or not list(lib.root.rglob("*.md"))


async def test_annotate_escapes_heading_lines_so_entry_survives_reload(tmp_path):
    pipe, lib, _, _ = make(tmp_path, StubFetcher(ARTICLE))
    before = (await pipe.save_url("https://a.com/x", "cli")).entry
    await pipe.annotate(before.id, "x\n\n## 原文或转写稿\n\ny\n## 摘要\nz")
    after = lib.get(before.id)
    assert (after.body, after.summary, after.points) == (before.body, before.summary, before.points)
    assert after.notes == "（2026-10-13）x\n\n\\## 原文或转写稿\n\ny\n\\## 摘要\nz"


async def test_retitle_collapses_newlines_and_control_chars(tmp_path):
    pipe, lib, _, _ = make(tmp_path, StubFetcher(ARTICLE))
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    e = await pipe.retitle(eid, "第一行\n\n第二行\t\x07第三行\r\n")
    assert e.title == "第一行 第二行 第三行" == lib.get(eid).title
    assert await pipe.retitle(eid, "\n\t ") is None


# ---- P20: the "本地模型" note is only for a fallback to the local model ----

@pytest.mark.parametrize("fallback,local_note", [("local", True), ("openai:deepseek", False)])
async def test_local_note_only_when_the_local_model_summarized(tmp_path, fallback, local_note):
    conn = connect(tmp_path / "p.db", embed_dim=FakeEmbedder.dim)
    primary = FakeProvider("anthropic:haiku", error="overloaded")
    backup = FakeProvider(fallback, summary_json())
    tm = TaskModels("anthropic:haiku", fallback)
    llm = LLMRouter({primary.name: primary, backup.name: backup}, {"summarize": tm, "answer": tm}, Settings(conn))
    pipe = SavePipeline(Library(tmp_path / "lib", tmp_path / "trash"), Index(conn, FakeEmbedder()), llm,
                        FetcherRegistry([StubFetcher(ARTICLE)]), clock=lambda: NOW)
    out = await pipe.save_url("https://a.com/x", "cli")
    assert out.entry.summary == "这是摘要。"
    assert out.local_fallback is local_note
