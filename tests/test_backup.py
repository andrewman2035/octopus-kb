import sqlite3
from datetime import date

from octopus.backup import KEEP, backup_db, prune
from octopus.db import connect


def test_backup_once_per_day_and_readable(tmp_path):
    conn = connect(tmp_path / "octopus.db", embed_dim=8)
    conn.execute("INSERT INTO settings(key, value) VALUES ('k', 'v')")
    out = backup_db(conn, tmp_path / "backups", date(2026, 10, 13))
    assert out == tmp_path / "backups" / "octopus-20261013.db"
    copy = sqlite3.connect(out)
    assert copy.execute("SELECT value FROM settings WHERE key = 'k'").fetchone()[0] == "v"
    assert backup_db(conn, tmp_path / "backups", date(2026, 10, 13)) is None


def test_prune_keeps_newest(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    for day in range(1, 11):
        (d / f"octopus-202610{day:02d}.db").write_bytes(b"x")
    (d / "notes.txt").write_text("keep me")
    removed = prune(d)
    assert len(removed) == 10 - KEEP
    left = sorted(p.name for p in d.glob("octopus-*.db"))
    assert left[0] == "octopus-20261004.db" and left[-1] == "octopus-20261010.db"
    assert (d / "notes.txt").exists()


# ---- P4 Task 5: backup hygiene ----

def test_prune_counts_only_real_daily_backups(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    for day in range(1, 9):
        (d / f"octopus-202610{day:02d}.db").write_bytes(b"x")
    odd = ["octopus-abcdefgh.db", "octopus-2026101.db", "octopus-202610011.db"]
    for name in odd:
        (d / name).write_bytes(b"keep")
    removed = prune(d)
    assert [p.name for p in removed] == ["octopus-20261001.db"]  # 8 real ones, 7 kept; look-alikes ignored
    assert all((d / name).exists() for name in odd)


def test_prune_removes_stale_temp_files_only(tmp_path):
    import os
    import time

    d = tmp_path / "backups"
    d.mkdir()
    stale, fresh = d / "octopus-20261001.db.tmp", d / "octopus-20261013.db.tmp"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")  # maybe a backup in progress right now
    old = time.time() - 2 * 86400
    os.utime(stale, (old, old))
    prune(d)
    assert not stale.exists() and fresh.exists()


def test_backup_file_and_dir_are_private(tmp_path):
    conn = connect(tmp_path / "octopus.db", embed_dim=8)
    out = backup_db(conn, tmp_path / "backups", date(2026, 10, 13))
    assert (tmp_path / "backups").stat().st_mode & 0o777 == 0o700
    assert out.stat().st_mode & 0o777 == 0o600


def test_a_backup_never_touches_another_process_s_temp_file(tmp_path):
    # P5 deferred minor: `octopus backup` and the service's daily backup on the same day shared one temp file
    import os
    import time

    conn = connect(tmp_path / "octopus.db", embed_dim=8)
    d = tmp_path / "backups"
    d.mkdir()
    # the other process, still copying: any pid but ours (in a container pytest itself is pid 1)
    theirs = d / f"octopus-20261013.db.{os.getpid() + 1}.tmp"
    theirs.write_bytes(b"in progress")
    out = backup_db(conn, d, date(2026, 10, 13))
    assert out == d / "octopus-20261013.db" and theirs.read_bytes() == b"in progress"
    assert sorted(p.name for p in d.iterdir()) == ["octopus-20261013.db", theirs.name]
    old = time.time() - 2 * 86400
    os.utime(theirs, (old, old))
    prune(d)  # left over after a day: removed like the older name
    assert not theirs.exists()


def test_a_backup_never_touches_a_temp_file_of_another_container_with_our_pid(tmp_path):
    """In Docker the service and `docker compose run … backup` are each pid 1 in their own container, sharing /data."""
    import os

    conn = connect(tmp_path / "octopus.db", embed_dim=8)
    d = tmp_path / "backups"
    d.mkdir()
    theirs = d / f"octopus-20261013.db.{os.getpid()}.tmp"  # same pid, another container, still copying
    theirs.write_bytes(b"in progress")
    assert backup_db(conn, d, date(2026, 10, 13)) == d / "octopus-20261013.db"
    assert theirs.read_bytes() == b"in progress"
    assert sorted(p.name for p in d.iterdir()) == ["octopus-20261013.db", theirs.name]
