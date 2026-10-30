"""P18: the library as an Obsidian vault folder — note format, finding files by id, edits kept."""
import logging
from datetime import datetime, timedelta, timezone

from octopus.library import ATTACH_END, ATTACH_START, Entry, Library, parse, rebase, render

CST = timezone(timedelta(hours=8))
EID = "20261013-abc123"


def make_entry(**kw):
    base = dict(id=EID, title="标题", source="web", saved_at=datetime(2026, 10, 13, 9, 30, tzinfo=CST),
                channel="cli", tags=["AI"], summary="摘要。", points=["要点一"], body="正文")
    base.update(kw)
    return Entry(**base)


def lib_at(tmp_path, **kw) -> Library:
    return Library(tmp_path / "lib", tmp_path / "trash", **kw)


# --- attachments section --------------------------------------------------------------------------------------

def test_attachment_lines_get_obsidian_links_in_their_own_section():
    body = (f"### 图片 1（a b.png）\n\nOCR\n\n附件：attachments/{EID}/a b.png\n附件：attachments/{EID}/doc.pdf\n"
            f"附件：attachments/{EID}/data.csv")
    text = render(make_entry(body=body))
    assert text.endswith(f"\n\n## 附件\n\n{ATTACH_START}\n![[attachments/{EID}/a b.png]]\n"
                         f"![[attachments/{EID}/doc.pdf]]\n[[attachments/{EID}/data.csv]]\n{ATTACH_END}\n")
    assert parse(text) == make_entry(body=body)  # the section is derived: it never becomes part of the body


def test_no_attachments_no_section_and_unsafe_names_get_a_markdown_link():
    assert "## 附件" not in render(make_entry())
    text = render(make_entry(body=f"附件：attachments/{EID}/x[1].png\n附件：attachments/{EID}/a#b.txt"))
    assert f"![](<attachments/{EID}/x[1].png>)" in text and f"[a#b.txt](<attachments/{EID}/a#b.txt>)" in text
    assert parse(text).body == f"附件：attachments/{EID}/x[1].png\n附件：attachments/{EID}/a#b.txt"


def test_links_obsidian_rewrote_and_text_after_the_section_survive():
    body = f"正文\n\n附件：attachments/{EID}/a.png"
    text = render(make_entry(body=body)).replace(f"![[attachments/{EID}/a.png]]",
                                                 f"![[Octopus/attachments/{EID}/a.png]]\n![[新加的.png]]")
    assert parse(text).body == body  # whatever is between the markers is generated
    later = text + "\n我在最后写的一段\n"
    assert parse(later).body == f"{body}\n\n我在最后写的一段"
    assert parse(render(parse(later))) == parse(later)


def test_a_user_section_named_attachments_in_the_body_is_kept():
    body = "正文\n\n## 附件\n\n我自己写的说明"
    assert parse(render(make_entry(body=body))).body == body


# --- what the user writes in Obsidian survives a save -----------------------------------------------------------

def test_unknown_frontmatter_keys_are_kept():
    text = render(make_entry()).replace("tags: [AI]\n", "tags: [AI]\naliases: [别名]\ncssclasses: [wide]\n")
    e = parse(text)
    assert e.extra == {"aliases": ["别名"], "cssclasses": ["wide"]}
    again = render(e)
    assert "aliases: [别名]" in again and "cssclasses: [wide]" in again
    assert parse(again) == e


def test_only_a_heading_on_the_first_line_is_the_title():
    text = render(make_entry()).replace("\n## 摘要\n", "\n开头\n\n# 这不是标题\n\n## 摘要\n", 1)
    e = parse(text)
    assert e.title == "标题" and render(e) == text  # left as written


def test_points_with_a_blank_line_between_them_stay_points():
    text = render(make_entry()).replace("- 要点一\n", "- 要点一\n\n- 要点二\n\n我的话\n", 1)
    e = parse(text)
    assert e.points == ["要点一", "要点二"] and e.more == "我的话"


def test_a_single_tag_typed_by_hand_is_one_tag():
    text = render(make_entry()).replace("tags: [AI]", "tags: AI")
    assert parse(text).tags == ["AI"]


def test_a_new_entry_never_writes_over_a_file_already_there(tmp_path):
    lib = lib_at(tmp_path)
    e = make_entry()
    taken = lib.path_for(e)
    taken.parent.mkdir(parents=True)
    other = render(make_entry(id="20261013-999999", body="另一条"))  # a file under this name that is not ours
    taken.write_text(other, encoding="utf-8")
    path = lib.save(e)
    assert path != taken and taken.read_text(encoding="utf-8") == other and path.name.endswith(" 1.md")


def test_text_before_the_summary_and_a_first_heading_title():
    text = render(make_entry()).replace("\n## 摘要\n", "\n# 我改的标题\n\n开头的话\n\n## 摘要\n", 1)
    e = parse(text)
    assert e.title == "我改的标题" and e.preamble == "# 我改的标题\n\n开头的话"
    assert parse(render(e)) == e
    e.title = "又改了"  # 改标题 from chat rewrites the heading too, so the heading does not undo it
    assert parse(render(e)).title == "又改了" and "\n# 又改了\n\n开头的话\n\n## 摘要\n" in render(e)


