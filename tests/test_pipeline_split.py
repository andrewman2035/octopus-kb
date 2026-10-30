from test_pipeline_media import make, staged

import shutil

import pytest
from fakes import fake_media, summary_json

from octopus.media import MediaError

URL = "https://www.reddit.com/r/x/comments/1"
NEW_ID = "20261013-eeeeee"


async def _two_merges(tmp_path, **kw):
    """A needs_manual link entry with two screenshots merged into it in the same minute."""
    pipe, lib, idx, media, p = make(tmp_path, **kw)
    eid = (await pipe.save_failed_url(URL, "cli", "没法自动获取")).entry.id
    a = await pipe.merge(eid, [staged(tmp_path, "a.png")], "")
    b = await pipe.merge(eid, [staged(tmp_path, "b.png")], "")
    return pipe, lib, idx, p, eid, a, b


def _sections(*outs):
    return [(o.added_section, o.added_files) for o in outs]


async def test_merge_reports_its_section_and_files(tmp_path):
    pipe, lib, _, _, eid, a, b = await _two_merges(tmp_path)
    assert a.added_section == f"## 补充内容（2026-10-13 09:30）\n\n### 图片 1（a.png）\n\n图片文字\n\n附件：attachments/{eid}/a.png"
    assert a.added_files == ["a.png"] and b.added_files == ["b.png"]
    assert lib.get(eid).body == f"{a.added_section}\n\n{b.added_section}"


async def test_split_everything_restores_needs_manual(tmp_path):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    out = await pipe.split(eid, _sections(a, b), NEW_ID, "cli")
    new = out.entry
    assert new.id == NEW_ID and new.title == "生成的标题" and new.summary == "这是摘要。"
    assert new.body == (f"### 图片 1（a.png）\n\n图片文字\n\n附件：attachments/{NEW_ID}/a.png\n\n"
                        f"### 图片 2（b.png）\n\n图片文字\n\n附件：attachments/{NEW_ID}/b.png")
    assert lib.get(NEW_ID).body == new.body and new.status == "ok"
    att = tmp_path / "attachments"
    assert (att / NEW_ID / "a.png").exists() and (att / NEW_ID / "b.png").exists()
    assert not (att / eid).exists() or not any((att / eid).iterdir())
    old = lib.get(eid)
    assert (old.status, old.body, old.summary, old.points, old.tags) == ("needs_manual", "", "", [], [])
    assert old.title == URL and old.url == URL
    assert idx.count() == 2


async def test_split_the_later_one_keeps_the_earlier(tmp_path):
    pipe, lib, _, p, eid, a, b = await _two_merges(tmp_path)
    calls = len(p.calls)
    out = await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert out.entry.body == f"### 图片 1（b.png）\n\n图片文字\n\n附件：attachments/{NEW_ID}/b.png"
    old = lib.get(eid)
    assert old.body == a.added_section and old.status == "ok" and old.title == "生成的标题"
    assert len(p.calls) == calls + 2  # the new entry and the old one were both summarised
    att = tmp_path / "attachments"
    assert (att / eid / "a.png").exists() and not (att / eid / "b.png").exists()
    assert (att / NEW_ID / "b.png").exists()


async def test_split_the_first_one_keeps_the_second(tmp_path):
    pipe, lib, _, _, eid, a, b = await _two_merges(tmp_path)
    await pipe.split(eid, _sections(a), NEW_ID, "cli")
    assert lib.get(eid).body == b.added_section


