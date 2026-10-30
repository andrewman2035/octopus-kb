"""P18: edits made outside Octopus (in Obsidian) are picked up by scanning the library folder."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from fakes import FakeEmbedder, FakeProvider, fake_media, summary_json

from octopus.config import TaskModels
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.index import Index
from octopus.library import Library, render
from octopus.llm.router import LLMRouter
from octopus.pipeline import SavePipeline
from octopus.settings import Settings
from octopus.sync import CONFIRM_DELETE_SECONDS, LibrarySync, claim_library

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 13, 9, 30, tzinfo=CST)


def make(tmp_path, respond=None):
    conn = connect(tmp_path / "p.db", embed_dim=FakeEmbedder.dim)
    lib = Library(tmp_path / "lib", tmp_path / "trash", tmp_path / "lib" / "attachments")
    emb = FakeEmbedder()
    idx = Index(conn, emb)
    p = FakeProvider("claude:haiku", respond or summary_json(title="生成的标题"))
    tm = TaskModels("claude:haiku", None)
    llm = LLMRouter({p.name: p}, {"summarize": tm, "answer": tm}, Settings(conn))
    media = fake_media(tmp_path)
    media.attachments_dir = tmp_path / "lib" / "attachments"
    pipe = SavePipeline(lib, idx, llm, FetcherRegistry([]), clock=lambda: NOW, media=media)
    claim_library(lib.root, conn)  # what `octopus init` (or the first start) does
    return pipe, LibrarySync(pipe, clock=Clock()), lib, idx, emb


class Clock:
    """Real time, plus however far a test moved it on."""

    def __init__(self):
        self.ahead = 0.0

    def __call__(self) -> float:
        return time.time() + self.ahead


async def twice(sync, **kw):
    """Two passes far enough apart for a missing file to count as deleted."""
    first = await sync.scan(**kw)
    sync.clock.ahead += CONFIRM_DELETE_SECONDS
    second = await sync.scan(**kw)
    return first, second


def edit(path, old, new, age=60):
    """Change a file the way an editor would, finished `age` seconds ago."""
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))


async def saved(pipe, text="复利需要时间。"):
    out = await pipe.save_note(text, "cli")
    return out.entry.id, pipe.library.find(out.entry.id)


async def test_own_writes_are_not_picked_up_as_edits(tmp_path):
    pipe, sync, _, idx, emb = make(tmp_path)
    await saved(pipe)
    calls = emb.calls
    report = await sync.scan(settle=0)
    assert report.updated == [] and report.moved == [] and report.deleted == []
    assert emb.calls == calls
    assert (await sync.scan(settle=0)).updated == []


async def test_a_body_edit_is_indexed_for_search_and_questions(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "复利需要时间。", "复利需要时间。还有耐心和长期主义。")
    report = await sync.scan(settle=0)
    assert report.updated == [eid]
    hits = await idx.search("长期主义", keyword_only=True)
    assert [h.entry_id for h in hits] == [eid] and "长期主义" in hits[0].text
    assert (await sync.scan(settle=0)).updated == []  # once


async def test_a_title_edit_in_frontmatter_or_first_heading_is_used(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "title: 生成的标题", "title: 我的标题")
    await sync.scan(settle=0)
    assert idx.titles() == [(eid, "我的标题")]
    edit(path, "\n## 摘要\n", "\n# 标题行\n\n## 摘要\n")
    await sync.scan(settle=0)
    assert idx.titles() == [(eid, "标题行")]


async def test_a_file_still_being_edited_waits_for_the_next_pass(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "复利需要时间。", "正在打字", age=0)
    assert (await sync.scan()).updated == []
    os.utime(path, (time.time() - 10, time.time() - 10))
    assert (await sync.scan()).updated == [eid]


async def test_a_copy_made_in_obsidian_never_takes_over_or_loses_the_original(tmp_path, caplog):
    """sim_dup: Obsidian's "Make a copy" (<name> 1.md, same id), the original edited, a restart, then a comment (备注)."""
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, orig = await saved(pipe, "原文 ORIGINAL")
    copy = orig.with_name(f"{orig.stem} 1.md")  # sorts before the original
    copy.write_text(orig.read_text(encoding="utf-8"), encoding="utf-8")
    edit(orig, "ORIGINAL", "ORIGINAL + 我改的", age=60)
    restarted = Library(lib.root, lib.trash, lib.attachments)
    pipe2 = SavePipeline(restarted, idx, pipe.llm, FetcherRegistry([]), clock=lambda: NOW, media=pipe.media)
    sync2 = LibrarySync(pipe2, clock=Clock())
    assert restarted.find(eid) == orig
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        await sync2.scan(settle=0)
    assert f"{copy} is a copy of {orig}" in caplog.text
    await pipe2.annotate(eid, "备注")
    assert "ORIGINAL + 我改的" in orig.read_text(encoding="utf-8") and "备注" in orig.read_text(encoding="utf-8")
    assert copy.exists() and "我改的" not in copy.read_text(encoding="utf-8") and "备注" not in copy.read_text(
        encoding="utf-8")
    hits = await idx.search("我改的", keyword_only=True)
    assert {h.entry_id for h in hits} == {eid}
    orig.unlink()  # the original deleted: its one copy becomes the entry, nothing is deleted
    await sync2.scan(settle=0)
    assert restarted.find(eid) == copy and idx.count() == 1