def test_prose_and_own_sections_after_the_points_are_kept():
    text = render(make_entry()).replace("- 要点一\n", "- 要点一\n- 我加的要点\n\n我的想法\n\n## 我的小节\n\n内容\n", 1)
    e = parse(text)
    assert e.points == ["要点一", "我加的要点"]
    assert e.more == "我的想法\n\n## 我的小节\n\n内容"
    assert parse(render(e)) == e
    e.points = ["新要点"]  # a re-summary replaces the points, not the user's text after them
    assert "## 要点\n\n- 新要点\n\n我的想法\n\n## 我的小节\n\n内容\n\n## 原文或转写稿" in render(e)


def test_files_written_before_p18_render_byte_for_byte_the_same():
    e = make_entry(notes="备注")
    text = render(e)
    assert parse(text).extra == {} and render(parse(text)) == text


# --- finding files by id ----------------------------------------------------------------------------------------

def test_a_renamed_or_moved_file_is_found_by_its_id_and_saved_in_place(tmp_path):
    lib = lib_at(tmp_path)
    path = lib.save(make_entry())
    moved = tmp_path / "lib" / "读书" / "我的笔记.md"
    moved.parent.mkdir()
    path.rename(moved)
    assert lib.find(EID) is None  # found by the next walk (the scanner's), never by find() itself
    lib.refresh()
    assert lib.find(EID) == moved
    assert lib.get(EID).title == "标题"
    assert lib.save(make_entry(title="新标题")) == moved  # never renamed back
    assert moved.exists() and not path.exists()
    assert lib.match_short("abc123") == [EID] and lib.resolve("c123") == (EID, [])


def test_files_without_an_octopus_id_are_ignored_quietly(tmp_path, caplog):
    lib = lib_at(tmp_path, attachments=tmp_path / "lib" / "attachments")
    good = lib.save(make_entry())
    own = tmp_path / "lib" / "我的笔记.md"
    own.write_text("---\ntags: [x]\n---\n\n随手记\n", encoding="utf-8")
    (tmp_path / "lib" / "plain.md").write_text("没有文件头", encoding="utf-8")
    other = tmp_path / "lib" / "other-id.md"
    other.write_text("---\nid: abc\nsaved_at: 2026-10-13\n---\n", encoding="utf-8")
    hidden = tmp_path / "lib" / ".trash" / "x.md"
    hidden.parent.mkdir()
    hidden.write_text(render(make_entry(id="20261013-111111")), encoding="utf-8")
    attached = tmp_path / "lib" / "attachments" / EID / "note.md"
    attached.parent.mkdir(parents=True)
    attached.write_text(render(make_entry(id="20261013-222222")), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        assert [p for p, _ in lib.iter_entries()] == [good]
    assert caplog.text == ""
    assert lib.find("20261013-111111") is None and lib.find("20261013-222222") is None
    assert own.read_text(encoding="utf-8") == "---\ntags: [x]\n---\n\n随手记\n"


def test_a_copied_file_with_the_same_id_does_not_take_over(tmp_path, caplog):
    lib = lib_at(tmp_path)
    path = lib.save(make_entry())
    copy = tmp_path / "lib" / "0000 副本.md"  # sorts before the original
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        assert lib.refresh() == {EID: path}
    assert f"{copy} is a copy of {path}" in caplog.text
    fresh = lib_at(tmp_path)  # nothing known, no index: two candidates, none adopted
    assert fresh.refresh() == {} and EID in fresh.ambiguous
    assert list(fresh.iter_entries()) == []


def test_save_remembers_what_it_wrote(tmp_path):
    lib = lib_at(tmp_path)
    path = lib.save(make_entry())
    st = path.stat()
    assert lib.wrote(path, (st.st_mtime_ns, st.st_size))
    path.write_text(path.read_text(encoding="utf-8") + "\n用户加的\n", encoding="utf-8")
    st = path.stat()
    assert not lib.wrote(path, (st.st_mtime_ns, st.st_size))


# --- rebase: an op that ran while the file was edited -----------------------------------------------------------

def test_rebase_keeps_the_users_edit_and_the_ops_change():
    base = make_entry(body="原文")
    mine = make_entry(body="原文\n\n## 补充内容（x）\n\n补充", summary="新摘要", tags=["新"], points=["新点"])
    theirs = make_entry(body="原文（我改过）", title="我改的标题", notes="我的备注")
    out = rebase(base, mine, theirs)
    assert out.body == "原文（我改过）\n\n## 补充内容（x）\n\n补充"
    assert (out.title, out.notes) == ("我改的标题", "我的备注")
    assert (out.summary, out.tags, out.points) == ("新摘要", ["新"], ["新点"])


def test_rebase_when_nothing_changed_on_disk_is_the_op():
    base = make_entry()
    mine = make_entry(summary="新")
    assert rebase(base, mine, make_entry()) == mine


def test_a_links_block_missing_its_closing_marker_loses_no_text():
    body = f"正文\n\n附件：attachments/{EID}/a.png"
    text = render(make_entry(body=body)).replace(f"{ATTACH_END}\n", "我在下面写的话\n")
    e = parse(text)
    assert "我在下面写的话" in e.body and e.body.startswith(body)
    again = render(e)
    assert "我在下面写的话" in again and again.endswith(f"{ATTACH_END}\n")
    assert parse(again) == e


def test_saving_a_note_with_an_unclosed_links_block_is_logged(tmp_path, caplog):
    lib = lib_at(tmp_path)
    body = f"正文\n\n附件：attachments/{EID}/a.png"
    path = lib.save(make_entry(body=body))
    path.write_text(path.read_text(encoding="utf-8").replace(f"{ATTACH_END}\n", "下面的话\n"), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        lib.save(lib.get(EID))
    assert "下面的话" in path.read_text(encoding="utf-8") and str(path) in caplog.text
