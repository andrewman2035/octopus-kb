"""Build a clean public tree from a git commit (default HEAD). Nothing is published: no remote, no push.

    uv run python scripts/export_public.py OUT_DIR [--private-markers FILE ...]
                                                   [--init-git --author "Name <email>"]

- Copies the tracked files of the commit, minus the internal ones (PRIVATE_PATHS / PRIVATE_GLOBS below) and every
  `.shortcut` file, always and with no override: a signed Shortcut carries the signer's Apple certificate, and users
  build and sign their own with extension/shortcuts/build.py.
- Scans every copied file for generic markers (email addresses, home-directory paths) and for every non-empty line
  of each --private-markers file, in the forms a marker may take: any case, full-width, split across lines,
  URL-encoded, slugged, and for an entry id also its hex part and short forms. Text files may be UTF-8 or UTF-16
  (with a BOM); any other binary must be on BINARY_ALLOWLIST and is searched byte for byte. Any hit aborts: the
  output is removed and each hit is printed as file:line plus the kind of marker (or which line of which markers
  file matched). The marker text itself is never printed.
- Markers files: UTF-8 (a BOM is ignored), one marker per line, blank lines skipped. A one-word ASCII marker of at
  most 5 letters is matched only as written (case-sensitive, whole word, no other forms): such words are also
  everyday words and paths. The run summary lists which lines got that treatment.
- Refuses while an owner TODO placeholder (OWNER_TODO below) is still anywhere in the exported files.
- --init-git makes a fresh repository in OUT_DIR with one commit by --author, dated in UTC, holding exactly the
  exported files. The commit message is scanned too. It never adds a remote.
- Refuses to write into a directory that exists and is not empty; removes its output on any failure.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

# Internal process documents and local tool state: never part of the public tree.
PRIVATE_PATHS = ("docs/superpowers/", ".omc/", ".superpowers/", ".claude/")  # at any depth
ROOT_PRIVATE_PATHS = ("scratch/",)  # only at the top of the tree
PRIVATE_GLOBS = ("docs/smoke-*.md", "*.DS_Store", "*.orig", "*.rej", "*.swp")
ROOT_PRIVATE_GLOBS = ("scratch.*",)
SHORTCUT_GLOB = "*.shortcut"  # at any depth (fnmatch's * crosses /): never exported
# Binary files that may be exported; they are searched byte for byte. Any other binary aborts the export.
BINARY_ALLOWLIST = ("extension/chrome/icons/*.png",)

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
# Reserved for documentation (RFC 2606 / 6761; any domain with an "example" label counts).
ALLOWED_DOMAINS = ("example.com", "example.org", "example.net")
ALLOWED_SUFFIXES = (".example", ".test", ".invalid", ".localhost", ".example.com", ".example.org", ".example.net")
ALLOWED_LOCAL_PARTS = ("noreply", "no-reply")
# iLink (WeChat ClawBot) user and bot ids look like addresses: only these invented ones are allowed.
ILINK_DOMAINS = ("im.wechat", "im.bot")
ILINK_PLACEHOLDERS = ("owner", "stranger", "friend", "new", "old", "x", "u", "b", "b2", "wx-owner-7f3a9c")
# Text that looks like an address but is not one: the tail of a Douyin share code pasted in tests.
ALLOWED_ADDRESSES = ("T@p.Rw",)
HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)([A-Za-z0-9._-]+)")
PLACEHOLDER_USERS = ("me", "you", "user", "runner", "octopus")

ENTRY_ID = re.compile(r"\d{8}-([0-9a-f]{6})")
OWNER_TODO = "OWNER" + "-TODO:"  # written in two parts, so this file does not carry the placeholder itself
SHORT_WORD = re.compile(r"[A-Za-z]{1,5}")  # a one-word marker this short is matched only as written
JOIN_DROP = re.compile(r"[\s\"'`#+/\\]+")  # dropped for the joined copy: whitespace, quotes, comment marks, "+"
SLUG_SEP = re.compile(r"[\W_]+")
MIN_JOINED = 6  # a marker shorter than this once joined is not looked for in the joined copy


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _matches(pattern: str, ascii_marker: bool) -> re.Pattern:
    """An ASCII marker never matches inside a longer ASCII word; Chinese around it is fine."""
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])" if ascii_marker else pattern)


@dataclass
class Marker:
    file: int  # 1-based: which --private-markers file
    line: int
    exact: re.Pattern | None = None  # a short one-word marker: as written, on NFKC text (no case folding)
    patterns: list[re.Pattern] = field(default_factory=list)  # on normalized text
    joined: str | None = None  # looked for in the joined copy of the normalized text
    raw: list[bytes] = field(default_factory=list)  # looked for in binary files

    @property
    def where(self) -> str:
        return f"markers file {self.file}, line {self.line}"


def _marker(file: int, line: int, text: str) -> Marker:
    m = Marker(file, line)
    m.raw = sorted({form.encode(enc) for form in (text, norm(text)) for enc in ("utf-8", "utf-16-le", "utf-16-be")})
    if SHORT_WORD.fullmatch(text):
        m.exact = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(text)}(?![A-Za-z0-9_])")
        return m
    n = " ".join(norm(text).split())
    ascii_marker = n.isascii()
    if (eid := ENTRY_ID.fullmatch(n)) is not None:
        hexpart = eid.group(1)
        forms = {hexpart} | {f for k in (4, 5) for f in (hexpart[:k], hexpart[-k:])
                             if re.search(r"\d", f) and re.search(r"[a-f]", f)}
        m.patterns = [re.compile(re.escape(n))] + [_matches(re.escape(f), True) for f in sorted(forms)]
        m.joined = n
        return m
    m.patterns.append(_matches(r"\s+".join(re.escape(w) for w in n.split(" ")), ascii_marker))
    parts = [p for p in SLUG_SEP.split(n) if p]
    if len(parts) > 1:
        m.patterns += [_matches(re.escape(sep.join(parts)), ascii_marker) for sep in ("-", "_")]
    joined = JOIN_DROP.sub("", n)
    m.joined = joined if len(joined) >= MIN_JOINED else None
    return m


def read_markers(paths: list[Path]) -> list[Marker]:
    """Every non-empty line of every markers file (a BOM is ignored)."""
    out = []
    for f, path in enumerate(paths, 1):
        for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            text = line.replace("﻿", "").strip()
            if text:
                out.append(_marker(f, n, text))
    return out


def marker_hits(text: str, markers: list[Marker]) -> list[tuple[int, int, int]]:
    """(line in text, markers file, markers line) for each marker found, once per line and marker: on each line as
    it is and URL-decoded, and on a copy of the whole text joined without whitespace, quotes or comment marks."""
    found: set[tuple[int, int, int]] = set()
    pieces, starts, pos = [], [], 0
    for n, line in enumerate(text.splitlines(), 1):
        nfkc = unicodedata.normalize("NFKC", line)
        forms = [norm(line)] + ([norm(unquote(line))] if "%" in line else [])
        for m in markers:
            hit = (m.exact.search(nfkc) if m.exact is not None
                   else any(p.search(form) for p in m.patterns for form in forms))
            if hit:
                found.add((n, m.file, m.line))
        piece = JOIN_DROP.sub("", forms[0])
        starts.append(pos)
        pieces.append(piece)
        pos += len(piece)
    whole = "".join(pieces)
    for m in markers:
        for hit in re.finditer(re.escape(m.joined), whole) if m.joined else ():
            n = max(i for i, start in enumerate(starts, 1) if start <= hit.start())
            found.add((n, m.file, m.line))
    return sorted(found)


def _allowed_email(m: re.Match) -> bool:
    domain = m.group(1).lower()
    local = m.group(0).split("@", 1)[0].lower()
    if domain in ILINK_DOMAINS:
        return local in ILINK_PLACEHOLDERS
    return (m.group(0) in ALLOWED_ADDRESSES or domain in ALLOWED_DOMAINS or domain.endswith(ALLOWED_SUFFIXES)
            or "example" in domain.split(".") or local in ALLOWED_LOCAL_PARTS)


def generic_hits(text: str) -> list[tuple[int, str]]:
    """(line number, kind) for each generic marker in text (NFKC, so full-width forms count): an email address
    outside the reserved domains, or a home directory of a real-looking user."""
    hits = []
    for n, line in enumerate(unicodedata.normalize("NFKC", text).splitlines(), 1):
        if any(not _allowed_email(m) for m in EMAIL.finditer(line)):
            hits.append((n, "email address"))
        if any(m.group(1) not in PLACEHOLDER_USERS for m in HOME_PATH.finditer(line)):
            hits.append((n, "home directory path"))
    return hits


def is_private(path: str) -> bool:
    """A tracked path that stays out of the public tree."""
    if any(path.startswith(p) or f"/{p}" in f"/{path}" for p in PRIVATE_PATHS):
        return True
    if path.startswith(ROOT_PRIVATE_PATHS) or ("/" not in path and any(fnmatch.fnmatch(path, g)
                                                                       for g in ROOT_PRIVATE_GLOBS)):
        return True
    return any(fnmatch.fnmatch(path, g) for g in (*PRIVATE_GLOBS, SHORTCUT_GLOB))


def _decode(data: bytes) -> str | None:
    """The text of a UTF-8 file, or of a UTF-16 one with a BOM; None for anything else."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def scan_file(path: Path, rel: str, markers: list[Marker]) -> list[str]:
    """The hits in one file as printable lines (never the marker text). A binary file is searched byte for byte
    when it is on the allowlist, and is a hit itself when it is not."""
    data = path.read_bytes()
    text = _decode(data)
    if text is None:
        if not any(fnmatch.fnmatch(rel, g) for g in BINARY_ALLOWLIST):
            return [f"{rel}: binary file not on the allowlist (BINARY_ALLOWLIST in scripts/export_public.py)"]
        return [f"{rel}: private marker in the bytes ({m.where})" for m in markers if any(r in data for r in m.raw)]
    out = [f"{rel}:{n}: owner TODO placeholder still present" for n, line in enumerate(text.splitlines(), 1)
           if OWNER_TODO in line]
    out += [f"{rel}:{n}: generic marker ({kind})" for n, kind in generic_hits(text)]
    where = {(m.file, m.line): m.where for m in markers}
    return out + [f"{rel}:{n}: private marker ({where[f, ln]})" for n, f, ln in marker_hits(text, markers)]


