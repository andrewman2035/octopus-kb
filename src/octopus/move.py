"""`octopus library move <dir>`: the library and its attachments move to another folder, e.g. in an Obsidian vault.

Copy everything, check every copy (size and SHA-256), and only then switch: config.toml gets library_dir, the index
gets the new paths (no re-embedding: the text did not change), notes get their attachment links, and the old copies
go to a dated folder under backups/ (not deleted). Run again after an interruption: copies already made are checked
and kept; after the switch, a small state file in data_dir says what is left to do."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from octopus.config import Config, ConfigError, load_config, write_default_config
from octopus.db import connect
from octopus.i18n import t
from octopus.index import Index
from octopus.config import library_dir_problem
from octopus.library import (ATTACH_LINE_RE, MARKER, READ_ERRORS, Library, attachment_links, has_links_block, parse,
                             read_marker)
from octopus.sync import claim_library, library_identity

STATE_NAME = "library-move.json"
INCOMING = "_incoming"  # the channels' staging folder: never moved


class MoveError(Exception):
    """The move cannot start or finish; the message is for the user."""


@dataclass
class MoveResult:
    target: Path
    entries: int
    files: int
    backup: Path | None


def _digest(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _same(a: Path, b: Path) -> bool:
    return b.is_file() and a.stat().st_size == b.stat().st_size and _digest(a) == _digest(b)


def _files_under(root: Path, skip: Path | None = None) -> list[Path]:
    """Every regular file under root, except hidden ones (.obsidian, .DS_Store), *.tmp and the folder skip."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [n for n in dirnames if not n.startswith(".") and here / n != skip]
        out.extend(here / n for n in filenames if not n.startswith(".") and not n.endswith(".tmp"))
    return sorted(out)


def _plan(src_lib: Path, src_att: Path, dst_lib: Path, dst_att: Path) -> list[tuple[Path, Path]]:
    pairs = [(p, dst_lib / p.relative_to(src_lib)) for p in _files_under(src_lib, skip=src_att)]
    pairs += [(p, dst_att / p.relative_to(src_att)) for p in _files_under(src_att, skip=src_att / INCOMING)]
    return pairs


def _conflict(pairs: list[tuple[Path, Path]], src_ids: dict[str, Path], dst: Library) -> str | None:
    expected = {d for _, d in pairs}
    for eid, path in dst.refresh().items():
        if eid in src_ids and path not in expected:
            return t("move.id_conflict", id=eid, path=path)
    for src, d in pairs:
        if d.exists() and not _same(src, d):
            return t("move.file_conflict", path=d)
    return None


def _copy(pairs: list[tuple[Path, Path]]) -> None:
    for src, dst in pairs:
        if _same(src, dst):  # copied by an earlier, interrupted run
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.move.tmp")  # hidden: no scan sees a half-copied file
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    for src, dst in pairs:
        if not _same(src, dst):
            raise MoveError(t("move.verify_failed", path=dst))


