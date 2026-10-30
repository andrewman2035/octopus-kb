import asyncio
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeEmbedder, FakeProvider, StubFetcher, fake_media, summary_json

from octopus.config import TaskModels
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.formatting import format_saved
from octopus.fetchers.base import FetchResult
from octopus.index import Index
from octopus.library import Library
from octopus.media import MediaError
from octopus.llm.router import LLMRouter
from octopus.models import Attachment, attachment_from_dict, attachment_to_dict
from octopus.pipeline import SavePipeline
from octopus.settings import Settings

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=CST)


def make(tmp_path, fetcher=None, media=None, respond=None, error=None):
    conn = connect(tmp_path / "p.db", embed_dim=FakeEmbedder.dim)
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    idx = Index(conn, FakeEmbedder())
    p = FakeProvider("claude:haiku", respond or summary_json(title="生成的标题"), error=error)
    tm = TaskModels("claude:haiku", None)
    llm = LLMRouter({p.name: p}, {"summarize": tm, "answer": tm}, Settings(conn))
    media = media or fake_media(tmp_path)
    pipe = SavePipeline(lib, idx, llm, FetcherRegistry([fetcher] if fetcher else []), clock=lambda: NOW, media=media)
    return pipe, lib, idx, media, p


def staged(tmp_path, name, data=b"x", kind="image", text=None, filename=None, user="u1"):
    d = tmp_path / "attachments" / "_incoming" / user
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return Attachment(kind=kind, path=d / name, filename=filename or name, text=text)


def entry_dirs(tmp_path):
    root = tmp_path / "attachments"
    return [p for p in root.iterdir() if p.name != "_incoming"] if root.exists() else []


def test_attachment_dict_round_trip(tmp_path):
    a = Attachment(kind="audio", path=tmp_path / "a.m4a", filename="a.m4a", mime="audio/mp4", text="语音", size=3)
    assert attachment_from_dict(attachment_to_dict(a)) == a
    assert isinstance(attachment_to_dict(a)["path"], str)


async def test_audio_result_is_transcribed_and_download_deleted(tmp_path):
    dl = tmp_path / "downloads"
    dl.mkdir()
    audio = dl / "ep.m4a"
    audio.write_bytes(b"a")
    fetcher = StubFetcher(FetchResult(url="https://b.com/v", source="bilibili", title="视频", text="简介",
                                      media_id="BV1", audio_path=audio))
    pipe, lib, _, media, _ = make(tmp_path, fetcher)
    out = await pipe.save_url("https://b.com/v", "cli")
    assert out.entry.body == "简介\n\n转写稿"
    assert (out.entry.status, out.entry.media_id, out.entry.language) == ("ok", "BV1", "zh")
    assert not audio.exists()
    assert media.calls == [("asr", str(audio))]


async def test_transcription_failure_is_partial(tmp_path):
    dl = tmp_path / "downloads"
    dl.mkdir()
    audio = dl / "ep.m4a"
    audio.write_bytes(b"a")
    fetcher = StubFetcher(FetchResult(url="https://b.com/v", source="bilibili", title="视频", audio_path=audio))
    pipe, *_ = make(tmp_path, fetcher, media=fake_media(tmp_path, fail="asr"))
    out = await pipe.save_url("https://b.com/v", "cli")
    assert out.entry.status == "needs_manual"
    assert "asr 失败" in out.note
    assert not audio.exists()


async def test_save_screenshots(tmp_path):
    pipe, lib, idx, media, _ = make(tmp_path)
    a1, a2 = staged(tmp_path, "1.png"), staged(tmp_path, "2.png")
    out = await pipe.save_attachments([a1, a2], "我的截图", "cli")
    e = out.entry
    assert e.source == "manual" and e.status == "ok" and e.title == "生成的标题"
    kept = tmp_path / "attachments" / e.id
    assert sorted(p.name for p in kept.iterdir()) == ["1.png", "2.png"]
    assert not a1.path.exists()
    assert e.body.startswith("我的截图\n\n### 图片 1（1.png）\n\n图片文字\n\n### 图片 2（2.png）\n\n图片文字")
    assert f"附件：attachments/{e.id}/1.png" in e.body
    assert [c[0] for c in media.calls] == ["ocr", "ocr"]
    assert idx.count() == 1


async def test_voice_with_platform_text_skips_asr(tmp_path):
    pipe, _, _, media, _ = make(tmp_path)
    out = await pipe.save_attachments([staged(tmp_path, "v.silk", kind="audio", text="微信转好的文字")], "", "clawbot")
    assert "微信转好的文字" in out.entry.body
    assert media.calls == []


async def test_files_pdf_text_and_unknown(tmp_path):
    pipe, _, _, media, _ = make(tmp_path)
    files = [staged(tmp_path, "a.pdf", kind="file"), staged(tmp_path, "b.txt", data="文本内容".encode(), kind="file"),
             staged(tmp_path, "c.zip", kind="file")]
    out = await pipe.save_attachments(files, "", "cli")
    e = out.entry
    assert e.source == "file"
    assert "PDF 文字" in e.body and "文本内容" in e.body
    assert "暂不支持读取这种文件" in e.body
    assert [c[0] for c in media.calls] == ["pdf"]


