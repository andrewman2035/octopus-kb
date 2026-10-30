import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from octopus.library import Entry, Library, new_entry_id, parse, render

CST = timezone(timedelta(hours=8))


def make_entry(**kw):
    base = dict(
        id="20261013-abc123",
        title="测试：标题/斜杠",
        source="web",
        saved_at=datetime(2026, 10, 13, 9, 30, tzinfo=CST),
        channel="cli",
        url="https://a.com/x",
        tags=["AI", "产品"],
        summary="一句话摘要。",
        points=["要点一", "要点二"],
        body="第一段\n\n## 原文里的小标题\n\n第二段",
    )
    base.update(kw)
    return Entry(**base)


def test_new_entry_id_format():
    assert re.fullmatch(r"20261013-[0-9a-f]{6}", new_entry_id(datetime(2026, 10, 13, tzinfo=CST)))


def test_render_layout():
    text = render(make_entry())
    assert text.startswith("---\nid: 20261013-abc123\ntitle: 测试：标题/斜杠\n")
    assert "media_url" not in text  # empty optional keys omitted
    assert "tags: [AI, 产品]" in text
    assert "\n## 摘要\n\n一句话摘要。\n\n## 要点\n\n- 要点一\n- 要点二\n\n## 原文或转写稿\n\n第一段" in text


