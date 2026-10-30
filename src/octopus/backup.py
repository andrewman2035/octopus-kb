"""Daily SQLite backups, 7 kept. Markdown needs no backup here: it is plain files."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
from datetime import date
from pathlib import Path

KEEP = 7
BACKUP_NAME = re.compile(r"octopus-\d{8}\.db")
STALE_TMP_SECONDS = 86400  # a younger .tmp may be a backup in progress (e.g. `octopus backup` beside the service)


def backup_db(conn: sqlite3.Connection, backups_dir: Path, today: date) -> Path | None:
    """Copy the live database (SQLite online backup API) to octopus-YYYYMMDD.db; None if today's exists.

    The copy holds everything the database does: private (0600) from the first byte, in a 0700 directory."""
    backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = backups_dir / f"octopus-{today:%Y%m%d}.db"
    if target.exists():
        return None
    # `octopus backup` beside the service: one temp file each. The pid alone is not enough: in Docker the service and
    # `docker compose run … backup` are both pid 1 in their own containers, sharing /data
    tmp = target.with_name(f"{target.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp")
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))  # new, so private from the first byte
    dest = sqlite3.connect(tmp)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    os.chmod(tmp, 0o600)
    tmp.replace(target)
    return target


def prune(backups_dir: Path, keep: int = KEEP, now: float | None = None) -> list[Path]:
    """Remove all but the `keep` newest daily backups (only files named octopus-YYYYMMDD.db) and
    octopus-*.tmp leftovers older than a day; returns the backups removed."""
    now = time.time() if now is None else now
    for p in backups_dir.glob("octopus-*.tmp"):
        try:
            if now - p.stat().st_mtime > STALE_TMP_SECONDS:
                p.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
    files = sorted(p for p in backups_dir.glob("octopus-*.db") if BACKUP_NAME.fullmatch(p.name) and p.is_file())
    old = files[:-keep] if len(files) > keep else []
    for p in old:
        p.unlink(missing_ok=True)
    return old