async def test_attachment_extraction_failure_is_partial(tmp_path):
    pipe, *_ = make(tmp_path, media=fake_media(tmp_path, fail="ocr"))
    out = await pipe.save_attachments([staged(tmp_path, "1.png")], "", "cli")
    assert out.entry.status == "partial"
    assert "ocr 失败" in out.note


async def test_attachments_restored_when_store_fails(tmp_path, monkeypatch):
    pipe, *_ = make(tmp_path)
    a = staged(tmp_path, "1.png")

    async def boom(*args, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pipe.index, "upsert", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        await pipe.save_attachments([a], "", "cli")
    assert a.path.exists()
    assert entry_dirs(tmp_path) == []


async def test_merge_into_needs_manual_entry(tmp_path):
    pipe, lib, idx, media, _ = make(tmp_path)
    first = await pipe.save_failed_url("https://www.reddit.com/r/x/comments/1", "cli", "没法自动获取")
    eid = first.entry.id
    out = await pipe.merge(eid, [staged(tmp_path, "shot.png")], "补充说明")
    e = out.entry
    assert out.merged and e.id == eid
    assert e.status == "ok" and e.title == "生成的标题"
    assert "## 补充内容（2026-10-13 09:30）" in e.body
    assert "补充说明" in e.body and "图片文字" in e.body
    assert (tmp_path / "attachments" / eid / "shot.png").exists()
    assert lib.get(eid).status == "ok"
    assert idx.count() == 1


async def test_merge_missing_entry(tmp_path):
    pipe, *_ = make(tmp_path)
    assert await pipe.merge("20990101-000000", [], "x") is None


async def test_refetch_updates_needs_manual_entry(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    pipe, lib, idx, _, _ = make(tmp_path, fetcher)
    first = await pipe.save_url("https://a.com/x", "cli")
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="正常文章", text="正文" * 200)
    out = await pipe.refetch(first.entry.id, "https://a.com/x")
    assert out.entry.id == first.entry.id and out.entry.status == "ok" and out.entry.title == "正常文章"
    assert lib.get(first.entry.id).body == "正文" * 200
    assert idx.count() == 1


async def test_refetch_still_failing_leaves_entry(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="还是不行"))
    pipe, lib, _, _, _ = make(tmp_path, fetcher)
    first = await pipe.save_url("https://a.com/x", "cli")
    out = await pipe.refetch(first.entry.id, "https://a.com/x")
    assert out.entry.status == "needs_manual" and out.note == "还是不行"


async def test_delete_waits_for_running_merge(tmp_path):
    gate = asyncio.Event()
    pipe, lib, idx, _, provider = make(tmp_path)
    first = await pipe.save_note("原始笔记", "cli")
    original_complete = provider.complete

    async def gated_complete(prompt, system):
        await gate.wait()
        return await original_complete(prompt, system)

    provider.complete = gated_complete
    merge_task = asyncio.create_task(pipe.merge(first.entry.id, [], "补充"))
    await asyncio.sleep(0.05)
    delete_task = asyncio.create_task(pipe.delete(first.entry.id))
    await asyncio.sleep(0.05)
    assert not delete_task.done()
    gate.set()
    await merge_task
    assert await delete_task is True
    assert lib.find(first.entry.id) is None and idx.count() == 0


async def test_unsafe_filenames_stay_inside_entry_dir(tmp_path):
    pipe, *_ = make(tmp_path)
    files = [staged(tmp_path, "a.png", filename="../../escaped.png"),
             staged(tmp_path, "b.png", filename="/abs/path.png"),
             staged(tmp_path, "c.png", filename="sub/2.png"),
             staged(tmp_path, "d.png", filename="..")]
    out = await pipe.save_attachments(files, "", "cli")
    kept = tmp_path / "attachments" / out.entry.id
    assert sorted(p.name for p in kept.iterdir()) == ["2.png", "escaped.png", "file4.png", "path.png"]
    assert not (tmp_path / "escaped.png").exists() and not (tmp_path / "attachments" / "escaped.png").exists()
    outside = [p for p in tmp_path.rglob("*.png") if kept not in p.parents]
    assert outside == []


async def test_filename_collision_gets_suffix(tmp_path):
    pipe, *_ = make(tmp_path)
    files = [staged(tmp_path, "a.png", user="u1"), staged(tmp_path, "a.png", user="u2")]
    out = await pipe.save_attachments(files, "", "cli")
    kept = tmp_path / "attachments" / out.entry.id
    assert sorted(p.name for p in kept.iterdir()) == ["a-1.png", "a.png"]


