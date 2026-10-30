"""P26: scripts/export_public.py builds a clean public tree from a commit, aborts on any marker without printing it,
and only ever makes a local repository. Every test uses a throwaway repository and invented markers."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("export_public", ROOT / "scripts" / "export_public.py")
export_public = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = export_public  # dataclasses look their module up
_spec.loader.exec_module(export_public)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
IDENT = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *IDENT, *args], check=True, capture_output=True, text=True).stdout


def make_repo(tmp_path, files: dict[str, str | bytes]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--template=")
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (repo / rel).write_bytes(content)
        else:
            (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "add", "-A", "-f")
    git(repo, "commit", "-q", "-m", "x")
    return repo


def markers_file(tmp_path, *lines, name="markers.txt", bom=False) -> Path:
    path = tmp_path / name
    path.write_text(("﻿" if bom else "") + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def hits(text: str, *lines: str, tmp_path) -> list[tuple[int, int, int]]:
    """(text line, markers file, markers line) for an invented markers file."""
    return export_public.marker_hits(text, export_public.read_markers([markers_file(tmp_path, *lines)]))


PUBLIC = {"README.md": "# Demo\n", "src/demo/app.py": "print('hi')\n", "docs/setup.md": "setup\n",
          "tests/test_x.py": "def test_x():\n    pass\n"}
INTERNAL = {"docs/superpowers/plan.md": "internal plan\n", "docs/smoke-p1.md": "smoke run\n",
            ".omc/state.json": "{}\n", ".superpowers/x.md": "x\n", "scratch.txt": "notes\n",
            "scratch/a.md": "a\n", "extension/shortcuts/Save (page).shortcut": b"AEA1\x00\xffbinary"}


def test_exports_the_tracked_public_files_only(tmp_path, capsys):
    repo = make_repo(tmp_path, {**PUBLIC, **INTERNAL})
    (repo / "untracked.md").write_text("not committed\n", encoding="utf-8")
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo)]) == 0
    got = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert got == sorted(PUBLIC)
    printed = capsys.readouterr().out
    assert "exported 4 files" in printed and all(f"  {p}" in printed for p in INTERNAL)


def test_the_export_never_contains_a_shortcut_file(tmp_path, capsys):
    """Owner ruling: nothing signed with the owner's Apple account is ever published; users sign their own."""
    shortcuts = {"extension/shortcuts/Save (page).shortcut": b"AEA1\x00\xffbinary",
                 "extension/shortcuts/存到 Octopus（网页）.shortcut": b"AEA1\x00signed",
                 "docs/Copy.shortcut": b"AEA1\x00elsewhere", "Top.shortcut": b"plain"}
    repo = make_repo(tmp_path, {**PUBLIC, **shortcuts})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo)]) == 0
    assert [p for p in out.rglob("*") if p.suffix == ".shortcut"] == []
    printed = capsys.readouterr().out
    assert all(f"  {p}" in printed for p in shortcuts)  # listed as excluded
    assert not any(export_public.fnmatch.fnmatch(p, g) for p in shortcuts for g in export_public.BINARY_ALLOWLIST)


def test_there_is_no_option_to_include_shortcuts(tmp_path):
    repo = make_repo(tmp_path, PUBLIC)
    with pytest.raises(SystemExit) as refused:
        export_public.main([str(tmp_path / "out"), "--repo", str(repo), "--include-shortcuts"])
    assert refused.value.code == 2 and not (tmp_path / "out").exists()


def test_a_generic_marker_aborts_and_removes_the_output(tmp_path, capsys):
    repo = make_repo(tmp_path, {**PUBLIC, "docs/setup.md": "line one\nmail " + "someone" + "@mail-provider.com\n"})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "docs/setup.md:2: generic marker (email address)" in err and "mail-provider" not in err
    assert list(out.iterdir()) == []


def test_a_private_marker_is_reported_by_file_and_line_never_by_its_text(tmp_path, capsys):
    repo = make_repo(tmp_path, {**PUBLIC, "tests/test_x.py": "TITLE = '一篇虚构的文章标题'\n"})
    first = markers_file(tmp_path, "Nothing Here", name="a.txt")
    second = markers_file(tmp_path, "Nothing Else", "", "一篇虚构的文章", name="b.txt")
    out = tmp_path / "out"
    args = [str(out), "--repo", str(repo), "--private-markers", str(first), "--private-markers", str(second)]
    assert export_public.main(args) == 1
    err = capsys.readouterr().err
    assert "tests/test_x.py:1: private marker (markers file 2, line 3)" in err
    assert "一篇虚构" not in err and "Nothing" not in err


# ---- the variants a marker may take (review p26 I1) ----