def _set_config(config_path: Path, target: Path) -> None:
    """library_dir = target as a top-level line of config.toml; every other line stays as it is."""
    write_default_config(config_path)  # no file yet (all defaults): start from the default one
    old = config_path.read_text(encoding="utf-8")
    line = f"library_dir = {json.dumps(str(target), ensure_ascii=False)}\n"
    lines = old.splitlines(keepends=True)
    end = next((i for i, ln in enumerate(lines) if re.match(r"\s*\[", ln)), len(lines))  # before the first [table]
    at = next((i for i in range(end) if re.match(r"\s*library_dir\s*=", lines[i])), None)
    if at is not None:
        lines[at] = line
    else:
        at = next((i + 1 for i in range(end) if re.match(r"\s*data_dir\s*=", lines[i])), 0)
        if at and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines.insert(at, line)
    tmp = config_path.with_name(f"{config_path.name}.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    shutil.copymode(config_path, tmp)
    os.replace(tmp, config_path)
    try:
        ok = load_config(config_path).library_dir == target
    except (ConfigError, ValueError, TypeError) as e:
        ok, why = False, str(e)
    else:
        why = ""
    if not ok:
        config_path.write_text(old, encoding="utf-8")
        raise MoveError(t("move.config_failed", path=config_path, why=why).strip())


def _merge_move(src: Path, dst: Path) -> None:
    """Move src to dst; when dst is there already (an earlier, interrupted run), move what is left into it."""
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    elif src.is_dir() and dst.is_dir():
        for child in list(src.iterdir()):
            _merge_move(child, dst / child.name)
        src.rmdir()
    else:
        os.replace(src, dst)


def _back_up_old(src_lib: Path, src_att: Path, backup: Path, identity: str) -> None:
    """What was copied goes to backup. Hidden files and folders (.obsidian, .trash: the user's, never copied) stay
    where they are; our own marker is removed so the old folder is no longer taken for the library."""
    if read_marker(src_lib) == identity:
        (src_lib / MARKER).unlink()
    for src, dst in ((src_lib, backup / "library"), (src_att, backup / "attachments")):
        if not src.is_dir():
            continue
        for child in list(src.iterdir()):
            if not child.name.startswith(".") and not (src == src_att and child.name == INCOMING):
                _merge_move(child, dst / child.name)
        try:
            src.rmdir()  # only when nothing hidden was left in it
        except OSError:
            pass


def _add_links(lib: Library) -> None:
    """Notes saved before P18 get their attachment links: appended to the file as it is (nothing else rewritten)."""
    for path in lib.refresh().values():
        try:
            text = path.read_text(encoding="utf-8")
            entry = parse(text)
        except (*READ_ERRORS, OSError, UnicodeDecodeError):
            continue  # a broken note is left as it is
        if ATTACH_LINE_RE.search(entry.body) and not has_links_block(text):
            section = attachment_links(entry.body, entry.heading_lang or None)
            tmp = path.with_name(f".{path.name}.links.tmp")
            tmp.write_text(text.rstrip("\n") + "\n" + section, encoding="utf-8")
            shutil.copymode(path, tmp)
            os.replace(tmp, path)


def _count_files(att: Path) -> int:
    return len(_files_under(att, skip=att / INCOMING))


def _identity(cfg: Config) -> str | None:
    if not cfg.db_path.exists():
        return None
    conn = connect(cfg.db_path, embed_dim=cfg.embed.dim)
    try:
        return library_identity(conn)
    finally:
        conn.close()


def adopt_library(cfg: Config, config_path: Path, folder: Path) -> int:
    """The library folder was moved or renamed by hand: point config.toml at it again. It must carry this
    library's marker. Returns the number of entries in it; the index follows on the service's next scan."""
    folder = Path(folder).expanduser().resolve()
    if (problem := library_dir_problem(cfg.data_dir, folder)) is not None:
        raise MoveError(t("move.cannot_use", problem=problem))
    mark, identity = read_marker(folder), _identity(cfg)
    if mark is None or (identity is not None and mark != identity):
        raise MoveError(t("move.not_ours", folder=folder, marker=MARKER))
    _set_config(config_path, folder)
    return len(Library(folder, cfg.trash_dir, dataclasses.replace(cfg, library_folder=folder).attachments_dir)
               .refresh())


def move_library(cfg: Config, config_path: Path, target: Path, now: datetime | None = None) -> MoveResult:
    """The service must not be running (the caller holds the instance lock)."""
    target = Path(target).expanduser().resolve()
    state_path = cfg.data_dir / STATE_NAME
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    if state and Path(state["to"]) != target:
        raise MoveError(t("move.unfinished", to=state["to"]))
    if (problem := library_dir_problem(cfg.data_dir, target)) is not None:
        raise MoveError(t("move.cannot_move_here", problem=problem))
    dst_att = dataclasses.replace(cfg, library_folder=target).attachments_dir
    if state is None:
        src_lib, src_att = cfg.library_dir, cfg.attachments_dir
        here = src_lib.resolve()
        if target == here:
            raise MoveError(t("move.already_there", target=target))
        if target.is_relative_to(here) or here.is_relative_to(target):
            raise MoveError(t("move.nested"))
        pairs = _plan(src_lib, src_att, target, dst_att)
        src_ids = Library(src_lib, cfg.trash_dir, src_att).refresh()
        if problem := _conflict(pairs, src_ids, Library(target, cfg.trash_dir, dst_att)):
            raise MoveError(problem)
        if read_marker(target) is not None and read_marker(target) != _identity(cfg):
            raise MoveError(t("move.foreign_library", target=target))
        _copy(pairs)
        backup = cfg.backups_dir / f"library-before-move-{(now or datetime.now()):%Y%m%d-%H%M%S}"
        state = {"from": str(src_lib), "attachments_from": str(src_att), "to": str(target), "backup": str(backup)}
        tmp = state_path.with_name(f"{STATE_NAME}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, state_path)
    # switched from here on: every step below may run again
    conn = connect(cfg.db_path, embed_dim=cfg.embed.dim)
    try:
        claim_library(target, conn)  # the marker first: a service started now finds its folder
        identity = library_identity(conn)
        _set_config(config_path, target)
        new_lib = Library(target, cfg.trash_dir, dst_att)
        index = Index(conn, None)
        new_lib.index_paths = index.paths
        _add_links(new_lib)
        for eid, path in new_lib.refresh().items():
            index.set_path(eid, path)
    finally:
        conn.close()
    backup = Path(state["backup"])
    _back_up_old(Path(state["from"]), Path(state["attachments_from"]), backup, identity)
    state_path.unlink(missing_ok=True)
    return MoveResult(target, len(new_lib.refresh()), _count_files(dst_att), backup if backup.exists() else None)
