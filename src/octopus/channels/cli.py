"""Terminal channel for local use and testing: `octopus chat`."""
from __future__ import annotations

import asyncio
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Callable

from octopus.channels.base import OnMessage, Reply, SendResult
from octopus.i18n import t
from octopus.media import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES
from octopus.models import Attachment, IncomingMessage

MAX_FILE_BYTES = 2 * 1024**3


def _as_path(line: str) -> Path | None:
    s = line.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    s = re.sub(r"\\(.)", r"\1", s)  # Terminal escapes spaces, parentheses etc. with a backslash
    if not (s.startswith("/") or s == "~" or s.startswith("~/")):
        return None
    try:
        p = Path(s).expanduser()
        return p if p.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _kind(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in IMAGE_SUFFIXES:
        return "image"
    if ext in AUDIO_SUFFIXES:
        return "audio"
    return "video" if ext in VIDEO_SUFFIXES else "file"


class CLIChannel:
    name = "cli"
    acks_saves = True

    def __init__(self, input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print,
                 incoming_dir: Path | None = None):
        self.incoming_dir = incoming_dir
        self.input_fn = input_fn
        self.output = output
        self._tasks: set[asyncio.Task] = set()
        self._closing = threading.Event()

    def _read_loop(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        """Runs on a daemon thread so a blocked input() never keeps the process alive."""
        def push(item: str | None) -> bool:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
                return True
            except RuntimeError:  # loop already closed
                return False

        while not self._closing.is_set():
            try:
                line = self.input_fn(t("cli_channel.prompt"))
            except EOFError:
                push(None)
                return
            except Exception:
                push(None)
                raise
            if not push(line):
                return

    async def start(self, on_message: OnMessage) -> None:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closing = threading.Event()
        threading.Thread(target=self._read_loop, args=(asyncio.get_running_loop(), queue), daemon=True).start()
        try:
            while True:
                line = await queue.get()
                if line is None:
                    break
                line = line.strip()
                if line == "/quit":
                    break
                if not line:
                    continue
                attachments = []
                path = _as_path(line) if self.incoming_dir else None
                if path is not None:
                    dest = self.incoming_dir / uuid.uuid4().hex
                    try:
                        if path.stat().st_size > MAX_FILE_BYTES:
                            self.output(t("cli_channel.too_big"))
                            continue
                        dest.mkdir(parents=True, exist_ok=True)
                        copy = dest / path.name
                        await asyncio.to_thread(shutil.copy2, path, copy)
                        size = copy.stat().st_size
                    except OSError as e:
                        shutil.rmtree(dest, ignore_errors=True)
                        self.output(t("cli_channel.copy_failed", error=e))
                        continue
                    attachments = [Attachment(kind=_kind(path), path=copy, filename=path.name, size=size)]
                    line = ""
                msg = IncomingMessage(channel=self.name, user_id="local", text=line, attachments=attachments,
                                      message_id=uuid.uuid4().hex)
                task = asyncio.create_task(on_message(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            self._closing.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def send(self, user_id: str, reply: Reply) -> SendResult:
        text = reply.text
        hints = [b.fallback for b in reply.buttons if b.fallback]
        if hints:
            text += "\n" + " / ".join(hints)
        self.output(f"\nOctopus> {text}\n")
        return SendResult(True)