def test_round_trip_keeps_body_headings(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    e = make_entry()
    path = lib.save(e)
    assert path == tmp_path / "lib/2026/10/20261013-abc123-测试-标题-斜杠.md"
    assert lib.load(path) == e
    assert parse(render(e)) == e


def test_notes_round_trip_between_points_and_body():
    e = make_entry(notes="第一条备注\n\n（2026-10-13）第二条")
    text = render(e)
    assert "\n## 要点\n\n- 要点一\n- 要点二\n\n## 我的备注\n\n第一条备注\n\n（2026-10-13）第二条\n\n## 原文或转写稿\n\n第一段" in text
    back = parse(text)
    assert (back.notes, back.points, back.body) == (e.notes, e.points, e.body)
    assert back == e


def test_empty_notes_render_no_heading_and_match_p4_output():
    text = render(make_entry())
    assert "我的备注" not in text
    assert render(make_entry(notes="  \n")) == text
    assert "\n## 要点\n\n- 要点一\n- 要点二\n\n## 原文或转写稿\n\n第一段" in text


def test_body_with_notes_and_points_headings_parses_unchanged():
    body = "开头\n\n## 我的备注\n\n假的备注\n\n## 要点\n\n- 假要点\n\n结尾"
    e = make_entry(body=body)
    back = parse(render(e))
    assert back.notes == "" and back.body == body and back.points == ["要点一", "要点二"]
    assert back == e
    with_notes = make_entry(body=body, notes="真备注")
    assert parse(render(with_notes)) == with_notes


def test_old_file_without_notes_section_parses_empty_notes():
    old = ("---\nid: 20261013-abc123\nsaved_at: 2026-10-13T09:30:00+08:00\n---\n\n"
           "## 摘要\n\n摘要。\n\n## 要点\n\n- 点\n\n## 原文或转写稿\n\n正文\n")
    e = parse(old)
    assert e.notes == "" and e.points == ["点"] and e.body == "正文"


def test_round_trip_minimal(tmp_path):
    e = make_entry(url=None, tags=[], summary="", points=[], body="", title="2026")
    assert parse(render(e)) == e


def test_find_get_delete(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    path = lib.save(make_entry())
    assert lib.find("20261013-abc123") == path
    assert lib.get("20261013-abc123").title == "测试：标题/斜杠"
    assert lib.delete("20261013-abc123") is True
    assert not path.exists()
    assert (tmp_path / "trash" / path.name).exists()
    assert lib.find("20261013-abc123") is None
    assert lib.delete("20261013-abc123") is False


def test_save_leaves_no_tmp_files(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    lib.save(make_entry())
    lib.save(make_entry(title="改个标题"))
    assert list((tmp_path / "lib").rglob("*.tmp")) == []


@pytest.mark.parametrize("missing", ["id", "saved_at"])
def test_parse_missing_required_key_raises_value_error(missing):
    text = "".join(ln for ln in render(make_entry()).splitlines(keepends=True)
                   if not ln.startswith(f"{missing}:"))
    with pytest.raises(ValueError, match=missing):
        parse(text)


def test_parse_missing_frontmatter_raises_value_error():
    with pytest.raises(ValueError):
        parse("just some text")


def test_iter_entries_skips_unreadable_file(tmp_path, caplog):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    good = lib.save(make_entry())
    bad = tmp_path / "lib" / "2026" / "10" / "garbage.md"
    bad.write_text("not a library file at all", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        found = list(lib.iter_entries())
    assert [p for p, _ in found] == [good]
    assert found[0][1] == make_entry()
    assert str(bad) not in caplog.text  # P18: no Octopus id in it, so it is the user's own note: left alone quietly
    broken = tmp_path / "lib" / "2026" / "10" / "broken.md"
    broken.write_text("---\nid: 20261013-ffffff\ntitle: [unclosed\n---\n\n正文\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        assert [p for p, _ in lib.iter_entries()] == [good]
    assert str(broken) in caplog.text


def test_title_change_moves_file(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    e = make_entry()
    old = lib.save(e)
    e.title = "新标题"
    new = lib.save(e)
    assert not old.exists() and new.exists()
    assert [p for p, _ in lib.iter_entries()] == [new]


def test_find_rejects_malformed_ids(tmp_path):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    entry = Entry(id="20261013-abc123", title="t", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc), channel="cli")
    lib.save(entry)
    for bad in ("*", "20261013", "../x", "20261013-abc12*", ""):
        assert lib.find(bad) is None
        assert lib.delete(bad) is False
    assert lib.find("20261013-abc123") is not None


def test_iter_entries_skips_file_with_wrong_typed_frontmatter(tmp_path, caplog):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    good = lib.save(make_entry())
    bad = tmp_path / "lib" / "2026" / "10" / "20261013-ffffff-bad.md"
    bad.write_text("---\nid: 20261013-ffffff\nsaved_at: 2026-10-13T09:30:00+08:00\ntags: 5\n---\n\n## 摘要\n\nx\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="octopus.library"):
        found = list(lib.iter_entries())
    assert [p for p, _ in found] == [good]
    assert str(bad) in caplog.text


def test_save_uses_unique_tmp_names(tmp_path, monkeypatch):
    import os

    lib = Library(tmp_path / "lib", tmp_path / "trash")
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        return real_replace(src, dst)

    monkeypatch.setattr("octopus.library.os.replace", spy)
    e = make_entry()
    lib.save(e)
    lib.save(e)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(n.endswith(".tmp") for n in seen)


def _lib_with(tmp_path, *ids):
    lib = Library(tmp_path / "lib", tmp_path / "trash")
    for i, eid in enumerate(ids):
        lib.save(make_entry(id=eid, title=f"标题{i}", saved_at=datetime(2026, 10, 13 - i, 9, tzinfo=CST)))
    return lib


def test_resolve_unique_suffix_of_3_4_and_6_chars(tmp_path):
    lib = _lib_with(tmp_path, "20261013-6e43c1", "20261012-aa5d02")
    for token in ("3c1", "43c1", "6e43c1"):
        assert lib.resolve(token) == ("20261013-6e43c1", [])
    assert lib.resolve("d02") == ("20261012-aa5d02", [])


def test_resolve_is_case_insensitive(tmp_path):
    lib = _lib_with(tmp_path, "20261013-6e43c1")
    assert lib.resolve("6E43C1") == ("20261013-6e43c1", [])
    assert lib.match_short("43C1") == ["20261013-6e43c1"]


def test_resolve_ambiguous_lists_newest_first(tmp_path):
    lib = _lib_with(tmp_path, "20261012-aaa123", "20261013-bbb123", "20261011-ccc999")
    assert lib.resolve("123") == (None, ["20261013-bbb123", "20261012-aaa123"])
    assert lib.resolve("a123") == ("20261012-aaa123", [])


def test_resolve_none_non_hex_and_too_short_or_long(tmp_path):
    lib = _lib_with(tmp_path, "20261013-6e43c1")
    assert lib.resolve("fff") == (None, [])
    for token in ("xyz9", "9e", "16e43c1", "", "../x"):
        assert lib.match_short(token) == [] and lib.resolve(token) == (None, [])


def test_resolve_full_id(tmp_path):
    lib = _lib_with(tmp_path, "20261013-6e43c1")
    assert lib.resolve("20261013-6e43c1") == ("20261013-6e43c1", [])
    assert lib.resolve("20261013-000000") == (None, [])


def test_resolve_4_to_6_chars_also_match_the_start(tmp_path):
    lib = _lib_with(tmp_path, "20261015-b305ad", "20261012-aa5d02")
    for token in ("b305", "B305A", "b305ad"):
        assert lib.resolve(token) == ("20261015-b305ad", []), token
    assert lib.resolve("c11") == (None, [])  # 3 characters: the end only
    assert lib.resolve("aa5") == (None, [])
    assert lib.resolve("aa5d") == ("20261012-aa5d02", [])


def test_resolve_start_and_end_matches_are_one_ambiguous_list(tmp_path):
    lib = _lib_with(tmp_path, "20261013-5cc8aa", "20261012-c15cc8", "20261011-5cc8b6", "20261010-000000")
    # the start of one, the end of another, both of a third: each once, newest first
    assert lib.resolve("5cc8") == (None, ["20261013-5cc8aa", "20261012-c15cc8", "20261011-5cc8b6"])
    assert lib.match_short("5cc8b6") == ["20261011-5cc8b6"]