async def test_several_copies_and_no_original_adopt_none_and_keep_the_index(tmp_path, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, orig = await saved(pipe)
    text = orig.read_text(encoding="utf-8")
    for name in ("a.md", "b.md"):
        (tmp_path / "lib" / name).write_text(text, encoding="utf-8")
    orig.unlink()
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        _, second = await twice(sync, settle=0)
    assert second.deleted == [] and second.moved == [] and idx.count() == 1
    assert lib.find(eid) is None and "2 files carry the id" in caplog.text
    await pipe.annotate(eid, "备注")
    assert all("备注" not in (tmp_path / "lib" / n).read_text(encoding="utf-8") for n in ("a.md", "b.md"))


async def test_a_renamed_file_keeps_its_entry_without_reembedding(tmp_path):
    pipe, sync, lib, idx, emb = make(tmp_path)
    eid, path = await saved(pipe)
    target = tmp_path / "lib" / "读书笔记" / "复利.md"
    target.parent.mkdir()
    path.rename(target)
    calls = emb.calls
    assert lib.find(eid) is None and lib.missed  # until the next pass (the worker starts one soon)
    report = await sync.scan(settle=0)
    assert report.moved == [eid] and report.updated == [] and report.deleted == []
    assert emb.calls == calls
    assert idx.conn.execute("SELECT path FROM entries WHERE id = ?", (eid,)).fetchone()[0] == str(target)
    await pipe.annotate(eid, "备注")
    assert target.exists() and lib.find(eid) == target  # not renamed back
    same_folder = path.with_name(f"{eid}-我起的名字.md")  # the id kept in front: still the user's name
    target.rename(same_folder)
    await sync.scan(settle=0)
    await pipe.retitle(eid, "新标题")
    assert same_folder.exists() and lib.find(eid) == same_folder


async def test_a_file_deleted_outside_leaves_the_index_after_two_passes_and_its_files_stay(tmp_path, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    files = tmp_path / "lib" / "attachments" / eid
    files.mkdir(parents=True)
    (files / "a.png").write_bytes(b"x")
    path.unlink()
    with caplog.at_level(logging.INFO, logger="octopus.sync"):
        first = await sync.scan(settle=0)
        assert first.deleted == [] and idx.count() == 1  # once is not enough
        sync.clock.ahead += CONFIRM_DELETE_SECONDS - 1
        assert (await sync.scan(settle=0)).deleted == []  # nor twice within a minute
        sync.clock.ahead += 1
        assert (await sync.scan(settle=0)).deleted == [eid] and idx.count() == 0
    assert eid in caplog.text
    assert (files / "a.png").exists() and not (tmp_path / "trash").exists()  # attachments stay in the vault
    assert (await sync.scan(settle=0)).deleted == [] and lib.find(eid) is None


async def test_a_file_back_before_the_second_pass_is_not_dropped(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    text = path.read_text(encoding="utf-8")
    path.unlink()
    await sync.scan(settle=0)
    path.write_text(text, encoding="utf-8")  # a save that deletes and writes again (vim, git checkout)
    sync.clock.ahead += CONFIRM_DELETE_SECONDS
    assert (await sync.scan(settle=0)).deleted == [] and idx.count() == 1
    path.unlink()
    sync.clock.ahead += CONFIRM_DELETE_SECONDS
    assert (await sync.scan(settle=0)).deleted == []  # the clock starts again


async def test_restored_from_obsidians_trash_finds_its_attachments_again(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    files = tmp_path / "lib" / "attachments" / eid
    files.mkdir(parents=True)
    (files / "a.png").write_bytes(b"x")
    (tmp_path / "lib" / ".trash").mkdir()
    trashed = tmp_path / "lib" / ".trash" / path.name
    path.rename(trashed)
    _, second = await twice(sync, settle=0)
    assert second.deleted == [eid]
    trashed.rename(path)  # put back
    assert (await sync.scan(settle=0)).updated == [eid] and idx.count() == 1
    assert (files / "a.png").exists()


async def test_an_icloud_placeholder_is_not_a_delete(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    path.rename(path.with_name(f".{path.name}.icloud"))  # evicted by iCloud (macOS 13 and earlier)
    _, second = await twice(sync, settle=0)
    assert second.deleted == [] and idx.count() == 1


async def test_many_notes_vanishing_at_once_drops_none_and_tells_the_owner_once(tmp_path, caplog):
    told = []

    async def notify(text):
        told.append(text)

    pipe, sync, lib, idx, _ = make(tmp_path)
    sync.notify = notify
    paths = [(await saved(pipe, f"笔记{i}"))[1] for i in range(6)]
    texts = [p.read_text(encoding="utf-8") for p in paths]
    for p in paths:
        p.unlink()
    with caplog.at_level(logging.WARNING, logger="octopus.sync"):
        _, second = await twice(sync, settle=0)
        sync.clock.ahead += CONFIRM_DELETE_SECONDS
        await sync.scan(settle=0)
    assert second.deleted == [] and idx.count() == 6
    assert len(told) == 1 and "6 条" in told[0] and "octopus library sync --apply-deletes" in told[0]
    for p, text in zip(paths, texts):  # they come back: nothing held any more
        p.write_text(text, encoding="utf-8")
    await sync.scan(settle=0)
    assert sync._missing == {}


async def test_one_of_a_few_notes_may_be_deleted_but_two_of_ten_may_not(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    paths = [(await saved(pipe, f"笔记{i}"))[1] for i in range(3)]
    paths[0].unlink()
    assert (await twice(sync, settle=0))[1].deleted != []
    more = [(await saved(pipe, f"另{i}"))[1] for i in range(7)]  # 9 entries now
    for p in more[:2]:
        p.unlink()
    assert (await twice(sync, settle=0))[1].deleted == []  # 2 of 9 is more than 10 %


async def test_a_library_folder_without_its_marker_is_left_alone(tmp_path):
    told = []

    async def notify(text):
        told.append(text)

    pipe, sync, lib, idx, _ = make(tmp_path)
    sync.notify = notify
    for i in range(3):
        await saved(pipe, f"笔记{i}")
    (tmp_path / "lib").rename(tmp_path / "renamed-in-obsidian")
    first, second = await twice(sync, settle=0)
    assert first.skipped and second.deleted == []
    await pipe.save_note("新的一条", "cli")  # a new save makes the folder again, but never the marker
    assert not (tmp_path / "lib" / ".octopus-library").exists()
    _, second = await twice(sync, settle=0)
    assert second.skipped and second.deleted == [] and idx.count() == 4
    assert len(told) == 1 and "笔记文件夹找不到了" in told[0]


async def test_the_first_start_of_an_empty_library_marks_its_folder(tmp_path):
    conn = connect(tmp_path / "p.db", embed_dim=FakeEmbedder.dim)
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    p = FakeProvider("claude:haiku", summary_json())
    tm = TaskModels("claude:haiku", None)
    pipe = SavePipeline(lib, Index(conn, FakeEmbedder()),
                        LLMRouter({p.name: p}, {"summarize": tm, "answer": tm}, Settings(conn)), FetcherRegistry([]))
    report = await LibrarySync(pipe).scan(settle=0)
    assert not report.skipped and (tmp_path / "lib" / ".octopus-library").exists()


async def test_another_librarys_marker_is_not_ours(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    await saved(pipe)
    (tmp_path / "lib" / ".octopus-library").write_text("someone-else\n", encoding="utf-8")
    assert (await sync.scan(settle=0)).skipped


def failing_reads(monkeypatch, failing):
    real = Library.read_id

    def read_id(p):
        if p in failing:
            raise OSError("offline")
        return real(p)

    monkeypatch.setattr(Library, "read_id", staticmethod(read_id))


async def test_a_known_file_that_cannot_be_read_now_keeps_its_entry(tmp_path, monkeypatch):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    renamed = tmp_path / "lib" / "我的笔记.md"  # no id in the name: only its frontmatter says whose it is
    path.rename(renamed)
    assert (await sync.scan(settle=0)).moved == [eid]
    edit(renamed, "复利需要时间。", "改过")
    failing = {renamed}
    failing_reads(monkeypatch, failing)
    _, second = await twice(sync, settle=0)
    assert second.deleted == [] and idx.count() == 1  # its id is known from before: present
    failing.clear()
    assert (await sync.scan(settle=0)).updated == [eid]


async def test_an_unreadable_file_never_stops_other_deletes(tmp_path, monkeypatch, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    keep, kept_path = await saved(pipe, "留着的")
    gone, gone_path = await saved(pipe, "删掉的")
    (tmp_path / "lib" / "我的GBK笔记.md").write_bytes("中文笔记".encode("gbk"))  # the user's own, not UTF-8
    locked = tmp_path / "lib" / "锁住的.md"
    locked.write_text("别人的", encoding="utf-8")
    failing_reads(monkeypatch, {locked, kept_path})  # kept_path: an entry's own file, unreadable now
    gone_path.unlink()
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        _, second = await twice(sync, settle=0)
        await sync.scan(settle=0)
    assert second.deleted == [gone] and idx.paths().keys() == {keep}
    assert caplog.text.count(str(locked)) == 1  # once per file version
    assert await sync.held_deletes() == []


async def test_reindex_drops_what_is_gone_even_with_an_unreadable_file(tmp_path, monkeypatch):
    pipe, sync, lib, idx, _ = make(tmp_path)
    keep, _ = await saved(pipe, "留着的")
    gone, gone_path = await saved(pipe, "删掉的")
    locked = tmp_path / "lib" / "锁住的.md"
    locked.write_text("别人的", encoding="utf-8")
    failing_reads(monkeypatch, {locked})
    gone_path.unlink()
    await idx.rebuild(lib)
    assert set(idx.paths()) == {keep}


async def test_an_unchanged_library_after_a_restart_costs_no_embedding(tmp_path):
    pipe, sync, lib, idx, emb = make(tmp_path)
    for i in range(3):
        await saved(pipe, f"笔记{i}")
    restarted = Library(lib.root, lib.trash, lib.attachments)
    pipe2 = SavePipeline(restarted, idx, pipe.llm, FetcherRegistry([]), media=pipe.media)
    calls = emb.calls
    report = await LibrarySync(pipe2).scan(settle=0)
    assert (report.updated, report.moved, report.deleted, emb.calls) == ([], [], [], calls)


async def test_the_scan_reads_files_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "复利需要时间。", "改过")
    threads = []
    real_walk, real_load = Library.paths, Library.load

    def walk(self, *a):
        threads.append(threading.current_thread())
        return real_walk(self, *a)

    def load(self, p):
        threads.append(threading.current_thread())
        return real_load(self, p)

    monkeypatch.setattr(Library, "paths", walk)
    monkeypatch.setattr(Library, "load", load)
    assert (await sync.scan(settle=0)).updated == [eid]
    assert len(threads) == 2 and threading.main_thread() not in threads


async def test_a_new_save_never_walks_the_library(tmp_path, monkeypatch):
    pipe, sync, lib, idx, _ = make(tmp_path)
    await saved(pipe)

    def no_walk(*a, **k):
        raise AssertionError("walked the library")

    monkeypatch.setattr(Library, "paths", no_walk)
    eid, path = await saved(pipe, "又一条")
    assert path.exists()
    att = await pipe.save_note("第三条", "cli")
    assert lib.find(att.entry.id) is not None


async def test_the_users_own_notes_are_left_alone(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    await saved(pipe)
    own = tmp_path / "lib" / "日记.md"
    own.write_text("---\ntags: [日记]\n---\n\n今天\n", encoding="utf-8")
    os.utime(own, (time.time() - 60, time.time() - 60))
    report = await sync.scan(settle=0)
    assert report.updated == [] and idx.count() == 1
    assert own.read_text(encoding="utf-8") == "---\ntags: [日记]\n---\n\n今天\n"


async def test_a_new_file_with_an_octopus_id_is_indexed(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    other = "20261012-0f0f0f"
    text = path.read_text(encoding="utf-8").replace(eid, other).replace("复利需要时间。", "另一条笔记")
    (tmp_path / "lib" / "另一条.md").write_text(text, encoding="utf-8")
    report = await sync.scan(settle=0)
    assert report.updated == [other] and idx.count() == 2


async def test_broken_frontmatter_keeps_the_old_index_and_warns_once_per_version(tmp_path, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "tags: [AI]", "tags: [AI")
    with caplog.at_level(logging.WARNING):
        assert (await sync.scan(settle=0)).updated == []
        await sync.scan(settle=0)
    assert caplog.text.count(str(path)) == 1
    assert idx.titles() == [(eid, "生成的标题")]
    edit(path, "tags: [AI", "tags: [AI, ")  # still broken, another version: one more warning
    with caplog.at_level(logging.WARNING):
        await sync.scan(settle=0)
    assert caplog.text.count(str(path)) == 2
    assert (await sync.scan(settle=0)).deleted == []  # broken is not deleted


async def test_an_external_edit_then_a_supplement_keeps_both(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "复利需要时间。", "复利需要时间。（我在 Obsidian 里加的）")
    edit(path, "tags: [AI]", "tags: [AI]\naliases: [复利]")
    out = await pipe.merge(eid, [], "补充的一段话")
    text = path.read_text(encoding="utf-8")
    assert "（我在 Obsidian 里加的）" in text and "补充的一段话" in text and "aliases: [复利]" in text
    assert out.entry.body.startswith("复利需要时间。（我在 Obsidian 里加的）\n\n## 补充内容（")
    assert (await sync.scan(settle=0)).updated == []  # what the merge wrote is already indexed


async def test_an_edit_made_while_the_model_works_is_not_overwritten(tmp_path):
    holder = {}

    def respond(prompt):
        if "path" in holder:  # the user saves in Obsidian while the summary is being written
            edit(holder.pop("path"), "复利需要时间。", "复利需要时间。（编辑中加的）", age=0)
        return summary_json(title="生成的标题")

    pipe, sync, lib, idx, _ = make(tmp_path, respond=respond)
    eid, path = await saved(pipe)
    holder["path"] = path
    await pipe.merge(eid, [], "补充")
    body = lib.get(eid).body
    assert body.startswith("复利需要时间。（编辑中加的）\n\n## 补充内容（") and body.endswith("补充")
    holder["path"] = path
    await pipe.resummarize(eid)
    assert lib.get(eid).body.count("（编辑中加的）") == 2


async def test_render_of_an_entry_saved_by_hand_is_indexed_on_first_scan(tmp_path):
    """At start every file is compared with the index: edits made while Octopus was not running count too."""
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    fresh = LibrarySync(pipe)  # a restart: nothing known yet
    e = lib.get(eid)
    e.body = "停机时改的"
    path.write_text(render(e), encoding="utf-8")
    os.utime(path, (time.time() - 60, time.time() - 60))
    assert (await fresh.scan(settle=0)).updated == [eid]


async def test_the_worker_picks_up_an_edit_and_questions_use_the_new_text(tmp_path):
    import asyncio

    from test_app import make_app, m

    app, ch, providers = make_app(tmp_path)
    claim_library(app.library.root, app.conn)
    await app.handle(m("/s 复利需要时间。"))
    await app.process_due_jobs()
    [(eid, _)] = app.index.titles()
    path = app.library.find(eid)
    edit(path, "复利需要时间。", "复利需要时间。微笑曲线说明了耐心的价值。")
    app.library_scan_interval = 0.05
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop))
    for _ in range(60):
        if await app.index.search("微笑曲线", keyword_only=True):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, 3)
    await app.handle(m("微笑曲线是什么？"))
    assert "微笑曲线说明了耐心的价值" in providers["claude:sonnet"].calls[-1][0]


async def test_the_users_own_text_outside_the_body_is_searchable_too(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    edit(path, "\n## 摘要\n", "\n开头写的蝴蝶效应\n\n## 摘要\n")
    edit(path, "- 要点二\n", "- 要点二\n\n我的想法：飞轮效应\n")
    await sync.scan(settle=0)
    for word in ("蝴蝶效应", "飞轮效应"):
        hits = await idx.search(word, keyword_only=True)
        assert {h.entry_id for h in hits} == {eid} and any(word in h.text for h in hits)


async def test_an_unmarked_folder_with_its_notes_where_the_index_says_is_marked(tmp_path, caplog):
    from octopus.library import MARKER

    pipe, sync, lib, idx, emb = make(tmp_path)
    for i in range(10):
        await saved(pipe, f"笔记{i}")
    (lib.root / MARKER).unlink()  # a library from before P18: no marker, no identity recorded
    idx.conn.execute("DELETE FROM meta WHERE key = 'library_id'")
    before = {p: p.read_bytes() for p in lib.paths()}
    calls = emb.calls
    with caplog.at_level(logging.INFO, logger="octopus.sync"):
        report = await sync.scan(settle=0)
    assert not report.skipped and (lib.root / MARKER).exists() and "WARNING" in caplog.text and "marked" in caplog.text
    assert (report.updated, report.moved, report.deleted, emb.calls) == ([], [], [], calls)
    assert {p: p.read_bytes() for p in lib.paths()} == before


async def test_an_empty_folder_made_again_is_not_marked(tmp_path):
    from octopus.library import MARKER

    pipe, sync, lib, idx, _ = make(tmp_path)
    for i in range(3):
        await saved(pipe, f"笔记{i}")
    lib.root.rename(tmp_path / "moved")
    lib.root.mkdir()
    assert (await sync.scan(settle=0)).skipped and not (lib.root / MARKER).exists()


async def test_half_of_the_notes_present_is_not_enough(tmp_path):
    from octopus.library import MARKER

    pipe, sync, lib, idx, _ = make(tmp_path)
    paths = [(await saved(pipe, f"笔记{i}"))[1] for i in range(4)]
    (lib.root / MARKER).unlink()
    for p in paths[:2]:
        p.unlink()
    assert (await sync.scan(settle=0)).skipped and not (lib.root / MARKER).exists()


async def test_notes_present_but_in_another_folder_do_not_mark_this_one(tmp_path):
    from octopus.library import MARKER

    pipe, sync, lib, idx, _ = make(tmp_path)
    await saved(pipe)
    (lib.root / MARKER).unlink()
    other = Library(tmp_path / "new-vault", lib.trash)  # library_dir changed by hand; the old folder is still there
    pipe2 = SavePipeline(other, idx, pipe.llm, FetcherRegistry([]), media=pipe.media)
    assert (await LibrarySync(pipe2).scan(settle=0)).skipped
    assert not (tmp_path / "new-vault" / MARKER).exists()


async def test_a_note_deleted_through_octopus_stays_deleted_whatever_copies_exist(tmp_path, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    copy = path.with_name(f"{path.stem} 1.md")  # a copy the user made before deleting in chat
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(copy, (time.time() - 60, time.time() - 60))
    assert await pipe.delete(eid)
    with caplog.at_level(logging.INFO, logger="octopus.sync"):
        await sync.scan(settle=0)
        await sync.scan(settle=0)
    assert idx.count() == 0 and lib.find(eid) is None and lib.resolve(eid[-6:]) == (None, [])
    assert caplog.text.count(str(copy)) == 1
    assert copy.read_text(encoding="utf-8")  # left alone
    edit(copy, "复利需要时间。", "改了一下")  # another version: one more line, still ignored
    with caplog.at_level(logging.INFO, logger="octopus.sync"):
        await sync.scan(settle=0)
    assert caplog.text.count(str(copy)) == 2 and idx.count() == 0


async def test_a_library_marked_before_is_never_marked_again_by_itself(tmp_path):
    from octopus.library import MARKER

    pipe, sync, lib, idx, _ = make(tmp_path)
    await saved(pipe)
    (lib.root / MARKER).unlink()  # its notes are all there, but this library had a marker: something changed
    assert (await sync.scan(settle=0)).skipped and not (lib.root / MARKER).exists()


async def test_the_notice_says_which_problem_it_is(tmp_path):
    from octopus.library import MARKER

    told = []

    async def notify(text):
        told.append(text)

    pipe, sync, lib, idx, _ = make(tmp_path)
    sync.notify = notify
    await saved(pipe)
    (lib.root / MARKER).write_text("another\n", encoding="utf-8")
    await sync.scan(settle=0)
    lib.root.rename(tmp_path / "gone")
    sync._told.clear()
    await sync.scan(settle=0)
    assert "标记文件" in told[0] and "找不到了" not in told[0]
    assert "找不到了" in told[1]


async def test_several_files_for_an_id_tell_the_owner_once(tmp_path):
    told = []

    async def notify(text):
        told.append(text)

    pipe, sync, lib, idx, _ = make(tmp_path)
    sync.notify = notify
    eid, orig = await saved(pipe)
    text = orig.read_text(encoding="utf-8")
    for name in ("a.md", "b.md"):
        (tmp_path / "lib" / name).write_text(text, encoding="utf-8")
    orig.unlink()
    await sync.scan(settle=0)
    await sync.scan(settle=0)
    assert len(told) == 1 and eid in told[0]


async def test_a_supplement_reaches_a_note_renamed_a_moment_ago(tmp_path):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    renamed = tmp_path / "lib" / "读书" / "改了名字.md"
    renamed.parent.mkdir()
    path.rename(renamed)  # no scan since
    out = await pipe.merge(eid, [], "补充的一段")
    assert out is not None and "补充的一段" in renamed.read_text(encoding="utf-8")
    lib._ids.pop(eid, None)
    renamed.rename(renamed.with_name("又改了.md"))
    assert await pipe.annotate(eid, "备注") is not None
    assert "备注" in renamed.with_name("又改了.md").read_text(encoding="utf-8")


async def test_the_window_still_merges_into_a_note_renamed_a_moment_ago(tmp_path):
    from test_app import make_app, m

    app, ch, providers = make_app(tmp_path)
    claim_library(app.library.root, app.conn)
    await app.handle(m("/s 复利需要时间。"))
    await app.process_due_jobs()
    [(eid, _)] = app.index.titles()
    path = app.library.find(eid)
    app.pending.set("tg", "me", eid, None, origin="add")
    path.rename(path.with_name("改名了.md"))
    app.library._ids.clear()
    assert app._merge_target(m("补充")) == eid


async def test_reindex_brings_back_a_deleted_note_put_back_from_the_trash(tmp_path, caplog):
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    assert await pipe.delete(eid)
    trashed = tmp_path / "trash" / path.name
    trashed.rename(path)  # the owner moves it back by hand
    await sync.scan(settle=0)
    assert idx.count() == 0  # scans keep ignoring it
    with caplog.at_level(logging.WARNING, logger="octopus.index.search"):
        await idx.rebuild(lib)
    assert set(idx.paths()) == {eid} and not idx.is_buried(eid) and eid in caplog.text
    assert lib.find(eid) == path


async def test_reindex_keeps_a_users_copy_out_while_the_deleted_note_is_still_in_the_trash(tmp_path):
    # P18 review R2-1 (sim_revive): the tombstone is lifted only when the trashed file was moved back
    pipe, sync, lib, idx, _ = make(tmp_path)
    eid, path = await saved(pipe)
    copy = path.with_name(path.stem + " 1.md")
    copy.write_text(path.read_text(encoding="utf-8").replace("复利", "副本自己的内容"), encoding="utf-8")
    assert await pipe.delete(eid)
    await idx.rebuild(lib)
    assert idx.count() == 0 and idx.is_buried(eid) and lib.find(eid) is None
    assert copy.exists()
    copy.unlink()  # the owner removes the copy and moves the deleted note back: restored as before
    (tmp_path / "trash" / path.name).rename(path)
    await idx.rebuild(lib)
    assert set(idx.paths()) == {eid} and not idx.is_buried(eid)


async def test_a_marker_deleted_after_the_upgrade_is_named_in_the_notice(tmp_path):
    # P18 review R2-2: the notes are all there, only .octopus-library is gone: say so, not "most notes are elsewhere"
    from octopus.library import MARKER
    from octopus.sync import LIBRARY_NO_MARKER, LIBRARY_STRANGE

    told = []

    async def notify(text):
        told.append(text)

    pipe, sync, lib, idx, _ = make(tmp_path)
    sync.notify = notify
    await saved(pipe)
    (lib.root / MARKER).unlink()
    assert (await sync.scan(settle=0)).skipped
    assert told == [LIBRARY_NO_MARKER.format(path=lib.root)] and "init" in told[0]
    for path in lib.root.rglob("*.md"):  # and when the notes are not there either: the old wording
        path.unlink()
    sync._told.clear()
    await sync.scan(settle=0)
    assert told[1] == LIBRARY_STRANGE.format(path=lib.root)