async def test_split_after_original_text_drops_the_blank_line_too(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    eid = (await pipe.save_note("原来的笔记", "cli")).entry.id
    m = await pipe.merge(eid, [], "粘错的一段")
    out = await pipe.split(eid, _sections(m), NEW_ID, "cli")
    assert lib.get(eid).body == "原来的笔记"
    assert out.entry.body == "粘错的一段" and out.entry.source == "manual"


async def test_section_edited_away_is_skipped(tmp_path):
    pipe, lib, _, _, eid, a, b = await _two_merges(tmp_path)
    entry = lib.get(eid)
    entry.body = entry.body.replace(a.added_section, "我手改过的内容")
    lib.save(entry)
    out = await pipe.split(eid, _sections(a, b), NEW_ID, "cli")
    assert out.entry.body == f"### 图片 1（b.png）\n\n图片文字\n\n附件：attachments/{NEW_ID}/b.png"
    assert lib.get(eid).body == "我手改过的内容"
    assert (tmp_path / "attachments" / eid / "a.png").exists()  # its section is gone: its file is not moved


async def test_nothing_left_to_split_is_none(tmp_path):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    before = lib.find(eid).read_text(encoding="utf-8")
    assert await pipe.split(eid, [("## 补充内容（2026-10-13 09:30）\n\n不在里面", [])], NEW_ID, "cli") is None
    assert await pipe.split("20990101-000000", _sections(a), NEW_ID, "cli") is None
    assert lib.find(eid).read_text(encoding="utf-8") == before
    assert lib.find(NEW_ID) is None and idx.count() == 1


async def test_failed_split_restores_everything(tmp_path, monkeypatch):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    before = lib.find(eid).read_text(encoding="utf-8")
    real_upsert = pipe.index.upsert

    async def flaky(entry, path):
        if entry.id == eid:
            raise RuntimeError("index down")
        return await real_upsert(entry, path)

    monkeypatch.setattr(pipe.index, "upsert", flaky)
    with pytest.raises(RuntimeError, match="index down"):
        await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert lib.find(eid).read_text(encoding="utf-8") == before
    assert lib.find(NEW_ID) is None and [r["id"] for r in idx.recent(10)] == [eid]
    att = tmp_path / "attachments"
    assert (att / eid / "b.png").exists() and not (att / NEW_ID).exists()


async def test_old_summary_failure_keeps_content_and_says_so(tmp_path):
    def respond(prompt):
        if "正确的补充" in prompt and "别的文章" not in prompt:
            raise RuntimeError("model down")
        return summary_json(title="生成的标题")

    pipe, lib, *_ = make(tmp_path, respond=respond)
    eid = (await pipe.save_failed_url(URL, "cli", "没法自动获取")).entry.id
    good = await pipe.merge(eid, [], "正确的补充")
    wrong = await pipe.merge(eid, [], "别的文章")
    out = await pipe.split(eid, _sections(wrong), NEW_ID, "cli")
    assert lib.get(NEW_ID).body == "别的文章"
    old = lib.get(eid)
    assert old.body == good.added_section and old.status == "partial" and old.summary == ""
    assert f"/resummarize {eid}" in out.note



class Crash(BaseException):
    """The process being killed: not an Exception, so nothing on the way catches it."""


def _links_ok(lib, tmp_path, eid):
    """Every attachment (附件) line of the entry's Markdown points at a file that exists."""
    body = lib.get(eid).body
    links = [ln.removeprefix("附件：") for ln in body.splitlines() if ln.startswith("附件：")]
    return all((tmp_path / link).is_file() for link in links)


@pytest.mark.parametrize("crash_on", ["b.png", "a.png"])  # the new entry's summary call, then the old one's
async def test_killed_during_a_model_call_leaves_both_markdowns_valid(tmp_path, crash_on):
    state = {"armed": False}

    def respond(prompt):
        if state["armed"]:
            assert _links_ok(lib, tmp_path, eid)  # no file has moved while a model is working
            if crash_on in prompt:
                raise Crash()
        return summary_json(title="生成的标题")

    pipe, lib, idx, _, p = make(tmp_path, respond=respond)
    eid = (await pipe.save_failed_url(URL, "cli", "没法自动获取")).entry.id
    await pipe.merge(eid, [staged(tmp_path, "a.png")], "")
    b = await pipe.merge(eid, [staged(tmp_path, "b.png")], "")
    before = lib.find(eid).read_text(encoding="utf-8")
    state["armed"] = True
    with pytest.raises(Crash):
        await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert lib.find(eid).read_text(encoding="utf-8") == before and _links_ok(lib, tmp_path, eid)
    assert lib.find(NEW_ID) is None and not (tmp_path / "attachments" / NEW_ID).exists()


async def test_failed_rollback_keeps_the_new_entry_and_its_files(tmp_path, monkeypatch):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    real_save, real_upsert = lib.save, pipe.index.upsert
    state = {"broken": False}

    async def upsert(entry, path):
        if entry.id == eid:
            state["broken"] = True  # and the disk goes too: the old Markdown cannot be put back
            raise RuntimeError("index down")
        return await real_upsert(entry, path)

    def save(entry):
        if state["broken"]:
            raise OSError("disk full")
        return real_save(entry)

    monkeypatch.setattr(pipe.index, "upsert", upsert)
    monkeypatch.setattr(lib, "save", save)
    with pytest.raises(RuntimeError, match="index down"):
        await pipe.split(eid, _sections(b), NEW_ID, "cli")
    new = lib.get(NEW_ID)
    assert "b.png" in new.body and _links_ok(lib, tmp_path, NEW_ID)
    assert lib.get(eid).body == a.added_section and _links_ok(lib, tmp_path, eid)


async def test_new_entry_index_failure_changes_nothing(tmp_path, monkeypatch):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    before = lib.find(eid).read_text(encoding="utf-8")
    real_upsert = pipe.index.upsert

    async def upsert(entry, path):
        if entry.id == NEW_ID:
            raise RuntimeError("index down")
        return await real_upsert(entry, path)

    monkeypatch.setattr(pipe.index, "upsert", upsert)
    with pytest.raises(RuntimeError, match="index down"):
        await pipe.split(eid, _sections(a, b), NEW_ID, "cli")
    assert lib.find(eid).read_text(encoding="utf-8") == before and _links_ok(lib, tmp_path, eid)
    assert lib.find(NEW_ID) is None and not (tmp_path / "attachments" / NEW_ID).exists()
    assert [r["id"] for r in idx.recent(10)] == [eid]


async def test_failed_copy_changes_nothing(tmp_path, monkeypatch):
    pipe, lib, idx, _, eid, a, b = await _two_merges(tmp_path)
    before = lib.find(eid).read_text(encoding="utf-8")
    real_copy = shutil.copy2

    def copy2(src, dst):
        if str(src).endswith("b.png"):
            raise OSError("disk full")
        return real_copy(src, dst)

    monkeypatch.setattr("octopus.pipeline.shutil.copy2", copy2)
    with pytest.raises(OSError, match="disk full"):
        await pipe.split(eid, _sections(a, b), NEW_ID, "cli")
    assert lib.find(eid).read_text(encoding="utf-8") == before and _links_ok(lib, tmp_path, eid)
    assert lib.find(NEW_ID) is None and not (tmp_path / "attachments" / NEW_ID).exists()


async def test_taken_new_id_is_refused(tmp_path):
    pipe, lib, _, _, eid, a, b = await _two_merges(tmp_path)
    taken = tmp_path / "attachments" / NEW_ID
    taken.mkdir(parents=True)
    (taken / "b.png").write_bytes(b"someone else's")
    with pytest.raises(MediaError):
        await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert (taken / "b.png").read_bytes() == b"someone else's"
    assert "b.png" in lib.get(eid).body and _links_ok(lib, tmp_path, eid)


async def test_carriage_returns_in_merged_text_still_split(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    eid = (await pipe.save_note("原来的笔记", "cli")).entry.id
    m = await pipe.merge(eid, [], "第一行\r\n第二行\r第三行")
    assert "\r" not in m.added_section
    out = await pipe.split(eid, [(m.added_section.replace("\n", "\r\n"), [])], NEW_ID, "cli")
    assert out.entry.body == "第一行\n第二行\n第三行" and lib.get(eid).body == "原来的笔记"


async def test_unreadable_sections_make_the_new_entry_partial(tmp_path):
    pipe, lib, *_ = make(tmp_path, media=fake_media(tmp_path, fail="ocr"))
    eid = (await pipe.save_note("原来的笔记", "cli")).entry.id
    m = await pipe.merge(eid, [staged(tmp_path, "a.png")], "")
    assert m.added_notes == ["a.png：ocr 失败"]
    out = await pipe.split(eid, [(m.added_section, m.added_files, m.added_notes)], NEW_ID, "cli")
    assert out.entry.status == "partial" and out.note == "a.png：ocr 失败"


async def test_identical_sections_one_is_split(tmp_path):
    pipe, lib, *_ = make(tmp_path)
    eid = (await pipe.save_note("原来的笔记", "cli")).entry.id
    first = await pipe.merge(eid, [], "同一段")
    second = await pipe.merge(eid, [], "同一段")
    assert first.added_section == second.added_section
    await pipe.split(eid, _sections(second), NEW_ID, "cli")
    assert lib.get(eid).body == f"原来的笔记\n\n{first.added_section}"


async def test_old_entry_deleted_by_hand_meanwhile_is_not_brought_back(tmp_path):
    armed = []

    def respond(prompt):
        if armed and "a.png" in prompt and "b.png" not in prompt:  # the old entry's summary call
            lib.find(eid).unlink()
        return summary_json(title="生成的标题")

    pipe, lib, idx, _, p = make(tmp_path, respond=respond)
    eid = (await pipe.save_failed_url(URL, "cli", "没法自动获取")).entry.id
    await pipe.merge(eid, [staged(tmp_path, "a.png")], "")
    b = await pipe.merge(eid, [staged(tmp_path, "b.png")], "")
    armed.append(1)
    out = await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert lib.find(eid) is None and "b.png" in lib.get(NEW_ID).body and _links_ok(lib, tmp_path, NEW_ID)
    assert out.note is None


async def test_original_still_linked_by_hand_is_kept(tmp_path):
    pipe, lib, _, _, eid, a, b = await _two_merges(tmp_path)
    entry = lib.get(eid)
    entry.body += f"\n\n我自己引用的：\n附件：attachments/{eid}/b.png"
    lib.save(entry)
    await pipe.split(eid, _sections(b), NEW_ID, "cli")
    assert f"附件：attachments/{eid}/b.png" in lib.get(eid).body and _links_ok(lib, tmp_path, eid)
    assert _links_ok(lib, tmp_path, NEW_ID) and "b.png" in lib.get(NEW_ID).body