def test_case_and_full_width_forms(tmp_path):
    assert hits("an invented title here", "Invented Title", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("Ｉｎｖｅｎｔｅｄ　Ｔｉｔｌｅ", "Invented Title", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("编号：２０２６１０１３－ａｂｃ１２３", "20261013-abc123", tmp_path=tmp_path) == [(1, 1, 1)]


def test_markers_split_across_lines(tmp_path):
    python = 'TITLE = ("虚构文章"\n         "标题")\n'
    markdown = "一段话提到虚构文章\n标题，然后继续。\n"
    assert hits(python, "虚构文章标题", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits(markdown, "虚构文章标题", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("an invented\ntitle", "Invented Title", tmp_path=tmp_path) == [(1, 1, 1)]


def test_url_encoded_slugged_and_spaced_forms(tmp_path):
    assert hits("https://s.example.com/?q=" + quote("虚构文章标题"), "虚构文章标题", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("/notes/invented-title.md", "Invented Title: Part Two", tmp_path=tmp_path) == []
    assert hits("/notes/invented-title-part-two.md", "Invented Title: Part Two", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("invented_title_part_two", "Invented Title: Part Two", tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("Invented  Title:   part two", "Invented Title: Part Two", tmp_path=tmp_path) == [(1, 1, 1)]


def test_an_id_marker_also_matches_its_hex_part_and_short_forms(tmp_path):
    marker = "20261013-abc123"
    assert hits("see x_ABC123.png", marker, tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("/read abc1 and /delete c123", marker, tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("@bc123 讲了什么", marker, tmp_path=tmp_path) == [(1, 1, 1)]
    assert hits("hash 9abc1234ff and abc1239", marker, tmp_path=tmp_path) == []  # inside longer tokens


def test_a_short_ascii_word_marker_matches_only_as_written(tmp_path):
    """A one-word title of a few letters is also an everyday word: matched only in its own case, as a whole word."""
    assert hits("see tests/conftest.py\nTESTS\nMy Tests.\n读了Tests的说明", "Tests", tmp_path=tmp_path) == [
        (3, 1, 1), (4, 1, 1)]


def test_a_bom_in_the_markers_file_is_ignored(tmp_path):
    found = export_public.read_markers([markers_file(tmp_path, "Invented Title", bom=True)])
    assert export_public.marker_hits("invented title", found) == [(1, 1, 1)]


def test_utf16_files_are_scanned(tmp_path, capsys):
    repo = make_repo(tmp_path, {**PUBLIC, "docs/notes.txt": "前言\n虚构文章标题\n".encode("utf-16")})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo), "--private-markers",
                               str(markers_file(tmp_path, "虚构文章标题"))]) == 1
    assert "docs/notes.txt:2: private marker (markers file 1, line 1)" in capsys.readouterr().err


def test_a_binary_file_not_on_the_allowlist_aborts(tmp_path, capsys):
    repo = make_repo(tmp_path, {**PUBLIC, "docs/report.docx": b"PK\x03\x04\x00\xff\xfe binary"})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo)]) == 1
    assert "docs/report.docx: binary file not on the allowlist" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_allowlisted_binaries_are_byte_scanned(tmp_path, capsys):
    png = b"\x89PNG\r\n\x1a\n\x00\x00" + "虚构文章标题".encode("utf-16-le") + b"\x00\xff"
    repo = make_repo(tmp_path, {**PUBLIC, "extension/chrome/icons/icon16.png": png})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo), "--private-markers",
                               str(markers_file(tmp_path, "虚构文章标题"))]) == 1
    assert "extension/chrome/icons/icon16.png: private marker in the bytes (markers file 1, line 1)" in \
        capsys.readouterr().err


def test_generic_scan_sees_full_width_at_and_real_looking_ilink_ids():
    # put together here, so this file does not trip its own scan
    assert export_public.generic_hits("write to someone" + "＠mail-provider.com") != []
    assert export_public.generic_hits("from o9cq80a1b2c3d4e5f6" + "@im.wechat") != []
    assert export_public.generic_hits("owner@im.wechat b2@im.bot wx-owner-7f3a9c@im.wechat") == []


def test_an_owner_todo_placeholder_aborts_the_export(tmp_path, capsys):
    todo = "OWNER" + "-TODO:"  # put together here, so this file does not trip the check itself
    repo = make_repo(tmp_path, {**PUBLIC, "src/demo/app.py": f"# Copyright (c) {todo} paste the upstream line\n"})
    out = tmp_path / "out"
    assert export_public.main([str(out), "--repo", str(repo)]) == 1
    assert "src/demo/app.py:1: owner TODO placeholder still present" in capsys.readouterr().err
    assert list(out.iterdir()) == []