async def test_partial_move_is_rolled_back(tmp_path, monkeypatch):
    pipe, *_ = make(tmp_path)
    a1, a2 = staged(tmp_path, "1.png"), staged(tmp_path, "2.png")
    real_move = shutil.move
    calls = []

    def flaky_move(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk error")
        return real_move(src, dst)

    monkeypatch.setattr("octopus.pipeline.shutil.move", flaky_move)
    with pytest.raises(OSError, match="disk error"):
        await pipe.save_attachments([a1, a2], "", "cli")
    assert a1.path.exists() and a2.path.exists()
    assert entry_dirs(tmp_path) == []


async def test_reserved_id_makes_attachment_save_idempotent(tmp_path):
    pipe, lib, idx, media, provider = make(tmp_path)
    eid = pipe.reserve_entry_id()
    first = await pipe.save_attachments([staged(tmp_path, "1.png")], "", "cli", entry_id=eid)
    assert first.entry.id == eid
    calls_before = (len(media.calls), len(provider.calls))
    again = await pipe.save_attachments([Attachment(kind="image", path=tmp_path / "gone.png", filename="1.png")],
                                        "", "cli", entry_id=eid)
    assert again.entry.id == eid and not again.merged
    assert (len(media.calls), len(provider.calls)) == calls_before
    assert idx.count() == 1 and len(list(lib.iter_entries())) == 1


async def test_retry_uses_files_already_in_entry_dir(tmp_path):
    pipe, lib, idx, media, _ = make(tmp_path)
    eid = pipe.reserve_entry_id()
    kept = tmp_path / "attachments" / eid
    kept.mkdir(parents=True)
    (kept / "1.png").write_bytes(b"x")
    gone = Attachment(kind="image", path=tmp_path / "attachments" / "_incoming" / "u1" / "1.png", filename="1.png")
    out = await pipe.save_attachments([gone], "", "cli", entry_id=eid)
    assert out.entry.id == eid and "图片文字" in out.entry.body
    assert (kept / "1.png").exists()
    assert out.entry.status == "ok"


async def test_attachment_save_with_nothing_to_store_raises(tmp_path):
    pipe, lib, idx, *_ = make(tmp_path)
    eid = pipe.reserve_entry_id()
    gone = Attachment(kind="image", path=tmp_path / "nope.png", filename="nope.png")
    with pytest.raises(MediaError, match="附件文件不见了"):
        await pipe.save_attachments([gone], "", "cli", entry_id=eid)
    assert idx.count() == 0 and list(lib.iter_entries()) == []
    assert entry_dirs(tmp_path) == []


def test_reserve_entry_id_avoids_existing_attachment_dirs(tmp_path, monkeypatch):
    pipe, *_ = make(tmp_path)
    (tmp_path / "attachments" / "20261013-aaaaaa").mkdir(parents=True)
    ids = iter(["20261013-aaaaaa", "20261013-bbbbbb"])
    monkeypatch.setattr("octopus.pipeline.new_entry_id", lambda now: next(ids))
    assert pipe.reserve_entry_id() == "20261013-bbbbbb"


async def test_merge_failure_rolls_back_markdown_and_files(tmp_path, monkeypatch):
    pipe, lib, idx, media, _ = make(tmp_path)
    first = await pipe.save_failed_url("https://www.reddit.com/r/x/comments/1", "cli", "没法自动获取")
    eid = first.entry.id
    before = lib.find(eid).read_text(encoding="utf-8")
    shot = staged(tmp_path, "shot.png")
    real_upsert = pipe.index.upsert
    calls = []

    async def flaky(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("index down")
        return await real_upsert(*args, **kw)

    monkeypatch.setattr(pipe.index, "upsert", flaky)
    with pytest.raises(RuntimeError, match="index down"):
        await pipe.merge(eid, [shot], "补充说明")
    assert lib.find(eid).read_text(encoding="utf-8") == before
    assert shot.path.exists()
    assert entry_dirs(tmp_path) == []
    out = await pipe.merge(eid, [shot], "补充说明")
    assert out.entry.body.count("## 补充内容") == 1
    assert lib.get(eid).body.count("## 补充内容") == 1


async def test_merge_extraction_failure_marks_partial_and_shows_note(tmp_path):
    pipe, *_ = make(tmp_path, media=fake_media(tmp_path, fail="ocr"))
    first = await pipe.save_note("原始笔记", "cli")
    out = await pipe.merge(first.entry.id, [staged(tmp_path, "shot.png")], "补充")
    assert out.entry.status == "partial"
    assert "ocr 失败" in out.note
    assert "ocr 失败" in format_saved(out)


async def test_refetch_leaves_entry_that_is_no_longer_needs_manual(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", title="好文章", text="正文" * 200))
    pipe, lib, *_ = make(tmp_path, fetcher)
    first = await pipe.save_url("https://a.com/x", "cli")
    assert first.entry.status == "ok"
    out = await pipe.refetch(first.entry.id, "https://a.com/x")
    assert out.entry.id == first.entry.id and out.note is None
    assert fetcher.calls == ["https://a.com/x"]
    assert lib.get(first.entry.id).body == "正文" * 200


async def test_refetch_keeps_supplement_sections(tmp_path):
    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    pipe, lib, *_ = make(tmp_path, fetcher)
    first = await pipe.save_url("https://a.com/x", "cli")
    entry = lib.get(first.entry.id)
    entry.body = "## 补充内容（2026-10-13 09:00）\n\n用户补的话"
    lib.save(entry)
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="正常文章", text="新正文")
    out = await pipe.refetch(first.entry.id, "https://a.com/x")
    assert out.entry.body == "新正文\n\n## 补充内容（2026-10-13 09:00）\n\n用户补的话"
    assert lib.get(first.entry.id).body == out.entry.body


async def test_text_file_read_is_capped(tmp_path):
    pipe, *_ = make(tmp_path)
    big = staged(tmp_path, "big.txt", data=b"a" * (3 * 1024 * 1024), kind="file")
    out = await pipe.save_attachments([big], "", "cli")
    assert 2 * 1024 * 1024 <= len(out.entry.body) < 2 * 1024 * 1024 + 1000


async def test_unreadable_text_file_becomes_note(tmp_path):
    pipe, *_ = make(tmp_path)
    d = tmp_path / "attachments" / "_incoming" / "u1" / "dir.txt"
    d.mkdir(parents=True)
    out = await pipe.save_attachments([Attachment(kind="file", path=d, filename="dir.txt")], "说明", "cli")
    assert out.entry.status == "partial"
    assert "dir.txt" in out.note


async def test_filename_control_characters_are_replaced(tmp_path):
    pipe, *_ = make(tmp_path)
    files = [staged(tmp_path, "a.png", filename="evil\nname\t\x00.png")]
    out = await pipe.save_attachments(files, "", "cli")
    kept = tmp_path / "attachments" / out.entry.id
    assert [p.name for p in kept.iterdir()] == ["evil_name__.png"]
    md = next((tmp_path / "lib").rglob("*.md")).read_text(encoding="utf-8")
    assert f"附件：attachments/{out.entry.id}/evil_name__.png" in md


async def test_delete_moves_attachments_to_trash(tmp_path):
    pipe, lib, idx, _, _ = make(tmp_path)
    out = await pipe.save_attachments([staged(tmp_path, "a.png")], "", "cli")
    eid = out.entry.id
    assert (tmp_path / "attachments" / eid / "a.png").exists()
    assert await pipe.delete(eid) is True
    assert not (tmp_path / "attachments" / eid).exists()
    assert (tmp_path / "trash" / f"{eid}-attachments" / "a.png").exists()
    # a later retry cannot resurrect the entry from its files
    with pytest.raises(MediaError):
        await pipe.save_attachments([], "", "cli", entry_id=eid)
    assert lib.find(eid) is None


async def test_delete_entry_without_attachments_still_works(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    out = await pipe.save_note("纯文字", "cli")
    assert await pipe.delete(out.entry.id) is True
    assert not list((tmp_path / "trash").glob("*-attachments"))


async def test_long_filename_is_truncated_keeping_suffix(tmp_path):
    pipe, *_ = make(tmp_path)
    long_name = "很长的文件名" * 30 + ".png"  # 540+ UTF-8 bytes
    out = await pipe.save_attachments([staged(tmp_path, "a.png", filename=long_name)], "", "cli")
    kept = tmp_path / "attachments" / out.entry.id
    [name] = [p.name for p in kept.iterdir()]
    assert len(name.encode("utf-8")) <= 200
    assert name.endswith(".png") and name.startswith("很长的文件名")
    assert f"附件：attachments/{out.entry.id}/{name}" in out.entry.body


async def test_source_outside_incoming_dir_is_refused(tmp_path):
    pipe, lib, idx, *_ = make(tmp_path)
    elsewhere = tmp_path / "Documents" / "secret.png"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"x")
    with pytest.raises(MediaError):
        await pipe.save_attachments([Attachment(kind="image", path=elsewhere, filename="secret.png")], "", "cli")
    assert elsewhere.exists()
    assert entry_dirs(tmp_path) == [] and list(lib.iter_entries()) == []


async def test_empty_incoming_subdir_is_removed_after_move(tmp_path):
    pipe, *_ = make(tmp_path)
    a = staged(tmp_path, "1.png", user="0123abcd")
    await pipe.save_attachments([a], "", "cli")
    incoming = tmp_path / "attachments" / "_incoming"
    assert not (incoming / "0123abcd").exists()
    assert incoming.is_dir()


def pdf_download(tmp_path, name="paper.pdf"):
    dl = tmp_path / "downloads"
    dl.mkdir(exist_ok=True)
    doc = dl / "abc.pdf"
    doc.write_bytes(b"%PDF-1.4")
    return doc, StubFetcher(FetchResult(url="https://a.com/" + name, source="file", title=name, doc_path=doc))


async def test_pdf_download_is_extracted_under_heavy_lock_and_deleted(tmp_path):
    doc, fetcher = pdf_download(tmp_path)
    pipe, lib, _, media, _ = make(tmp_path, fetcher)
    await pipe._heavy.acquire()  # another heavy job (ASR/OCR) is running
    task = asyncio.create_task(pipe.save_url("https://a.com/paper.pdf", "cli"))
    await asyncio.sleep(0.05)
    assert media.calls == []  # waits for the heavy lock instead of running OCR in the fetcher thread
    pipe._heavy.release()
    out = await task
    assert media.calls == [("pdf", str(doc))]
    assert (out.entry.status, out.entry.title, out.entry.body) == ("ok", "paper.pdf", "PDF 文字")
    assert not doc.exists()


async def test_pdf_extraction_failure_needs_manual_and_cleans_up(tmp_path):
    doc, fetcher = pdf_download(tmp_path)
    pipe, *_ = make(tmp_path, fetcher, media=fake_media(tmp_path, fail="pdf"))
    out = await pipe.save_url("https://a.com/paper.pdf", "cli")
    assert out.entry.status == "needs_manual" and out.note == "pdf 失败"
    assert not doc.exists()


async def test_pdf_without_text_needs_manual(tmp_path):
    doc, fetcher = pdf_download(tmp_path, "scan.pdf")
    pipe, *_ = make(tmp_path, fetcher, media=fake_media(tmp_path, pdf="  \n"))
    out = await pipe.save_url("https://a.com/scan.pdf", "cli")
    assert out.entry.status == "needs_manual" and "没有识别出文字" in out.note
    assert not doc.exists()


async def test_merge_retry_picks_up_files_moved_by_killed_attempt(tmp_path):
    pipe, lib, idx, media, _ = make(tmp_path)
    first = await pipe.save_failed_url("https://www.reddit.com/r/x/comments/1", "cli", "没法自动获取")
    eid = first.entry.id
    # the first attempt moved the file, then the process died before the Markdown was saved
    kept = tmp_path / "attachments" / eid
    kept.mkdir(parents=True)
    (kept / "shot.png").write_bytes(b"x")
    gone = Attachment(kind="image", path=tmp_path / "attachments" / "_incoming" / "u1" / "shot.png", filename="shot.png")
    out = await pipe.merge(eid, [gone], "")
    assert out.merged and out.entry.status == "ok"
    assert "图片文字" in out.entry.body
    assert f"附件：attachments/{eid}/shot.png" in lib.get(eid).body
    assert out.entry.body.count("## 补充内容") == 1


async def test_merge_with_missing_files_and_no_text_raises(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    first = await pipe.save_failed_url("https://www.reddit.com/r/x/comments/1", "cli", "没法自动获取")
    eid = first.entry.id
    gone = Attachment(kind="image", path=tmp_path / "nope.png", filename="nope.png")
    with pytest.raises(MediaError, match="附件文件不见了"):
        await pipe.merge(eid, [gone], "  ")
    e = lib.get(eid)
    assert e.status == "needs_manual" and "补充内容" not in e.body


async def test_merge_retry_does_not_re_add_files_already_linked(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    saved = await pipe.save_attachments([staged(tmp_path, "a.png")], "", "cli")
    eid = saved.entry.id
    gone = Attachment(kind="image", path=tmp_path / "attachments" / "_incoming" / "u1" / "a.png", filename="a.png")
    with pytest.raises(MediaError, match="附件文件不见了"):
        await pipe.merge(eid, [gone], "")
    assert "补充内容" not in lib.get(eid).body


async def test_delete_is_not_blocked_by_refetch_network_fetch(tmp_path):
    import threading

    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    pipe, lib, idx, _, _ = make(tmp_path, fetcher)
    first = await pipe.save_url("https://a.com/x", "cli")
    eid = first.entry.id
    started, gate = threading.Event(), threading.Event()
    real_fetch = fetcher.fetch

    def gated_fetch(url):
        started.set()
        gate.wait(5)
        return real_fetch(url)

    fetcher.fetch = gated_fetch
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="好了", text="正文" * 200)
    refetch = asyncio.create_task(pipe.refetch(eid, "https://a.com/x"))
    try:
        await asyncio.to_thread(started.wait, 5)
        assert await asyncio.wait_for(pipe.delete(eid), timeout=1) is True  # not stuck behind the download
    finally:
        gate.set()
    assert await refetch is None  # deleted meanwhile: the fetched result is discarded
    assert lib.find(eid) is None and idx.count() == 0


async def test_refetch_discards_result_when_supplemented_during_fetch(tmp_path):
    import threading

    fetcher = StubFetcher(FetchResult(url="https://a.com/x", source="web", status="needs_manual", note="网络错误"))
    pipe, lib, *_ = make(tmp_path, fetcher)
    eid = (await pipe.save_url("https://a.com/x", "cli")).entry.id
    started, gate = threading.Event(), threading.Event()
    real_fetch = fetcher.fetch

    def gated_fetch(url):
        started.set()
        gate.wait(5)
        return real_fetch(url)

    fetcher.fetch = gated_fetch
    fetcher.result = FetchResult(url="https://a.com/x", source="web", title="抓到的", text="抓到的正文" * 100)
    refetch = asyncio.create_task(pipe.refetch(eid, "https://a.com/x"))
    try:
        await asyncio.to_thread(started.wait, 5)
        merged = await asyncio.wait_for(pipe.merge(eid, [], "我粘贴的正文"), timeout=1)
    finally:
        gate.set()
    out = await refetch
    assert out.entry.body == merged.entry.body and "抓到的正文" not in lib.get(eid).body
    assert "我粘贴的正文" in lib.get(eid).body


# ---- final review I3: a recording the platform already transcribed has no file (ClawBot voice) ----

def wechat_voice(text="微信转好的文字"):
    return Attachment(kind="audio", path=None, filename="voice.silk", text=text)


async def test_transcribed_voice_without_file_is_saved(tmp_path):
    pipe, lib, idx, media, _ = make(tmp_path)
    out = await pipe.save_attachments([wechat_voice()], "", "clawbot")
    e = out.entry
    assert e.body == "### 录音 1（voice.silk）\n\n微信转好的文字"  # no 附件： line: there is no file
    assert (e.status, e.source) == ("ok", "manual")
    assert media.calls == [] and entry_dirs(tmp_path) == []
    assert idx.count() == 1


async def test_transcribed_voice_saved_next_to_real_files(tmp_path):
    pipe, *_ = make(tmp_path)
    out = await pipe.save_attachments([staged(tmp_path, "1.png"), wechat_voice()], "", "clawbot")
    body = out.entry.body
    assert "### 图片 1（1.png）\n\n图片文字" in body and "### 录音 1（voice.silk）\n\n微信转好的文字" in body
    assert f"附件：attachments/{out.entry.id}/1.png" in body and "voice.silk" not in body.split("附件：", 1)[1]


async def test_transcribed_voice_merges_into_needs_manual_entry(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    first = await pipe.save_failed_url("https://www.reddit.com/r/x/comments/1", "clawbot", "没法自动获取")
    out = await pipe.merge(first.entry.id, [wechat_voice("口述的帖子内容")], "")
    assert out.merged and out.entry.status == "ok"
    assert "### 录音 1（voice.silk）\n\n口述的帖子内容" in lib.get(first.entry.id).body


def _post_images(tmp_path, n=2):
    folder = tmp_path / "downloads" / "wechat-abc"
    folder.mkdir(parents=True)
    paths = []
    for i in range(1, n + 1):
        (folder / f"{i:02d}.jpg").write_bytes(b"i%d" % i)
        paths.append(folder / f"{i:02d}.jpg")
    return folder, paths


def _post_fetcher(paths, text="说明文字", **kw):
    return StubFetcher(FetchResult(url="https://mp.weixin.qq.com/s/p", source="wechat_mp", title="图集",
                                   text=text, image_paths=paths, **kw))


async def test_picture_post_is_ocrd_and_attached(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, _, media, _ = make(tmp_path, _post_fetcher(paths))
    out = await pipe.save_url("https://mp.weixin.qq.com/s/p", "cli")
    e = out.entry
    assert e.status == "ok"
    assert e.body == (f"说明文字\n\n### 图片 1\n\n图片文字\n\n### 图片 2\n\n图片文字\n\n"
                      f"附件：attachments/{e.id}/01.jpg\n附件：attachments/{e.id}/02.jpg")
    assert [c[0] for c in media.calls] == ["ocr", "ocr"]
    assert sorted(p.name for p in (tmp_path / "attachments" / e.id).iterdir()) == ["01.jpg", "02.jpg"]
    assert not folder.exists()


async def test_picture_post_empty_ocr_and_one_failure(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, _, media, _ = make(tmp_path, _post_fetcher(paths))
    results = iter(["", MediaError("ocr 失败")])

    def ocr(path):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    media.ocr = ocr
    out = await pipe.save_url("https://mp.weixin.qq.com/s/p", "cli")
    assert out.entry.status == "partial"
    assert "第 2 张图片识别失败" in out.note
    assert "### 图片 1\n\n（没有识别出文字）" in out.entry.body
    assert out.entry.body.count("附件：") == 2  # the failed image is still kept


async def test_picture_post_all_ocr_failed_without_caption_needs_manual(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, *_ = make(tmp_path, _post_fetcher(paths, text=""), media=fake_media(tmp_path, fail="ocr"))
    out = await pipe.save_url("https://mp.weixin.qq.com/s/p", "cli")
    assert out.entry.status == "needs_manual"
    assert "ocr 失败" in out.note
    assert not folder.exists()


async def test_picture_post_removes_downloads_when_not_saved(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, idx, media, p = make(tmp_path, _post_fetcher(paths))

    async def boom(*a, **k):
        raise RuntimeError("index down")

    idx.upsert = boom
    with pytest.raises(RuntimeError):
        await pipe.save_url("https://mp.weixin.qq.com/s/p", "cli")
    assert not folder.exists()
    assert entry_dirs(tmp_path) == []


async def test_refetch_picture_post_attaches_images(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, *_ = make(tmp_path, _post_fetcher(paths))
    first = await pipe.save_failed_url("https://mp.weixin.qq.com/s/p", "cli", "稍后")
    out = await pipe.refetch(first.entry.id, "https://mp.weixin.qq.com/s/p")
    assert out.entry.status == "ok"
    assert f"附件：attachments/{first.entry.id}/01.jpg" in lib.get(first.entry.id).body
    assert not folder.exists()


async def _needs_manual_entry(tmp_path, paths):
    pipe, lib, idx, media, _ = make(tmp_path, _post_fetcher(paths))
    first = await pipe.save_failed_url("https://mp.weixin.qq.com/s/p", "cli", "稍后")
    return pipe, lib, idx, first.entry.id


async def test_refetch_index_failure_rolls_back_entry_and_images(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, idx, entry_id = await _needs_manual_entry(tmp_path, paths)
    before = lib.get(entry_id).body

    async def boom(*a, **k):
        raise RuntimeError("index down")

    idx.upsert = boom
    with pytest.raises(RuntimeError):
        await pipe.refetch(entry_id, "https://mp.weixin.qq.com/s/p")
    e = lib.get(entry_id)
    assert (e.status, e.body) == ("needs_manual", before)
    assert "附件：" not in e.body
    assert entry_dirs(tmp_path) == [] and not folder.exists()


async def test_refetch_entry_deleted_midway_leaves_no_images(tmp_path):
    folder, paths = _post_images(tmp_path)
    pipe, lib, idx, entry_id = await _needs_manual_entry(tmp_path, paths)
    real = pipe._summarize_into

    async def delete_then_summarize(*a, **k):
        lib.find(entry_id).unlink()
        return await real(*a, **k)

    pipe._summarize_into = delete_then_summarize
    assert await pipe.refetch(entry_id, "https://mp.weixin.qq.com/s/p") is None
    assert entry_dirs(tmp_path) == []


async def test_images_outside_downloads_are_rejected_and_kept(tmp_path):
    outside = tmp_path / "elsewhere" / "01.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    pipe, *_ = make(tmp_path, _post_fetcher([outside]), media=fake_media(tmp_path, ocr=""))
    with pytest.raises(MediaError):
        await pipe.save_url("https://mp.weixin.qq.com/s/p", "cli")
    assert outside.read_bytes() == b"x"
    assert entry_dirs(tmp_path) == []


def _post_videos(tmp_path, n=2):
    dl = tmp_path / "downloads"
    dl.mkdir(exist_ok=True)
    paths = []
    for i in range(1, n + 1):
        (dl / f"v{i}.mp4").write_bytes(b"v%d" % i)
        paths.append(dl / f"v{i}.mp4")
    return paths


def _video_post(paths, text="帖子正文", **kw):
    return StubFetcher(FetchResult(url="https://x.com/someone/status/1", source="x", text=text, language="zh",
                                   video_paths=paths, **kw))


async def test_post_with_one_video_text_then_transcript(tmp_path):
    paths = _post_videos(tmp_path, 1)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert (out.entry.status, out.note) == ("ok", None)
    assert out.entry.body == "帖子正文\n\n转写稿"
    assert media.calls == [("asr", str(paths[0]))]
    assert not paths[0].exists()


async def test_post_with_several_videos_transcribes_each_in_order(tmp_path):
    paths = _post_videos(tmp_path, 2)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))
    heard = iter(["第一段", "第二段"])
    calls = []

    def transcribe(path):
        calls.append(path.name)
        from octopus.media.asr import Transcript
        return Transcript(next(heard), "zh")

    media.transcribe = transcribe
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "ok"
    assert out.entry.body == "帖子正文\n\n### 视频 1\n\n第一段\n\n### 视频 2\n\n第二段"
    assert calls == ["v1.mp4", "v2.mp4"]
    assert not any(p.exists() for p in paths)


async def test_video_that_cannot_be_transcribed_keeps_the_text_as_partial(tmp_path):
    paths = _post_videos(tmp_path, 1)
    pipe, *_ = make(tmp_path, _video_post(paths), media=fake_media(tmp_path, fail="asr"))
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "partial"
    assert out.entry.body == "帖子正文"
    assert "视频没能读取" in out.note and "asr 失败" in out.note
    assert not paths[0].exists()


async def test_one_of_several_videos_failing_is_partial_and_marked(tmp_path):
    paths = _post_videos(tmp_path, 2)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))
    from octopus.media.asr import Transcript
    results = iter([MediaError("asr 失败"), Transcript("第二段", "zh")])

    def transcribe(path):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    media.transcribe = transcribe
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "partial"
    assert out.entry.body == "帖子正文\n\n### 视频 1\n\n（asr 失败）\n\n### 视频 2\n\n第二段"
    assert "视频没能读取" in out.note
    assert not any(p.exists() for p in paths)


async def test_video_failure_keeps_an_earlier_note(tmp_path):
    paths = _post_videos(tmp_path, 1)
    fetcher = _video_post(paths, status="partial", note="长推文")
    pipe, *_ = make(tmp_path, fetcher, media=fake_media(tmp_path, fail="asr"))
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "partial"
    assert out.note.startswith("长推文") and "视频没能读取" in out.note


async def test_videos_are_removed_when_transcription_is_cancelled(tmp_path):
    paths = _post_videos(tmp_path, 2)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))

    def transcribe(path):
        raise KeyboardInterrupt  # stands in for a cancel arriving while the first video is heard

    media.transcribe = transcribe
    with pytest.raises(KeyboardInterrupt):
        await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert not any(p.exists() for p in paths)


async def test_x_video_post_end_to_end_text_then_transcript(tmp_path):
    """P29: the X fetcher downloads the video into downloads/, the pipeline transcribes it after the post's text and
    removes it."""
    import httpx
    import respx

    from octopus.fetchers.x import XFetcher

    post = {"__typename": "Tweet", "text": "一段带视频的帖子", "lang": "zh", "user": {"name": "测试", "screen_name": "someone"},
            "mediaDetails": [{"type": "video", "video_info": {"variants": [
                {"bitrate": 256000, "content_type": "video/mp4", "url": "https://video.twimg.com/v/lo.mp4"}]}}]}
    pipe, lib, _, media, _ = make(tmp_path, XFetcher(tmp_path / "downloads", max_bytes=1000))
    with respx.mock:
        respx.get(url__startswith="https://cdn.syndication.twimg.com/").mock(return_value=httpx.Response(200, json=post))
        respx.get("https://video.twimg.com/v/lo.mp4").mock(return_value=httpx.Response(
            200, content=b"mp4", headers={"content-type": "video/mp4"}))
        out = await pipe.save_url("https://x.com/someone/status/1000", "cli")
    assert (out.entry.source, out.entry.status) == ("x", "ok")
    assert out.entry.body == "一段带视频的帖子\n\n转写稿"
    assert [c[0] for c in media.calls] == ["asr"] and media.calls[0][1].endswith(".mp4")
    assert list((tmp_path / "downloads").iterdir()) == []


# ---- P29 fix round 1 ----

async def test_video_headings_keep_the_posts_own_numbers(tmp_path):
    paths = _post_videos(tmp_path, 2)
    pipe, *_ = make(tmp_path, _video_post(paths, video_numbers=[2, 3], status="partial", note="视频没能下载：x"))
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.body == "帖子正文\n\n### 视频 2\n\n转写稿\n\n### 视频 3\n\n转写稿"
    assert out.entry.status == "partial"


async def test_the_one_video_left_of_several_keeps_its_heading(tmp_path):
    paths = _post_videos(tmp_path, 1)
    pipe, *_ = make(tmp_path, _video_post(paths, video_numbers=[2], status="partial", note="视频没能下载：x"))
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.body == "帖子正文\n\n### 视频 2\n\n转写稿"


async def test_a_video_without_speech_keeps_the_post_ok_with_a_short_note(tmp_path):
    from octopus.media import NoSpeechError

    paths = _post_videos(tmp_path, 1)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))

    def transcribe(path):
        raise NoSpeechError("没有识别出语音内容")

    media.transcribe = transcribe
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "ok"
    assert out.entry.body == "帖子正文\n\n（视频里没有讲话）"
    assert not paths[0].exists()


async def test_one_of_several_videos_without_speech_says_so_in_its_section(tmp_path):
    from octopus.media import NoSpeechError
    from octopus.media.asr import Transcript

    paths = _post_videos(tmp_path, 2)
    pipe, lib, _, media, _ = make(tmp_path, _video_post(paths))
    results = iter([NoSpeechError("没有识别出语音内容"), Transcript("第二段", "zh")])

    def transcribe(path):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    media.transcribe = transcribe
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "ok"
    assert out.entry.body == "帖子正文\n\n### 视频 1\n\n（视频里没有讲话）\n\n### 视频 2\n\n第二段"


async def test_videos_are_removed_without_speech_to_text(tmp_path):
    paths = _post_videos(tmp_path, 1)
    pipe, *_ = make(tmp_path, _video_post(paths))
    pipe.media = None
    out = await pipe.save_url("https://x.com/someone/status/1", "cli")
    assert out.entry.status == "partial" and out.entry.body == "帖子正文"
    assert not paths[0].exists()


async def test_a_cancelled_save_tells_the_fetch_thread_to_stop(tmp_path):
    import threading

    from octopus.fetchers.base import FETCH_CANCEL

    started, seen = threading.Event(), []

    class Slow:
        name = "x"

        def can_handle(self, url):
            return True

        def fetch(self, url):
            stop = FETCH_CANCEL.get()
            started.set()
            seen.append(stop.wait(5))  # the download loop would see this and stop
            raise RuntimeError("stopped")

    pipe, *_ = make(tmp_path, Slow())
    task = asyncio.create_task(pipe.save_url("https://x.com/someone/status/1", "cli"))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [True]
