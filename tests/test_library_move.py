"""P18: `octopus library move <dir>` — the library and its attachments move into a vault folder."""
import asyncio
import json
import shutil
from datetime import datetime, timezone

import pytest
from fakes import FakeEmbedder

from octopus import move
from octopus.cli import main
from octopus.config import load_config
from octopus.db import connect
from octopus.index import Index
from octopus.instance import InstanceLock
from octopus.library import Entry, Library
from octopus.move import MoveError, move_library

A, B = "20261013-aaaaaa", "20261014-bbbbbb"


def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.toml"
    config.write_text(f'# 我的配置\ndata_dir = "{data}"\n\n[embed]\ndim = 16\n', encoding="utf-8")
    cfg = load_config(config)
    lib = Library(cfg.library_dir, cfg.trash_dir)
    conn = connect(cfg.db_path, embed_dim=16)
    idx = Index(conn, FakeEmbedder())
    for eid, body in ((A, f"复利\n\n附件：attachments/{A}/图 1.png"), (B, "长期主义")):
        e = Entry(id=eid, title=f"标题{eid[-1]}", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
                  channel="cli", body=body)
        asyncio.run(idx.upsert(e, lib.save(e)))
    conn.close()
    note = lib.find(A)  # written before P18: no links section yet
    note.write_text(note.read_text(encoding="utf-8").split("\n## 附件\n")[0], encoding="utf-8")
    (cfg.attachments_dir / A).mkdir(parents=True)
    (cfg.attachments_dir / A / "图 1.png").write_bytes(b"png" * 100)
    (cfg.attachments_dir / "_incoming" / "x").mkdir(parents=True)
    (cfg.attachments_dir / "_incoming" / "x" / "staged.jpg").write_bytes(b"s")
    (cfg.library_dir / "我的.md").write_text("自己的笔记", encoding="utf-8")
    return cfg, config


def db_paths(cfg):
    conn = connect(cfg.db_path, embed_dim=16)
    try:
        return dict(conn.execute("SELECT id, path FROM entries").fetchall())
    finally:
        conn.close()


def test_move_copies_verifies_switches_and_keeps_a_backup(tmp_path):
    cfg, config = setup(tmp_path)
    old_lib, old_att = cfg.library_dir, cfg.attachments_dir
    target = tmp_path / "Vault" / "Octopus"
    res = move_library(cfg, config, target)
    assert (res.entries, res.files) == (2, 1)
    new = load_config(config)
    assert new.library_dir == target and new.attachments_dir == target / "attachments"
    assert config.read_text(encoding="utf-8").startswith("# 我的配置\n")  # the rest of the file is untouched
    assert (target / "attachments" / A / "图 1.png").read_bytes() == b"png" * 100
    assert (target / "我的.md").read_text(encoding="utf-8") == "自己的笔记"
    note = Library(target, cfg.trash_dir).find(A)
    assert note.parent == target / "2026" / "10"
    assert f"![[attachments/{A}/图 1.png]]" in note.read_text(encoding="utf-8")  # links added on the way
    assert db_paths(cfg) == {A: str(note), B: str(Library(target, cfg.trash_dir).find(B))}
    # old copies: in a dated backup folder, not deleted; staging stays where the channels put files
    assert not old_lib.exists() and not (old_att / A).exists()
    assert (old_att / "_incoming" / "x" / "staged.jpg").exists()
    assert res.backup.parent == cfg.backups_dir
    assert (res.backup / "library" / "我的.md").exists() and (res.backup / "attachments" / A / "图 1.png").exists()
    assert not (cfg.data_dir / move.STATE_NAME).exists()


def test_the_index_follows_without_reembedding(tmp_path, monkeypatch):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    move_library(cfg, config, target)
    conn = connect(cfg.db_path, embed_dim=16)
    try:
        assert conn.execute("SELECT count(*) FROM chunks_vec").fetchone()[0] >= 2  # vectors kept
    finally:
        conn.close()