def test_the_summary_names_the_markers_matched_only_as_written(tmp_path, capsys):
    repo = make_repo(tmp_path, PUBLIC)
    markers = markers_file(tmp_path, "Invented Title", "Tests", "虚构文章标题", "Demo")
    assert export_public.main([str(tmp_path / "out"), "--repo", str(repo), "--private-markers", str(markers)]) == 1
    capsys.readouterr()  # "Demo" is in README.md: matched as written, so the run stops
    markers = markers_file(tmp_path, "Invented Title", "Tests", "虚构文章标题", "Ghost", name="m2.txt")
    assert export_public.main([str(tmp_path / "out2"), "--repo", str(repo), "--private-markers", str(markers)]) == 0
    printed = capsys.readouterr().out
    assert "2 short one-word markers matched only as written (case-sensitive, whole word): file 1 lines 2, 4" in printed
    assert "Tests" not in printed and "Ghost" not in printed


# ---- the output and the fresh repository ----

def test_refuses_a_non_empty_target(tmp_path, capsys):
    repo = make_repo(tmp_path, PUBLIC)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("mine", encoding="utf-8")
    assert export_public.main([str(out), "--repo", str(repo)]) == 2
    assert "refusing" in capsys.readouterr().err
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def test_a_failure_part_way_removes_the_partial_output(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, PUBLIC)
    out = tmp_path / "out"

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(export_public, "scan_file", broken)
    with pytest.raises(OSError):
        export_public.main([str(out), "--repo", str(repo)])
    assert not out.exists() or list(out.iterdir()) == []


def test_init_git_makes_one_utc_commit_by_the_author_with_every_file_and_no_remote(tmp_path, monkeypatch):
    files = {**PUBLIC, ".gitignore": "*.log\n", "docs/example.log": "kept on purpose\n"}  # tracked though ignored
    repo = make_repo(tmp_path, {**files, **INTERNAL})
    git(repo, "commit", "-q", "--allow-empty", "-m", "second")  # history that must not come along
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Someone Else")  # the environment never overrides the author given
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "else@example.org")
    out = tmp_path / "out"
    author = "Jane Doe <jane@example.com>"
    assert export_public.main([str(out), "--repo", str(repo), "--init-git", "--author", author]) == 0
    log = git(out, "log", "--format=%an <%ae>|%cn <%ce>|%ad|%cd|%s", "--date=format:%z").strip().splitlines()
    assert log == [f"{author}|{author}|+0000|+0000|Initial public release"]
    assert git(out, "remote").strip() == ""
    assert sorted(git(out, "ls-files").split("\n")[:-1]) == sorted(files)


def test_init_git_ignores_global_and_system_git_config(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, PUBLIC)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[i18n]\n\tcommitEncoding = ISO-8859-1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))  # the user's global config would put an encoding header on the commit
    out = tmp_path / "out"
    author = "Jane Doe <jane@example.com>"
    assert export_public.main([str(out), "--repo", str(repo), "--init-git", "--author", author]) == 0
    assert "encoding" not in subprocess.run(["git", "-C", str(out), "cat-file", "-p", "HEAD"], capture_output=True,
                                            text=True, check=True, env={"PATH": os.environ["PATH"]}).stdout


def test_init_git_message_is_scanned(tmp_path, capsys):
    repo = make_repo(tmp_path, PUBLIC)
    out = tmp_path / "out"
    args = [str(out), "--repo", str(repo), "--private-markers", str(markers_file(tmp_path, "虚构文章标题")),
            "--init-git", "--author", "Jane Doe <jane@example.com>", "--message", "Release 虚构文章标题"]
    assert export_public.main(args) == 1
    assert "commit message: private marker (markers file 1, line 1)" in capsys.readouterr().err
    assert not (out / ".git").exists()


def test_init_git_needs_a_well_formed_author(tmp_path):
    repo = make_repo(tmp_path, PUBLIC)
    with pytest.raises(SystemExit):
        export_public.main([str(tmp_path / "a"), "--repo", str(repo), "--init-git"])
    with pytest.raises(SystemExit):
        export_public.main([str(tmp_path / "b"), "--repo", str(repo), "--init-git", "--author", "no email"])


@pytest.mark.parametrize("path,private", [
    ("docs/superpowers/p5-notes.md", True), ("docs/smoke-p3.md", True), (".omc/x", True), ("a/.DS_Store", True),
    ("scratch/notes.md", True), ("scratch.txt", True), ("extension/shortcuts/x.shortcut", True),
    ("src/octopus/scratchpad.py", False), ("tests/test_scratch_paths.py", False), ("docs/scratch.md", False),
    ("docs/setup.md", False), ("docs/manual-testing.md", False), ("README.md", False), ("src/octopus/app.py", False),
    ("extension/shortcuts/build.py", False),
])
def test_which_paths_are_private(path, private):
    assert export_public.is_private(path) is private