def message_hits(message: str, markers: list[Marker]) -> list[str]:
    where = {(m.file, m.line): m.where for m in markers}
    return ([f"commit message: generic marker ({kind})" for _, kind in generic_hits(message)]
            + [f"commit message: private marker ({where[f, ln]})" for _, f, ln in marker_hits(message, markers)])


def _git(repo: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, **kw)


def tracked_files(repo: Path, rev: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "-z", "--name-only", rev).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def export(repo: Path, rev: str, out: Path) -> tuple[list[str], list[str]]:
    """Writes the commit's public files into out; returns (copied, excluded) paths."""
    files = tracked_files(repo, rev)
    excluded = [p for p in files if is_private(p)]
    keep = set(files) - set(excluded)
    archive = _git(repo, "archive", "--format=tar", rev).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and m.name in keep]
        tar.extractall(out, members=members, filter="data")
    return sorted(m.name for m in members), excluded


def parse_author(author: str) -> tuple[str, str]:
    m = re.fullmatch(r"\s*([^<>]+?)\s*<([^<>\s]+@[^<>\s]+)>\s*", author)
    if not m:
        raise SystemExit('--author must look like "Name <email>"')
    return m.group(1), m.group(2)


def init_git(out: Path, name: str, email: str, message: str, expected: list[str]) -> None:
    """One commit by name <email>, dated now in UTC, holding exactly the expected files. Identity and date come from
    this call only: no templates or hooks, no signing, no ignore files, no global or system git config and no GIT_*
    variables from the environment."""
    stamp = f"{int(time.time())} +0000"
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email,
               GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
               TZ="UTC")
    opts = ["-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-c", "core.excludesFile=/dev/null",
            "-c", f"user.name={name}", "-c", f"user.email={email}"]

    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(out), *opts, *args], check=True, capture_output=True, text=True,
                              env=env).stdout

    subprocess.run(["git", "init", "-q", "--template=", "-b", "main", str(out)], check=True, env=env)
    run("add", "-A", "-f")
    staged = sorted(p for p in run("ls-files", "-z").split("\0") if p)
    if staged != sorted(expected):
        raise SystemExit(f"the commit would hold {len(staged)} files, not the {len(expected)} exported")
    run("commit", "-q", "--no-verify", "-m", message)