def test_an_interrupted_copy_runs_again_to_the_end(tmp_path, monkeypatch):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    real = shutil.copy2
    calls = []

    def flaky(src, dst, **kw):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk unplugged")
        return real(src, dst, **kw)

    monkeypatch.setattr(move.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        move_library(cfg, config, target)
    assert load_config(config).library_dir == cfg.library_dir  # not switched yet
    assert cfg.library_dir.exists()
    monkeypatch.setattr(move.shutil, "copy2", real)
    res = move_library(load_config(config), config, target)
    assert res.entries == 2 and load_config(config).library_dir == target


def test_interrupted_after_the_switch_finishes_on_the_next_run(tmp_path, monkeypatch):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"

    def boom(*a, **k):
        raise OSError("killed")

    monkeypatch.setattr(move, "_back_up_old", boom)
    with pytest.raises(OSError):
        move_library(cfg, config, target)
    assert load_config(config).library_dir == target
    state = json.loads((cfg.data_dir / move.STATE_NAME).read_text(encoding="utf-8"))
    assert state["to"] == str(target)
    monkeypatch.undo()
    other = tmp_path / "Elsewhere"
    with pytest.raises(MoveError, match="没做完"):
        move_library(load_config(config), config, other)
    res = move_library(load_config(config), config, target)
    assert res.entries == 2 and not cfg.library_dir.exists()
    assert not (cfg.data_dir / move.STATE_NAME).exists()


def test_a_copy_that_does_not_verify_stops_before_the_switch(tmp_path, monkeypatch):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    real = shutil.copy2

    def corrupt(src, dst, **kw):
        real(src, dst, **kw)
        if str(src).endswith(".png"):
            with open(dst, "ab") as f:
                f.write(b"!")

    monkeypatch.setattr(move.shutil, "copy2", corrupt)
    with pytest.raises(MoveError, match="校验"):
        move_library(cfg, config, target)
    assert load_config(config).library_dir == cfg.library_dir and (cfg.attachments_dir / A / "图 1.png").exists()


def test_a_target_with_another_file_for_the_same_id_is_refused(tmp_path):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    target.mkdir(parents=True)
    (target / "别的.md").write_text(f"---\nid: {A}\nsaved_at: 2026-10-13\n---\n\n不一样\n", encoding="utf-8")
    with pytest.raises(MoveError, match=A):
        move_library(cfg, config, target)
    assert load_config(config).library_dir == cfg.library_dir
    assert not (target / "2026").exists() and not (cfg.data_dir / move.STATE_NAME).exists()


@pytest.mark.parametrize("where", ["data", "parent", "inside", "same"])
def test_targets_that_cannot_work_are_refused(tmp_path, where):
    cfg, config = setup(tmp_path)
    target = {"data": cfg.data_dir, "parent": tmp_path, "inside": cfg.library_dir / "sub",
              "same": cfg.library_dir}[where]
    with pytest.raises(MoveError):
        move_library(cfg, config, target)
    assert load_config(config).library_dir == cfg.library_dir


def test_cli_prints_a_chinese_summary_and_refuses_while_running(tmp_path, capsys):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    lock = InstanceLock(cfg.data_dir / "octopus.lock")
    assert lock.acquire()
    try:
        assert main(["--config", str(config), "library", "move", str(target)]) == 1
        assert "正在运行" in capsys.readouterr().out
    finally:
        lock.release()
    assert main(["--config", str(config), "library", "move", str(target)]) == 0
    out = capsys.readouterr().out
    assert "2 条笔记" in out and "1 个附件" in out and str(target) in out
    assert main(["--config", str(config), "library", "move", str(tmp_path / "data")]) == 1


def test_the_marker_moves_with_the_library_and_hidden_folders_stay(tmp_path):
    from octopus.library import MARKER, read_marker
    from octopus.sync import library_identity

    cfg, config = setup(tmp_path)
    (cfg.library_dir / ".obsidian").mkdir()
    (cfg.library_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    target = tmp_path / "Vault" / "Octopus"
    res = move_library(cfg, config, target)
    conn = connect(cfg.db_path, embed_dim=16)
    try:
        assert read_marker(target) == library_identity(conn) is not None
    finally:
        conn.close()
    # the user's Obsidian settings are neither copied nor swept into the backup; our old marker is gone
    assert (cfg.library_dir / ".obsidian" / "app.json").exists() and not (target / ".obsidian").exists()
    assert not (cfg.library_dir / MARKER).exists() and not (res.backup / "library" / ".obsidian").exists()


def test_links_are_added_without_rewriting_the_rest_of_the_note(tmp_path):
    cfg, config = setup(tmp_path)
    note = Library(cfg.library_dir, cfg.trash_dir).find(A)
    note.write_text(note.read_text(encoding="utf-8").replace("title:", "# 我的注释\ntitle:"), encoding="utf-8")
    before = note.read_text(encoding="utf-8")
    target = tmp_path / "Vault" / "Octopus"
    move_library(cfg, config, target)
    after = Library(target, cfg.trash_dir).refresh()[A].read_text(encoding="utf-8")
    assert after.startswith(before) and f"![[attachments/{A}/图 1.png]]" in after


def test_a_target_holding_another_library_is_refused(tmp_path):
    from octopus.library import write_marker

    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    write_marker(target, "another-library")
    with pytest.raises(MoveError, match="另一个"):
        move_library(cfg, config, target)


def test_a_target_inside_the_data_dir_is_refused(tmp_path):
    cfg, config = setup(tmp_path)
    with pytest.raises(MoveError):
        move_library(cfg, config, cfg.trash_dir / "x")


def test_adopt_points_the_config_at_a_library_folder_moved_by_hand(tmp_path, capsys):
    cfg, config = setup(tmp_path)
    target = tmp_path / "Vault" / "Octopus"
    move_library(cfg, config, target)
    moved = tmp_path / "Vault" / "改了名字"
    target.rename(moved)
    assert main(["--config", str(config), "library", "adopt", str(tmp_path / "Vault")]) == 1  # not that folder
    assert "标记" in capsys.readouterr().out
    assert main(["--config", str(config), "library", "adopt", str(moved)]) == 0
    assert load_config(config).library_dir == moved and "2 条笔记" in capsys.readouterr().out