def _clear(out: Path) -> None:
    if out.exists():
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a clean public tree (never publishes anything).")
    ap.add_argument("out", type=Path, help="target directory: must not exist, or be empty")
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--private-markers", type=Path, action="append", default=[],
                    help="a file with one private marker per line (may be given more than once)")
    ap.add_argument("--init-git", action="store_true", help="make a fresh repository with one commit")
    ap.add_argument("--author", help='"Name <email>" for that commit (required with --init-git)')
    ap.add_argument("--message", default="Initial public release")
    args = ap.parse_args(argv)
    if args.init_git and not args.author:
        ap.error("--init-git needs --author")
    author = parse_author(args.author) if args.init_git else None
    out: Path = args.out
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"refusing: {out} exists and is not an empty directory", file=sys.stderr)
        return 2
    markers = read_markers(args.private_markers)
    if author is not None and (refused := message_hits(args.message, markers)):
        print("\n".join(refused), file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    try:
        copied, excluded = export(args.repo, args.rev, out)
        hits = [h for rel in copied for h in scan_file(out / rel, rel, markers)]
        if hits:
            print("\n".join(hits), file=sys.stderr)
            print(f"aborted: {len(hits)} hit(s); the output was removed", file=sys.stderr)
            _clear(out)
            return 1
        binaries = [rel for rel in copied if _decode((out / rel).read_bytes()) is None]
        if author is not None:
            init_git(out, *author, args.message, copied)
    except BaseException:
        _clear(out)
        raise
    size = sum((out / rel).stat().st_size for rel in copied)
    print(f"exported {len(copied)} files, {size} bytes, into {out}")
    print(f"excluded {len(excluded)} tracked files:")
    for p in excluded:
        print(f"  {p}")
    print(f"not text, searched byte for byte ({len(binaries)}):")
    for p in binaries:
        print(f"  {p} (binary, byte-scanned)")
    short: dict[int, list[int]] = {}
    for m in markers:
        if m.exact is not None:
            short.setdefault(m.file, []).append(m.line)
    if short:
        where = "; ".join(f"file {f} line{'s' if len(ls) > 1 else ''} {', '.join(map(str, ls))}"
                          for f, ls in sorted(short.items()))
        print(f"{sum(map(len, short.values()))} short one-word markers matched only as written (case-sensitive, "
              f"whole word): {where}")
    print(f"scanned: generic markers and {len(markers)} private markers from {len(args.private_markers)} "
          f"file(s): 0 hits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
